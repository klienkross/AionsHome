# TTS Book Reading v2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 重写朗读功能为纯 WS 驱动的逐句合成+预取架构，删除 SSE/REST 端点，全屏播放模式 UI。

**Architecture:** 前端通过 WS 发送控制消息（start/pause/resume/stop/audio_ended），后端 ReadingSession 逐句合成 TTS 并推送音频 URL，始终预取下一句。15 分钟无交互自动停止。

**Tech Stack:** Python (FastAPI + asyncio + httpx), MiMo TTS API, WebSocket, vanilla JS + MediaSession API

---

## File Map

| 文件 | 职责 | 类型 |
|------|------|------|
| `aion-chat/reading.py` | ReadingSession：chunk 管理、朗读稿生成、逐句 TTS 合成+预取、超时停止 | Rewrite |
| `aion-chat/tts.py` | 提取 `extract_hints_and_clean` 为模块级公共函数 | Modify |
| `aion-chat/main.py` | WS handler 转发 reading_* 消息；移除 reading router | Modify |
| `aion-chat/routes/reading.py` | 删除 | Delete |
| `aion-chat/static/reading.html` | 替换旧朗读控制为全屏播放模式 UI | Modify |

---

### Task 1: 提取 TTS 公共函数

**Files:**
- Modify: `aion-chat/tts.py`

- [ ] **Step 1: 将 `_extract_hints_and_clean` 改名为公共函数**

在 `aion-chat/tts.py` 第 54 行，将 `_extract_hints_and_clean` 重命名为 `extract_hints_and_clean`（去掉下划线前缀），使其成为模块公共 API：

```python
def extract_hints_and_clean(text: str) -> tuple[str, str]:
    """提取【】中的语气/动作提示，返回 (style_hint, clean_text)"""
    hints = _STAGE_HINT_RE.findall(text)
    style = '，'.join(h.strip() for h in hints if h.strip()) if hints else ''
    clean = _STAGE_HINT_RE.sub('', text)
    clean = _strip_tags(clean).strip()
    return style, clean
```

- [ ] **Step 2: 更新 TTSStreamer 内部引用**

在 `aion-chat/tts.py` 第 203 行，`_dispatch` 方法内将 `_extract_hints_and_clean` 改为 `extract_hints_and_clean`：

```python
    def _dispatch(self, text: str):
        """发起异步合成任务 — 提取【】语气提示，拼接纯文本"""
        style_hint, clean_text = extract_hints_and_clean(text)
```

- [ ] **Step 3: Commit**

```bash
git add aion-chat/tts.py
git commit -m "refactor: 暴露 extract_hints_and_clean 为公共函数"
```

---

### Task 2: 重写 ReadingSession

**Files:**
- Rewrite: `aion-chat/reading.py`

- [ ] **Step 1: 写入完整的新 reading.py**

```python
"""Book reading session: WS-driven, sentence-by-sentence TTS with prefetch."""
import asyncio
import base64
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

        _sessions[book_id] = self

    async def _ws_send(self, data: dict):
        """Send message to the initiating WS client."""
        try:
            await self._ws.send_text(json.dumps(data, ensure_ascii=False))
        except Exception:
            self._stopped = True

    async def run(self):
        """Main reading loop."""
        try:
            # Load book metadata
            async with get_db() as db:
                db.row_factory = _row_dict
                cur = await db.execute("SELECT title FROM books WHERE book_id = ?", (self.book_id,))
                row = await cur.fetchone()
                self._book_title = row["title"] if row else "未知"

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
                    await self._ws_send({"type": "reading_done", "reason": "book_end"})
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

            await self._write_end_summary(reason)
            await self._ws_send({"type": "reading_done", "reason": reason})

        except Exception:
            logger.exception(f"ReadingSession crashed: book={self.book_id}")
            try:
                await self._ws_send({"type": "reading_error", "message": "朗读会话异常终止"})
            except Exception:
                pass
        finally:
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
            return None

        style_hint, clean_text = extract_hints_and_clean(text)
        if not clean_text:
            return None

        voice = SETTINGS.get("tts_voice") or "冰糖"
        safe_id = re.sub(r'[^a-zA-Z0-9_\-]', '', f"read_{self.book_id[:8]}_{int(time.time()*1000)}")
        chunk_name = f"{safe_id}_s{hash(clean_text) % 99999}"

        messages = []
        if style_hint:
            messages.append({"role": "user", "content": style_hint})
        messages.append({"role": "assistant", "content": clean_text})

        try:
            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.post(
                    "https://api.xiaomimimo.com/v1/chat/completions",
                    headers={"api-key": key, "Content-Type": "application/json"},
                    json={
                        "model": "mimo-v2.5-tts",
                        "messages": messages,
                        "audio": {"format": "wav", "voice": voice},
                    },
                )
            if resp.status_code != 200:
                logger.warning("TTS API error: status=%d", resp.status_code)
                return None

            data = resp.json()
            audio_b64 = data["choices"][0]["message"]["audio"]["data"]
            audio_bytes = base64.b64decode(audio_b64)

            cache_path = TTS_CACHE_DIR / f"{chunk_name}.wav"
            cache_path.write_bytes(audio_bytes)
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
- 段落末尾加一句简短的共读吐槽（≤25字），用「吐槽：xxx」格式
- 吐槽内也可用【】标注语气

注意：尽量保持原文不动。语气标注和吐槽之外，原文措辞不变。"""

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

    async def _write_end_summary(self, reason: str):
        """Write summary to messages table."""
        if not self.conv_id:
            return

        prompt = f"""用户刚结束了《{self._book_title or '书'}》的朗读（读到第{self.chapter_index+1}章，原因: {reason}）。
请写一条 ≤200字 的总结消息，像刚读完书在跟朋友随口聊。用自然亲切的口吻。"""

        result = await call_sentinel_text(prompt)
        summary = result.strip() if result else f"今天读了《{self._book_title}》到第{self.chapter_index+1}章，下次继续~"

        msg_id = f"reading_summary_{self.book_id}_{int(time.time())}"
        async with get_db() as db:
            await db.execute(
                "INSERT INTO messages (id, conv_id, role, content, created_at) VALUES (?, ?, ?, ?, ?)",
                (msg_id, self.conv_id, "assistant", summary, time.time()),
            )
            await db.commit()

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
```

