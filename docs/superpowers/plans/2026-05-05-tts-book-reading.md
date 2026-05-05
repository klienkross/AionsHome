# TTS Book Reading Feature Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在现有 Book 和 TTS 系统上构建 AI 朗读交互功能，支持逐段朗读、自动继续、用户插话和结束总结。

**Architecture:** 新增 `reading.py` (ReadingSession 状态机) + `routes/reading.py` (SSE 控制流 + 用户消息端点)，复用现有 TTSStreamer / sentinel / ai_providers 不加改动。前端 `reading.html` 新增朗读控制栏和 TTS 播放。

**Tech Stack:** Python (FastAPI + asyncio + aiosqlite), MiMo-V2.5 TTS, Server-Sent Events, vanilla JS

---

## File Map

| 文件 | 职责 | 类型 |
|---|---|---|
| `aion-chat/reading.py` | ReadingSession 类: 状态机、定时器、朗读稿生成、TTS调度、结束总结 | Create |
| `aion-chat/routes/reading.py` | REST API: SSE 朗读流、用户插话、停止、状态查询 | Create |
| `aion-chat/main.py` | 挂载 reading router | Modify |
| `aion-chat/static/reading.html` | 朗读控制 UI、TTS 播放器、SSE 事件处理 | Modify |
| `aion-chat/tts.py` | 不改，直接复用 TTSStreamer | — |
| `aion-chat/sentinel.py` | 不改，复用 call_sentinel_text | — |
| `aion-chat/ai_providers.py` | 不改，复用 stream_ai / simple_ai_call | — |

---

### Task 1: ReadingSession 状态机核心

**Files:**
- Create: `aion-chat/reading.py`

- [ ] **Step 1: 创建 ReadingSession 类骨架**

```python
"""Book reading session: state machine, timer, annotation pipeline, TTS orchestration."""
import asyncio
import json
import logging
import time
from database import get_db
from sentinel import call_sentinel_text
from tts import TTSStreamer
from ws import manager as ws_manager

logger = logging.getLogger("reading")

# In-memory active sessions, keyed by book_id (one session per book at a time)
_sessions: dict[str, "ReadingSession"] = {}

def get_session(book_id: str) -> "ReadingSession | None":
    return _sessions.get(book_id)

def _row_dict(cursor, row):
    return {col[0]: row[i] for i, col in enumerate(cursor.description)}


class ReadingSession:
    STATE_IDLE = "idle"
    STATE_READING = "reading"
    STATE_WAITING = "waiting"
    STATE_CHATTING = "chatting"

    WAIT_SECONDS = 10
    SLEEP_CHECK_1 = 3   # "睡着了？"
    SLEEP_CHECK_2 = 6   # "还没醒？那我继续读了~"
    SLEEP_GOODNIGHT = 9  # "晚安~" → IDLE

    def __init__(self, book_id: str, chapter_index: int, conv_id: str):
        self.book_id = book_id
        self.chapter_index = chapter_index
        self.segment_index = 0
        self.conv_id = conv_id
        self.state = self.STATE_IDLE
        self.auto_counter = 0
        self.context_summary = ""
        self._conversation_log: list[dict] = []  # {role, content} pairs for end summary

        # SSE event queue — the SSE generator reads from this
        self._sse_queue: asyncio.Queue = asyncio.Queue()

        # User message notification — message endpoint sets this, SSE loop waits on it
        self._user_msg_event = asyncio.Event()
        self._pending_user_msg: str | None = None

        # Stop signal
        self._stop_event = asyncio.Event()

        _sessions[book_id] = self
```

- [ ] **Step 2: 实现分段数据获取**

```python
    async def _load_segment(self) -> dict | None:
        """Load current segment from DB. Returns None if chapter/book ended."""
        async with get_db() as db:
            db.row_factory = _row_dict
            # Get chapter
            cur = await db.execute(
                "SELECT * FROM book_chapters WHERE book_id = ? AND chapter_index = ?",
                (self.book_id, self.chapter_index),
            )
            chapter = await cur.fetchone()
            if not chapter:
                return None  # book ended

            segments_meta = json.loads(chapter.get("segments_meta", "[]"))
            if self.segment_index >= len(segments_meta):
                return None  # chapter ended

            meta = segments_meta[self.segment_index]
            # Load segment text from text_content using meta offsets
            text = chapter["text_content"]
            start = meta["start"]
            end = meta["end"] if meta["end"] <= len(text) else len(text)
            segment_text = text[start:end].strip()
            return {
                "text": segment_text,
                "chapter_title": chapter.get("title", ""),
                "total_segments": len(segments_meta),
            }
```

