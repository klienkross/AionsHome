"""Book reading session: WS-driven, sentence-by-sentence TTS with prefetch."""
import asyncio
import base64
import hashlib
import json
import logging
import re
import time

import httpx

from config import get_key, TTS_CACHE_DIR, SETTINGS
from database import get_db
from sentinel import call_sentinel_text
from tts import extract_hints_and_clean

logger = logging.getLogger("reading")

_sessions: dict[str, "ReadingSession"] = {}


def get_session(book_id: str) -> "ReadingSession | None":
    return _sessions.get(book_id)


def _row_dict(cursor, row):
    return {col[0]: row[i] for i, col in enumerate(cursor.description)}


_SENTENCE_ENDS = set('。！？…!?')
_CHUNK_MAX_CHARS = 800


def _split_sentences(text: str) -> list[str]:
    """将朗读稿按句号切分为句子，每句 ≤200 字。吐槽单独成句。"""
    # 先分离吐槽
    parts = re.split(r'(「吐槽：[^」]*」)', text)
    sentences = []
    for part in parts:
        part = part.strip()
        if not part:
            continue
        if part.startswith('「吐槽：'):
            sentences.append(part)
            continue
        # 按句号切
        buf = ""
        for ch in part:
            buf += ch
            if ch in _SENTENCE_ENDS and len(buf) >= 10:
                sentences.append(buf.strip())
                buf = ""
            elif len(buf) >= 200:
                # 强制切
                sentences.append(buf.strip())
                buf = ""
        if buf.strip():
            sentences.append(buf.strip())
    return [s for s in sentences if s]


def _split_chapter_into_chunks(paragraphs: list[str]) -> list[str]:
    """按段落边界切分为 ~800 字 chunk，超长段落按句号二次切分。"""
    chunks = []
    current_paras = []
    current_chars = 0
    for p in paragraphs:
        plen = len(p)
        if current_chars + plen > _CHUNK_MAX_CHARS and current_paras:
            chunks.append("\n".join(current_paras))
            current_paras = []
            current_chars = 0
        if plen > _CHUNK_MAX_CHARS:
            # 超长段落按句号切
            buf = ""
            for ch in p:
                buf += ch
                if len(buf) >= _CHUNK_MAX_CHARS and ch in _SENTENCE_ENDS:
                    chunks.append(buf)
                    buf = ""
            if buf:
                chunks.append(buf)
        else:
            current_paras.append(p)
            current_chars += plen
    if current_paras:
        chunks.append("\n".join(current_paras))
    return chunks