- [ ] **Step 2: Commit**

```bash
git add aion-chat/reading.py
git commit -m "feat: 重写 ReadingSession — WS 驱动、逐句合成+预取"
```

---

### Task 3: 修改 WS handler + 清理 router

**Files:**
- Modify: `aion-chat/main.py`
- Delete: `aion-chat/routes/reading.py`

- [ ] **Step 1: 在 main.py 中移除 reading router 导入和挂载**

删除这两行：
```python
from routes import reading as reading_routes
```
```python
app.include_router(reading_routes.router)
```

- [ ] **Step 2: 在 main.py 顶部添加 reading 导入**

在已有的 import 区域添加：
```python
from reading import ReadingSession, get_session
```

- [ ] **Step 3: 在 WS handler 中添加 reading 消息处理**

在 `websocket_endpoint` 函数的 `msg_type` 分支中，在 `elif msg_type == "register_client":` 之后添加：

```python
                elif msg_type == "reading_start":
                    book_id = msg.get("book_id", "")
                    if not book_id:
                        await ws.send_text(json.dumps({"type": "reading_error", "message": "缺少 book_id"}))
                    elif get_session(book_id):
                        await ws.send_text(json.dumps({"type": "reading_error", "message": "该书已有活跃朗读会话"}))
                    else:
                        session = ReadingSession(
                            book_id=book_id,
                            chapter_index=msg.get("chapter_index", 0),
                            conv_id=msg.get("conv_id", ""),
                            ws=ws,
                        )
                        asyncio.create_task(session.run())
                elif msg_type == "reading_audio_ended":
                    # Find session for this WS
                    for s in list(_reading_sessions_for_ws(ws)):
                        s.on_audio_ended()
                elif msg_type == "reading_pause":
                    for s in list(_reading_sessions_for_ws(ws)):
                        s.on_pause()
                elif msg_type == "reading_resume":
                    for s in list(_reading_sessions_for_ws(ws)):
                        s.on_resume()
                elif msg_type == "reading_stop":
                    for s in list(_reading_sessions_for_ws(ws)):
                        s.on_stop()
```

- [ ] **Step 4: 添加辅助函数 `_reading_sessions_for_ws`**

在 `websocket_endpoint` 函数之前添加：

```python
def _reading_sessions_for_ws(ws):
    """Find active reading sessions initiated by this WS connection."""
    from reading import _sessions
    return [s for s in _sessions.values() if s._ws is ws]
```

- [ ] **Step 5: 在 WS disconnect 时清理 reading session**

在 `websocket_endpoint` 的 `finally` 块中，`manager.disconnect(ws)` 之前添加：

```python
        for s in list(_reading_sessions_for_ws(ws)):
            s.on_disconnect()
```

- [ ] **Step 6: 删除 routes/reading.py**

```bash
git rm aion-chat/routes/reading.py
```

- [ ] **Step 7: Commit**