- [ ] **Step 3: 实现前情提要生成**

```python
    async def generate_context_summary(self, chapter_idx: int) -> str:
        """Call sentinel to summarize prior chapters + conversation history."""
        # Gather previous chapter summaries
        async with get_db() as db:
            db.row_factory = _row_dict
            cur = await db.execute(
                "SELECT chapter_index, summary FROM book_annotations WHERE book_id = ? AND chapter_index < ? ORDER BY chapter_index",
                (self.book_id, chapter_idx),
            )
            rows = await cur.fetchall()

        prev_summaries = "\n".join(
            f"第{r['chapter_index']+1}章: {r['summary']}" for r in rows if r.get("summary")
        )
        conv_summary = ""
        if self._conversation_log:
            recent = self._conversation_log[-20:]  # last 20 exchanges
            conv_summary = "朗读期间的对话:\n" + "\n".join(
                f"- {'用户' if m['role']=='user' else 'AI'}: {m['content'][:100]}" for m in recent
            )

        prompt = f"""你是一位认真的共读伙伴。请根据已有章节摘要和对话历史，为接下来要读的章节写一段 ≤200字 的前情提要，包含情节进展、人物状态和整体氛围。用自然的口吻，像在和朋友聊书。

已有章节摘要：
{prev_summaries if prev_summaries else "（这是第一章，无前情）"}

{conv_summary if conv_summary else ""}

只需输出前情提要，不要额外说明。"""

        result = await call_sentinel_text(prompt)
        return result.strip() if result else ""
```

- [ ] **Step 4: 实现朗读稿生成**

```python
    def _make_reading_system_prompt(self) -> str:
        book_title = self._book_title or "这本书"
        chapter_title = self._current_chapter_title or ""
        return f"""你正在为《{book_title}》做有声朗读，当前章节：{chapter_title}。

前情提要：{self.context_summary}

请将以下原文改写为适合口语朗读的版本：
- 保持原文意思，适当口语化但不过度发挥
- 在【】内标注详细语气（可用舞台描述风格，如【压低声音，带着一丝狡黠】）
- 段落末尾加一句简短的共读吐槽（≤25字），用「吐槽：xxx」格式
- 吐槽内也可以用【】标注语气，如「吐槽：【憋笑】这段也太中二了」
- TTS 会直接朗读全文包括吐槽，所以吐槽要自然融入"""

    async def generate_reading_script(self, segment_text: str) -> str:
        """Generate annotated reading script for one segment."""
        messages = [
            {"role": "system", "content": self._make_reading_system_prompt()},
            {"role": "user", "content": segment_text},
        ]
        result = await call_sentinel_text(messages)
        if not result:
            logger.warning(f"Reading script generation failed for {self.book_id}, using raw text")
            return segment_text
        return result.strip()
```

- [ ] **Step 5: 实现 TTS 合成**

```python
    async def _synthesize_and_collect(self, text: str, msg_id: str) -> list[dict]:
        """Feed text to TTSStreamer, collect audio URLs from sse_queue."""
        voice = ws_manager.get_tts_voice() or "冰糖"
        sse_queue: asyncio.Queue = asyncio.Queue()
        streamer = TTSStreamer(msg_id, voice, ws_manager, sse_queue=sse_queue)

        # Feed entire script at once (already complete, no streaming needed)
        streamer.feed(text)
        await streamer.flush()
        await asyncio.sleep(0)  # let pending queue callbacks settle

        # Collect tts_chunk events from the queue
        chunks = []
        while not sse_queue.empty():
            try:
                event = sse_queue.get_nowait()
                data = json.loads(event)
                if data.get("type") == "tts_chunk":
                    chunks.append(data["data"])
            except Exception:
                break

        return chunks
```