class ReadingSession:
    IDLE_TIMEOUT = 900  # 15 分钟无交互自动停止

    def __init__(self, book_id: str, chapter_index: int, conv_id: str, ws):
        self.book_id = book_id
        self.chapter_index = chapter_index
        self.conv_id = conv_id
        self._ws = ws
        self._paused = False
        self._stopped = False
        self._last_interaction = time.time()
        self._audio_ended = asyncio.Event()
        self._book_title = None
        self._context_summary = ""
        self._chunk_index = 0
        self._total_chunks = 0
        self._http = httpx.AsyncClient(timeout=60)

        _sessions[book_id] = self

    async def _ws_send(self, data: dict):
        """Send message to the initiating WS client."""
        try:
            await self._ws.send_text(json.dumps(data, ensure_ascii=False))
        except Exception:
            logger.debug("WS send failed, stopping session", exc_info=True)
            self._stopped = True

    async def run(self):
        """Main reading loop."""
        try:
            logger.info(f"ReadingSession started: book={self.book_id} chapter={self.chapter_index}")

            # Load book metadata
            async with get_db() as db:
                db.row_factory = _row_dict
                cur = await db.execute("SELECT title FROM books WHERE book_id = ?", (self.book_id,))
                row = await cur.fetchone()
                self._book_title = row["title"] if row else "未知"

            logger.info(f"ReadingSession book_title={self._book_title}")

            while not self._stopped:
                # Load chapter
                async with get_db() as db:
                    db.row_factory = _row_dict
                    cur = await db.execute(
                        "SELECT title, paragraphs FROM book_chapters WHERE book_id = ? AND chapter_index = ?",
                        (self.book_id, self.chapter_index),
                    )
                    chapter = await cur.fetchone()

                if not chapter:
                    break

                chapter_title = chapter.get("title", "")
                paragraphs = json.loads(chapter.get("paragraphs", "[]"))
                chunks = _split_chapter_into_chunks(paragraphs)
                self._total_chunks = len(chunks)

                await self._ws_send({
                    "type": "reading_chapter_start",
                    "chapter_index": self.chapter_index,
                    "chapter_title": chapter_title,
                })

                # Generate context summary for this chapter
                self._context_summary = await self._generate_context_summary()

                # Read each chunk
                for ci, chunk_text in enumerate(chunks):
                    if self._stopped:
                        break
                    self._chunk_index = ci

                    # Generate annotated reading script
                    script = await self._generate_script(chunk_text, chapter_title)
                    if self._stopped:
                        break

                    await self._ws_send({
                        "type": "reading_chunk_start",
                        "chapter_index": self.chapter_index,
                        "chunk_index": ci,
                        "total_chunks": self._total_chunks,
                        "script": script,
                    })

                    # Split into sentences and read them
                    sentences = _split_sentences(script)
                    await self._read_sentences(sentences)
                    if self._stopped:
                        break

                if self._stopped:
                    break

                # Chapter done → advance
                self.chapter_index += 1

            # End
            if self._stopped:
                reason = "timeout" if time.time() - self._last_interaction > self.IDLE_TIMEOUT else "user_stop"
            else:
                reason = "book_end"

            summary = await self._write_end_summary(reason)
            await self._ws_send({"type": "reading_done", "reason": reason, "summary": summary})

        except Exception:
            logger.exception(f"ReadingSession crashed: book={self.book_id}")
            try:
                await self._ws_send({"type": "reading_error", "message": "朗读会话异常终止"})
            except Exception:
                pass
        finally:
            await self._http.aclose()
            _sessions.pop(self.book_id, None)

    async def _read_sentences(self, sentences: list[str]):
        """Read sentences with one-ahead prefetch."""
        if not sentences:
            return

        # Start prefetching sentence 0
        next_task = asyncio.create_task(self._tts_one(sentences[0]))

        for i, sentence in enumerate(sentences):
            if self._stopped:
                next_task.cancel()
                break

            # Wait for current sentence's audio
            audio_url = await next_task

            # Start prefetching next sentence
            if i + 1 < len(sentences):
                next_task = asyncio.create_task(self._tts_one(sentences[i + 1]))

            # Push audio to frontend
            if audio_url:
                await self._ws_send({
                    "type": "reading_audio",
                    "url": audio_url,
                    "seq": i,
                    "text": sentence,
                    "chapter_index": self.chapter_index,
                    "chunk_index": self._chunk_index,
                    "total_chunks": self._total_chunks,
                })

                # Wait for frontend to signal audio ended (or timeout/stop)
                await self._wait_audio_ended()
            else:
                # TTS failed for this sentence — skip it
                logger.warning("TTS failed, skipping sentence %d", i)

    async def _wait_audio_ended(self):
        """Wait for audio_ended signal, respecting pause and idle timeout."""
        while not self._stopped:
            if self._paused:
                await asyncio.sleep(0.3)
                self._check_idle_timeout()
                continue

            self._audio_ended.clear()
            try:
                await asyncio.wait_for(self._audio_ended.wait(), timeout=60)
                return
            except asyncio.TimeoutError:
                self._check_idle_timeout()

    def _check_idle_timeout(self):
        if time.time() - self._last_interaction > self.IDLE_TIMEOUT:
            self._stopped = True

    async def _tts_one(self, text: str) -> str | None:
        """Synthesize one sentence. Returns audio URL or None on failure."""
        key = get_key("mimo")
        if not key:
            logger.warning("TTS: no MiMo API key")
            return None

        style_hint, clean_text = extract_hints_and_clean(text)
        if not clean_text:
            logger.warning("TTS: empty clean_text after extraction")
            return None

        voice = SETTINGS.get("tts_voice") or "冰糖"
        safe_id = re.sub(r'[^a-zA-Z0-9_\-]', '', f"read_{self.book_id[:8]}_{int(time.time()*1000)}")
        chunk_name = f"{safe_id}_s{hashlib.md5(clean_text.encode()).hexdigest()[:8]}"

        messages = []
        if style_hint:
            messages.append({"role": "user", "content": style_hint})
        messages.append({"role": "assistant", "content": clean_text})

        try:
            resp = await self._http.post(
                "https://api.xiaomimimo.com/v1/chat/completions",
                headers={"api-key": key, "Content-Type": "application/json"},
                json={
                    "model": "mimo-v2.5-tts",
                    "messages": messages,
                    "audio": {"format": "wav", "voice": voice},
                },
            )
            if resp.status_code != 200:
                logger.warning("TTS API error: status=%d body=%s", resp.status_code, resp.text[:200])
                return None

            data = resp.json()
            audio_b64 = data["choices"][0]["message"]["audio"]["data"]
            audio_bytes = base64.b64decode(audio_b64)

            cache_path = TTS_CACHE_DIR / f"{chunk_name}.wav"
            cache_path.write_bytes(audio_bytes)
            logger.info("TTS saved: %s (%d bytes)", chunk_name, len(audio_bytes))
            return f"/api/tts/audio/{chunk_name}"

        except Exception as e:
            logger.error("TTS synthesis failed: %s", e)
            return None

    async def _generate_script(self, chunk_text: str, chapter_title: str) -> str:
        """Generate annotated reading script for one chunk."""
        book_title = self._book_title or "这本书"
        system_prompt = f"""你正在为《{book_title}》做有声朗读，当前章节：{chapter_title}。

前情提要：{self._context_summary}

请直接输出原文，仅做以下少量添加：
- 在需要语气变化的位置插入【】语气标注（可用舞台描述风格，如【压低声音，带着一丝狡黠】）
- 可以在需要的部分加一句简短的共读评论（≤25字），用「评论：xxx」格式
- 评论内也可用【】标注语气

注意：尽量保持原文不动。语气标注和评论之外，原文措辞不变。"""

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": chunk_text},
        ]
        result = await call_sentinel_text(messages)
        if not result:
            logger.warning("Script generation failed, using raw text")
            return chunk_text
        return result.strip()

    async def _generate_context_summary(self) -> str:
        """Generate context summary for current chapter."""
        async with get_db() as db:
            db.row_factory = _row_dict
            cur = await db.execute(
                "SELECT chapter_index, summary FROM book_annotations WHERE book_id = ? AND chapter_index < ? ORDER BY chapter_index",
                (self.book_id, self.chapter_index),
            )
            rows = await cur.fetchall()

        prev_summaries = "\n".join(
            f"第{r['chapter_index']+1}章: {r['summary']}" for r in rows if r.get("summary")
        )
        if not prev_summaries:
            return ""

        prompt = f"""请根据以下章节摘要，写一段 ≤200字 的前情提要，包含情节进展、人物状态和整体氛围。用自然口吻。

已有章节摘要：
{prev_summaries}

只需输出前情提要。"""

        result = await call_sentinel_text(prompt)
        return result.strip() if result else ""

    async def _write_end_summary(self, reason: str) -> str:
        """Generate a reading summary and write it to the most recent active conversation.
        Returns the summary text for display in the player."""
        prompt = f"""用户刚结束了《{self._book_title or '书'}》的朗读（读到第{self.chapter_index+1}章，原因: {reason}）。
请根据上下文写一条 ≤200字 的总结消息，用自然的口吻。"""

        result = await call_sentinel_text(prompt)
        summary = result.strip() if result else f"今天读了《{self._book_title}》到第{self.chapter_index+1}章，下次继续~"

        # Find the most recent active conversation (same logic as auto-digest)
        try:
            async with get_db() as db:
                db.row_factory = _row_dict
                cur = await db.execute(
                    "SELECT id FROM conversations ORDER BY updated_at DESC LIMIT 1"
                )
                conv_row = await cur.fetchone()

            if conv_row:
                target_conv_id = conv_row["id"]
                msg_id = f"reading_summary_{self.book_id}_{int(time.time())}"
                async with get_db() as db:
                    await db.execute(
                        "INSERT INTO messages (id, conv_id, role, content, created_at) VALUES (?, ?, ?, ?, ?)",
                        (msg_id, target_conv_id, "assistant", summary, time.time()),
                    )
                    await db.execute(
                        "UPDATE conversations SET updated_at = ? WHERE id = ?",
                        (time.time(), target_conv_id),
                    )
                    await db.commit()
                logger.info("Reading summary written to conv=%s", target_conv_id)
        except Exception:
            logger.exception("Failed to write reading summary to DB")

        return summary

    # ── External API (called from WS handler) ──

    def on_audio_ended(self):
        self._last_interaction = time.time()
        self._audio_ended.set()

    def on_pause(self):
        self._paused = True
        self._last_interaction = time.time()

    def on_resume(self):
        self._paused = False
        self._last_interaction = time.time()

    def on_stop(self):
        self._stopped = True
        self._audio_ended.set()  # unblock wait loop

    def on_disconnect(self):
        self._stopped = True
        self._audio_ended.set()