```bash
git add aion-chat/main.py
git commit -m "feat: WS handler 转发 reading 消息，移除 REST 端点"
```

---

### Task 4: 前端全屏播放模式

**Files:**
- Modify: `aion-chat/static/reading.html`

- [ ] **Step 1: 删除旧的朗读控制栏 HTML**

删除 `#readingBar` div（约第 532-547 行的 `<!-- Reading control bar -->` 到 `</div>`）。

- [ ] **Step 2: 在 `</body>` 前添加全屏播放模式 HTML**

在 `</body>` 标签之前（在现有 `<script>` 标签之前）插入：

```html
<!-- ══ 全屏播放模式 ══ -->
<div id="playerMode" style="display:none;position:fixed;inset:0;z-index:9999;
    background:#1a1a2e;color:#eee;flex-direction:column;">

  <!-- 顶栏 -->
  <div style="padding:16px 20px;display:flex;align-items:center;gap:12px;">
    <button onclick="exitPlayerMode()" style="background:none;border:none;color:#eee;font-size:20px;">←</button>
    <div style="flex:1;overflow:hidden;">
      <div id="playerBookTitle" style="font-size:14px;opacity:0.7;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;"></div>
      <div id="playerChapterTitle" style="font-size:12px;opacity:0.5;"></div>
    </div>
  </div>

  <!-- 文本展示区 -->
  <div id="playerText" style="flex:1;overflow-y:auto;padding:24px 20px;font-size:18px;line-height:2;
      display:flex;align-items:center;justify-content:center;text-align:center;">
    <span style="opacity:0.4;">准备中...</span>
  </div>

  <!-- 进度 -->
  <div id="playerProgress" style="text-align:center;font-size:12px;opacity:0.5;padding:8px;">
  </div>

  <!-- 控制栏 -->
  <div style="padding:20px 40px 40px;display:flex;align-items:center;justify-content:center;gap:40px;">
    <button id="playerPauseBtn" onclick="togglePlayerPause()"
      style="width:64px;height:64px;border-radius:50%;border:2px solid #eee;background:none;color:#eee;font-size:24px;">
      ⏸
    </button>
    <button onclick="stopPlayerMode()"
      style="width:44px;height:44px;border-radius:50%;border:1px solid rgba(255,255,255,0.3);background:none;color:#aaa;font-size:16px;">
      ⏹
    </button>
  </div>

  <!-- Hidden audio -->
  <audio id="playerAudio" style="display:none"></audio>
</div>
```

- [ ] **Step 3: 添加播放模式 CSS**

在 `<style>` 块末尾（`</style>` 之前）添加：

```css
/* ── 播放模式 ── */
#playerMode .reading-text { font-size: 18px; line-height: 2; max-width: 600px; }
#playerMode .tone-tag { color: #888; font-size: 0.8em; }
#playerMode .spit { color: #7c9cff; font-style: italic; }
```

- [ ] **Step 4: 替换旧的朗读 JS 逻辑**

删除从 `let readingActive = false;`（约第 1386 行）到文件末尾 `</script></body>` 之前的所有朗读相关 JS（约第 1386-1553 行），替换为：