- [ ] **Step 6: 实现定时器等待逻辑**

```python
    async def wait_for_input_or_timeout(self) -> str:
        """
        Wait for user input or timeout.
        Returns: "timeout" | "stop" | "user_message"
        Sets self._pending_user_msg if user sent a message.
        """
        self.state = self.STATE_WAITING
        self._user_msg_event.clear()

        try:
            await asyncio.wait_for(self._user_msg_event.wait(), timeout=self.WAIT_SECONDS)
            # User sent a message
            if self._stop_event.is_set():
                return "stop"
            return "user_message"
        except asyncio.TimeoutError:
            # Timeout — auto-continue
            if self._stop_event.is_set():
                return "stop"
            return "timeout"
```

- [ ] **Step 7: 实现休眠检查消息生成**

```python
    def _get_sleep_check_message(self, counter: int) -> str | None:
        """Return sleep check message for given counter, or None if should continue."""
        if counter == self.SLEEP_CHECK_1:
            return "【轻声】嗯？睡着了吗？"
        elif counter == self.SLEEP_CHECK_2:
            return "【略带无奈的笑意】还没醒吗？那我先继续读了~"
        elif counter >= self.SLEEP_GOODNIGHT:
            return "【温柔】看来真睡着了，晚安~"
        return None
```

- [ ] **Step 8: 实现对话处理**

```python
    async def handle_chat(self, user_msg: str) -> str:
        """Handle user interjection during reading. Returns AI reply text."""
        self.state = self.STATE_CHATTING
        self.auto_counter = 0  # reset
        self._conversation_log.append({"role": "user", "content": user_msg})

        # Build lightweight context
        messages = [
            {"role": "system", "content": f"""你正在和用户共读《{self._book_title or '这本书'}》。
当前章节：{self._current_chapter_title or ''}
前情提要：{self.context_summary}

你是用户的共读伙伴，用自然、亲切的口吻和用户聊书。回复简洁（≤150字），像朋友间聊天。"""},
        ]
        # Last 5 exchanges
        for m in self._conversation_log[-10:]:
            messages.append({"role": m["role"], "content": m["content"]})
        # Add current message if not already in log
        if not messages[-1]["content"] == user_msg:
            messages.append({"role": "user", "content": user_msg})

        reply = await call_sentinel_text(messages)
        reply_text = reply.strip() if reply else "嗯，我在听~"

        self._conversation_log.append({"role": "assistant", "content": reply_text})
        self.state = self.STATE_WAITING
        return reply_text
```

- [ ] **Step 9: 实现结束总结**

```python
    async def write_end_summary(self, reason: str):
        """Write a summary message to the conversation so the bot remembers the reading."""
        if not self._conversation_log and reason == "goodnight":
            # No conversation at all — write a minimal note
            summary_text = f"读《{self._book_title or '书'}》读到第{self.chapter_index+1}章，你睡着了，我先道晚安了。下次继续~"
        else:
            conv_text = "\n".join(
                f"{'用户' if m['role']=='user' else 'AI'}: {m['content'][:150]}"
                for m in self._conversation_log[-30:]
            )
            prompt = f"""用户刚读完《{self._book_title or '一本书'}》第{self.chapter_index+1}章后结束朗读（原因: {reason}）。

朗读期间的对话记录：
{conv_text if conv_text else "（无对话）"}

请写一条 ≤300字 的总结消息，像刚读完书在跟朋友随口聊：今天读了什么、读到哪了、1-2句感想或印象深刻的地方。用自然亲切的口吻。"""

            result = await call_sentinel_text(prompt)
            summary_text = result.strip() if result else f"今天和你一起读了《{self._book_title or '书'}》第{self.chapter_index+1}章，下次继续~"

        # Write to messages table
        msg_id = f"reading_summary_{self.book_id}_{int(time.time())}"
        async with get_db() as db:
            await db.execute(
                "INSERT INTO messages (id, conversation_id, role, content, created_at) VALUES (?, ?, ?, ?, ?)",
                (msg_id, self.conv_id, "assistant", summary_text, time.time()),
            )
            await db.commit()
```

- [ ] **Step 10: 实现主朗读循环**

```python
    async def run_loop(self):
        """Main reading loop, called from SSE generator."""
        self.state = self.STATE_READING

        # Phase 0: context summary for this chapter
        self.context_summary = await self.generate_context_summary(self.chapter_index)

        # Load book metadata
        async with get_db() as db:
            db.row_factory = _row_dict
            cur = await db.execute("SELECT title FROM books WHERE book_id = ?", (self.book_id,))
            row = await cur.fetchone()
            self._book_title = row["title"] if row else None

        while not self._stop_event.is_set():
            # Load segment
            seg = await self._load_segment()
            if seg is None:
                # Chapter ended — check next chapter
                await self._sse_queue.put(json.dumps({
                    "type": "chapter_end",
                    "data": {"chapter_index": self.chapter_index}
                }))
                break  # caller handles chapter transition

            self._current_chapter_title = seg["chapter_title"]
            self.state = self.STATE_READING

            # Generate reading script
            script = await self.generate_reading_script(seg["text"])

            # Notify frontend of segment text (for display)
            await self._sse_queue.put(json.dumps({
                "type": "segment",
                "data": {
                    "chapter_index": self.chapter_index,
                    "segment_index": self.segment_index,
                    "total_segments": seg["total_segments"],
                    "text": script,
                }
            }))

            # Synthesize TTS
            msg_id = f"read_{self.book_id}_c{self.chapter_index}_s{self.segment_index}"
            await self._synthesize_and_collect(script, msg_id)

            # Signal TTS done
            await self._sse_queue.put(json.dumps({
                "type": "tts_done",
                "data": {"msg_id": msg_id}
            }))

            # Increment segment
            self.segment_index += 1

            # Wait for user input or timeout
            result = await self.wait_for_input_or_timeout()

            if result == "stop":
                break
            elif result == "user_message":
                reply = await self.handle_chat(self._pending_user_msg)
                # Synthesize chat reply
                chat_msg_id = f"read_chat_{self.book_id}_{int(time.time())}"
                await self._synthesize_and_collect(reply, chat_msg_id)
                await self._sse_queue.put(json.dumps({
                    "type": "chat_reply",
                    "data": {"text": reply, "msg_id": chat_msg_id}
                }))
                # Back to WAITING — the loop continues
            elif result == "timeout":
                self.auto_counter += 1
                sleep_msg = self._get_sleep_check_message(self.auto_counter)
                if sleep_msg:
                    # Synthesize sleep check
                    sleep_msg_id = f"read_sleep_{self.book_id}_{int(time.time())}"
                    await self._synthesize_and_collect(sleep_msg, sleep_msg_id)
                    await self._sse_queue.put(json.dumps({
                        "type": "sleep_check",
                        "data": {"message": sleep_msg, "counter": self.auto_counter, "msg_id": sleep_msg_id}
                    }))
                if self.auto_counter >= self.SLEEP_GOODNIGHT:
                    await self.write_end_summary("goodnight")
                    await self._sse_queue.put(json.dumps({
                        "type": "done",
                        "data": {"reason": "goodnight"}
                    }))
                    break
                # Otherwise continue to next segment

        # Cleanup
        if self._stop_event.is_set():
            await self.write_end_summary("user_stop")
            await self._sse_queue.put(json.dumps({
                "type": "done",
                "data": {"reason": "user_stop"}
            }))
        else:
            # Chapter ended normally
            await self._sse_queue.put(json.dumps({
                "type": "done",
                "data": {"reason": "chapter_end"}
            }))

        _sessions.pop(self.book_id, None)
        self.state = self.STATE_IDLE
```

- [ ] **Step 11: 实现 stop 和 enqueue 方法**

```python
    def stop(self):
        self._stop_event.set()
        self._user_msg_event.set()  # wake up wait loop

    def enqueue_user_message(self, content: str):
        self._pending_user_msg = content
        self._user_msg_event.set()
```

- [ ] **Step 12: Commit**

```bash
git add aion-chat/reading.py
git commit -m "feat: ReadingSession 状态机 — 朗读调度核心"

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
```


---

### Task 2: 朗读 REST API