```javascript
// ══ 全屏播放模式 ══

let _playerActive = false;
let _playerPaused = false;
let _playerBookId = null;

function enterPlayerMode(bookId, chapterIndex) {
  _playerBookId = bookId;
  _playerActive = true;
  _playerPaused = false;
  document.getElementById('playerMode').style.display = 'flex';
  document.getElementById('playerBookTitle').textContent = currentBookTitle || '';
  document.getElementById('playerPauseBtn').textContent = '⏸';
  document.getElementById('playerText').innerHTML = '<span style="opacity:0.4;">准备中...</span>';
  document.getElementById('playerProgress').textContent = '';

  // Send start via WS
  const convId = localStorage.getItem('active_conv_id') || '';
  _wsSend({type: 'reading_start', book_id: bookId, chapter_index: chapterIndex, conv_id: convId});

  // MediaSession
  if ('mediaSession' in navigator) {
    navigator.mediaSession.metadata = new MediaMetadata({
      title: currentBookTitle || '朗读中',
      artist: 'Aion',
    });
    navigator.mediaSession.setActionHandler('pause', () => togglePlayerPause());
    navigator.mediaSession.setActionHandler('play', () => togglePlayerPause());
  }
}

function exitPlayerMode() {
  if (_playerActive) stopPlayerMode();
  document.getElementById('playerMode').style.display = 'none';
}

function stopPlayerMode() {
  _playerActive = false;
  _wsSend({type: 'reading_stop'});
  const audio = document.getElementById('playerAudio');
  audio.pause();
  audio.src = '';
}

function togglePlayerPause() {
  if (!_playerActive) return;
  _playerPaused = !_playerPaused;
  const btn = document.getElementById('playerPauseBtn');
  const audio = document.getElementById('playerAudio');
  if (_playerPaused) {
    btn.textContent = '▶';
    audio.pause();
    _wsSend({type: 'reading_pause'});
  } else {
    btn.textContent = '⏸';
    audio.play().catch(() => {});
    _wsSend({type: 'reading_resume'});
  }
}

function _wsSend(data) {
  if (_commonWs && _commonWs.readyState === 1) {
    _commonWs.send(JSON.stringify(data));
  }
}

function _handlePlayerWsMessage(msg) {
  if (!_playerActive && msg.type !== 'reading_error') return;

  switch (msg.type) {
    case 'reading_audio': {
      // Display text
      const html = msg.text
        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
        .replace(/「吐槽：([^」]*)」/g, '<span class="spit">$1</span>')
        .replace(/【([^】]*)】/g, '<span class="tone-tag">【$1】</span>');
      document.getElementById('playerText').innerHTML = `<div class="reading-text">${html}</div>`;

      // Progress
      document.getElementById('playerProgress').textContent =
        `第${msg.chapter_index + 1}章 · ${msg.chunk_index + 1}/${msg.total_chunks}段`;

      // Play audio
      const audio = document.getElementById('playerAudio');
      audio.src = msg.url;
      audio.onended = () => {
        _wsSend({type: 'reading_audio_ended'});
      };
      if (!_playerPaused) audio.play().catch(() => {});
      break;
    }
    case 'reading_chunk_start':
      // Optional: could show full script preview
      break;
    case 'reading_chapter_start':
      document.getElementById('playerChapterTitle').textContent = msg.chapter_title || `第${msg.chapter_index+1}章`;
      break;
    case 'reading_done':
      _playerActive = false;
      const reasons = {user_stop: '已停止', book_end: '全书读完', timeout: '已自动停止'};
      document.getElementById('playerText').innerHTML =
        `<span style="opacity:0.6;">${reasons[msg.reason] || '朗读结束'}</span>`;
      break;
    case 'reading_error':
      document.getElementById('playerText').innerHTML =
        `<span style="color:#f66;">${msg.message || '出错了'}</span>`;
      _playerActive = false;
      break;
  }
}
```

- [ ] **Step 5: 将 WS 消息路由到播放模式处理**

修改 `connectCommonWS` 调用处（约第 620 行），将 `handleWsReadingMessage` 替换为：

```javascript
connectCommonWS(msg => {
  _handlePlayerWsMessage(msg);
});
```

- [ ] **Step 6: 修改"开始朗读"按钮的触发方式**

找到"开始朗读"按钮相关的 `startReading()` 调用（旧代码已删），改为在阅读界面的适当位置添加入口。在 reader 顶栏的共读按钮旁添加朗读按钮：

将 `#readerTopbar` 内的 HTML：
```html
<button class="invite-btn" id="inviteBtn" onclick="inviteRead()">📝 共读</button>
```
后面添加：
```html
<button class="invite-btn" onclick="enterPlayerMode(currentBookId, currentChIdx)">🔊 朗读</button>
```

- [ ] **Step 7: 删除旧的 `showReadingBar` / `onReaderOpen` / `checkReadingStatus` 调用**

搜索并删除以下残留调用（如果有）：
- `showReadingBar(true/false)`
- `onReaderOpen(bookId)`
- `checkReadingStatus()`

这些函数已在 Step 4 中被删除，调用处也要清理。

- [ ] **Step 8: Commit**

```bash
git add aion-chat/static/reading.html
git commit -m "feat: 全屏播放模式 UI — WS 驱动、暂停/停止、MediaSession"
```

---

## Verification Checklist

After all tasks complete, verify:

- [ ] `python -c "import ast; ast.parse(open('reading.py').read()); ast.parse(open('tts.py').read()); ast.parse(open('main.py').read())"` 通过
- [ ] `routes/reading.py` 已删除
- [ ] 启动服务器无报错
- [ ] 打开 reading.html，进入某章，点"朗读"按钮进入全屏播放模式
- [ ] WS 收到 `reading_start` 后后端开始合成，前端收到 `reading_audio` 并播放
- [ ] 播完一句后前端发 `audio_ended`，后端推下一句（验证预取生效：几乎无延迟）
- [ ] 点暂停/继续正常工作
- [ ] 点停止 → 收到 `reading_done`
- [ ] 断开 WS → session 自动清理
- [ ] 15 分钟无交互 → 自动停止（可改短测试）