**Files:**
- Create: `aion-chat/routes/reading.py`

- [ ] **Step 1: 创建路由文件和 SSE 端点**

```python
"""Reading session REST API: SSE control stream, user messages, stop, status."""
import json
import asyncio
import logging
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from reading import ReadingSession, get_session, _sessions

router = APIRouter(prefix="/api/books", tags=["reading"])
logger = logging.getLogger("reading.routes")


class ReadingMessageBody(BaseModel):
    content: str
    conv_id: str = ""
```

- [ ] **Step 2: 实现 GET /{book_id}/read/start SSE 端点**

> EventSource 只支持 GET，参数通过 query string 传递。

```python
@router.get("/{book_id}/read/start")
async def start_reading(book_id: str, chapter_index: int = 0, conv_id: str = ""):
    """Start reading session. Returns SSE stream that stays alive until reading ends."""
    existing = get_session(book_id)
    if existing:
        raise HTTPException(400, "该书已有活跃的朗读会话，请先停止")

    session = ReadingSession(book_id, chapter_index, conv_id)

    async def event_stream():
        try:
            # Run the reading loop in a background task
            loop_task = asyncio.create_task(session.run_loop())

            # Pull events from the SSE queue and yield them
            while not loop_task.done() or not session._sse_queue.empty():
                try:
                    event = await asyncio.wait_for(session._sse_queue.get(), timeout=0.5)
                    yield f"data: {event}\n\n"
                except asyncio.TimeoutError:
                    continue

            # Get any exception from the loop task
            await loop_task

        except asyncio.CancelledError:
            session.stop()
        except Exception as e:
            logger.error(f"Reading session error: {e}")
            yield f"data: {json.dumps({'type': 'error', 'data': {'message': str(e)}})}\n\n"
        finally:
            _sessions.pop(book_id, None)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
```

- [ ] **Step 3: 实现 POST /{book_id}/read/message**

```python
@router.post("/{book_id}/read/message")
async def reading_message(book_id: str, body: ReadingMessageBody):
    """User sends a message during reading."""
    session = get_session(book_id)
    if not session:
        raise HTTPException(404, "没有活跃的朗读会话")
    session.enqueue_user_message(body.content)
    return {"status": "ok"}
```

- [ ] **Step 4: 实现 POST /{book_id}/read/stop**

```python
@router.post("/{book_id}/read/stop")
async def stop_reading(book_id: str):
    """Stop the active reading session."""
    session = get_session(book_id)
    if not session:
        raise HTTPException(404, "没有活跃的朗读会话")
    session.stop()
    return {"status": "stopped"}
```

- [ ] **Step 5: 实现 GET /{book_id}/read/status**

```python
@router.get("/{book_id}/read/status")
async def reading_status(book_id: str):
    """Get current reading session status."""
    session = get_session(book_id)
    if not session:
        return {"active": False}
    return {
        "active": True,
        "book_id": session.book_id,
        "chapter_index": session.chapter_index,
        "segment_index": session.segment_index,
        "state": session.state,
        "auto_counter": session.auto_counter,
    }
```

- [ ] **Step 6: Commit**

```bash
git add aion-chat/routes/reading.py
git commit -m "feat: 朗读 SSE 控制流与用户交互 API"

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
```


---

### Task 3: 挂载路由到 main.py

**Files:**
- Modify: `aion-chat/main.py`

- [ ] **Step 1: 添加 import 和 router**

在 `from routes import webhooks as webhooks_routes` 之后添加:

```python
from routes import reading as reading_routes
```

在 `app.include_router(webhooks_routes.router)` 之后添加:

```python
app.include_router(reading_routes.router)
```

- [ ] **Step 2: Commit**

```bash
git add aion-chat/main.py
git commit -m "feat: 挂载朗读路由"

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
```


---

### Task 4: 前端朗读控制 UI (reading.html)

**Files:**
- Modify: `aion-chat/static/reading.html`

- [ ] **Step 1: 在 reader 视图底部添加朗读控制栏 HTML**

在 `#readerView` 内部底部导航之前插入:

```html
<!-- Reading control bar -->
<div id="readingBar" style="display:none;position:fixed;bottom:60px;left:0;right:0;z-index:100;
    background:var(--bg);border-top:1px solid var(--border);padding:12px 16px;
    display:flex;align-items:center;gap:12px;">
  <button id="btnReadStart" onclick="startReading()"
    style="padding:8px 20px;border-radius:20px;border:none;background:var(--accent);color:#fff;font-size:14px;">
    开始朗读
  </button>
  <button id="btnReadStop" onclick="stopReading()" disabled
    style="padding:8px 20px;border-radius:20px;border:1px solid var(--border);background:transparent;color:var(--text);font-size:14px;">
    停止
  </button>
  <span id="readingStatus" style="font-size:13px;color:var(--text-secondary);"></span>
  <span id="readingTimer" style="font-size:12px;color:var(--text-muted);margin-left:auto;"></span>
  <!-- Audio player for TTS -->
  <audio id="ttsAudio" style="display:none" onended="onAudioEnded()"></audio>
</div>
```

- [ ] **Step 2: 添加 JavaScript 朗读控制逻辑**

在 `</body>` 之前，`<script>` 标签内（或新建 script 块）添加:

```javascript
// ── Reading Controls ──

let readingActive = false;
let readingBookId = null;
let readingEventSource = null;
let ttsQueue = [];  // Queue of audio URLs to play sequentially
let ttsPlaying = false;

function showReadingBar(show) {
  const bar = document.getElementById('readingBar');
  bar.style.display = show ? 'flex' : 'none';
}

// Called when user opens reader view
function onReaderOpen(bookId) {
  readingBookId = bookId;
  showReadingBar(true);
  checkReadingStatus();
}

async function checkReadingStatus() {
  try {
    const r = await fetch(`/api/books/${readingBookId}/read/status`);
    const data = await r.json();
    if (data.active) {
      readingActive = true;
      document.getElementById('btnReadStart').disabled = true;
      document.getElementById('btnReadStop').disabled = false;
      document.getElementById('readingStatus').textContent =
        `朗读中 — 第${data.chapter_index+1}章 第${data.segment_index+1}段`;
    }
  } catch(e) {}
}

async function startReading() {
  if (!readingBookId) return;
  const btn = document.getElementById('btnReadStart');
  btn.disabled = true;
  document.getElementById('btnReadStop').disabled = false;
  document.getElementById('readingStatus').textContent = '准备中...';

  const convId = localStorage.getItem('active_conv_id') || '';

  readingEventSource = new EventSource(
    `/api/books/${readingBookId}/read/start?chapter_index=0&conv_id=${encodeURIComponent(convId)}`
  );

  readingEventSource.onmessage = (e) => {
    try {
      const event = JSON.parse(e.data);
      handleReadingEvent(event);
    } catch(err) {
      console.error('Reading SSE parse error:', err);
    }
  };

  readingEventSource.onerror = () => {
    console.error('Reading SSE connection error');
    readingActive = false;
    document.getElementById('btnReadStart').disabled = false;
    document.getElementById('btnReadStop').disabled = true;
    document.getElementById('readingStatus').textContent = '连接中断';
  };

  readingActive = true;
}

function handleReadingEvent(event) {
  const status = document.getElementById('readingStatus');
  switch(event.type) {
    case 'segment':
      status.textContent = `朗读中 — 第${event.data.segment_index+1}/${event.data.total_segments}段`;
      // Display the reading script in the content area
      displayReadingText(event.data.text);
      break;

    case 'tts_done':
      // Frontend plays audio from WS tts_chunk events
      // (or from stored URLs if we use them)
      break;

    case 'sleep_check':
      status.textContent = event.data.message;
      break;

    case 'chat_reply':
      status.textContent = '已回复';
      break;

    case 'done':
      status.textContent = event.data.reason === 'goodnight' ? '晚安~' :
                           event.data.reason === 'user_stop' ? '已停止' : '本章结束';
      onReadingEnd();
      break;

    case 'chapter_end':
      status.textContent = '本章读完';
      break;

    case 'error':
      console.error('Reading error:', event.data.message);
      status.textContent = '出错了';
      onReadingEnd();
      break;
  }
}

function onReadingEnd() {
  readingActive = false;
  if (readingEventSource) {
    readingEventSource.close();
    readingEventSource = null;
  }
  document.getElementById('btnReadStart').disabled = false;
  document.getElementById('btnReadStop').disabled = true;
}

async function stopReading() {
  if (!readingBookId) return;
  await fetch(`/api/books/${readingBookId}/read/stop`, { method: 'POST' });
  onReadingEnd();
  document.getElementById('readingStatus').textContent = '已停止';
}

function displayReadingText(text) {
  // Split text from 吐槽 for styling
  let html = text
    .replace(/「吐槽：([^」]*)」/g, '<span class="spit">吐槽：$1</span>')
    .replace(/【([^】]*)】/g, '<span class="tone-tag">【$1】</span>');
  // Show in the existing content area or a reading overlay
  const container = document.getElementById('readingContent') || document.getElementById('readerContent');
  if (container) {
    container.innerHTML = `<div class="reading-script">${html}</div>`;
  }
}

// ── TTS Audio Player ──

// Listen for WS tts_chunk events (from common WS connection)
// Assume common.js exposes onTtsChunk callback or we hook into ws messages
function enqueueTtsAudio(url) {
  ttsQueue.push(url);
  if (!ttsPlaying) playNextTts();
}

function playNextTts() {
  if (ttsQueue.length === 0) {
    ttsPlaying = false;
    return;
  }
  ttsPlaying = true;
  const url = ttsQueue.shift();
  const audio = document.getElementById('ttsAudio');
  audio.src = url;
  audio.play().catch(e => console.error('TTS play error:', e));
}

function onAudioEnded() {
  playNextTts();
}
```

- [ ] **Step 3: 集成 WS tts_chunk 事件到朗读播放队列**

在现有 WS 消息处理中（`connectCommonWS` 的回调），添加对 `tts_chunk` 的处理:

```javascript
// In the WS message handler, add:
if (msg.type === 'tts_chunk' && readingActive) {
  enqueueTtsAudio(msg.data.url);
}
```

- [ ] **Step 4: 朗读时发送消息的入口**

在现有聊天输入框（`#readerReply` 内的 input）的发送逻辑中，添加朗读模式判断:

```javascript
async function sendReaderMessage(content) {
  if (readingActive) {
    // Send via reading message endpoint
    await fetch(`/api/books/${readingBookId}/read/message`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ content, conv_id: localStorage.getItem('active_conv_id') || '' })
    });
  } else {
    // Existing send logic via /api/conversations/{conv_id}/send
  }
}
```

- [ ] **Step 5: 添加 CSS 样式**

在 `<style>` 块中添加:

```css
.reading-script { line-height: 1.8; font-size: 16px; padding: 16px; }
.spit {
  color: var(--accent);
  font-style: italic;
  background: var(--accent-bg-subtle, rgba(100,100,255,0.1));
  border-radius: 4px;
  padding: 2px 6px;
}
.tone-tag {
  color: var(--text-muted);
  font-size: 0.85em;
  opacity: 0.7;
}
#readingBar {
  backdrop-filter: blur(10px);
}
```

- [ ] **Step 6: 在进入 reader 视图时触发 showReadingBar**

在 `openReader()` 或等效函数末尾添加 `onReaderOpen(bookId)` 调用。

- [ ] **Step 7: Commit**

```bash
git add aion-chat/static/reading.html
git commit -m "feat: 朗读控制 UI — 开始/停止、SSE 事件处理、TTS 播放队列"

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
```


---

## Verification Checklist

After all tasks complete, verify:

- [ ] `GET /api/books/{book_id}/read/start` 返回 SSE 流
- [ ] SSE 流中包含 `segment`、`tts_done`、`done` 事件
- [ ] 用户发消息到 `/read/message` 能触发 `chat_reply` 事件
- [ ] 10s 无消息自动继续下一段
- [ ] 连续 3 次超时触发 "睡着了吗？"
- [ ] 连续 9 次超时触发 "晚安~" 并停止
- [ ] 停止后 messages 表中有总结记录
- [ ] 朗读期间 TTS 音频正常播放
- [ ] 朗读结束后 `readingBar` 恢复初始状态
