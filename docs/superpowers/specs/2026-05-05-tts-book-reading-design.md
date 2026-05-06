# TTS Book Reading Feature Design (v2 Rewrite)

## Overview

在现有 Book 系统上构建"睡前听书"功能。用户打开一本书进入播放模式，AI 逐段生成带语气标注的朗读稿并 TTS 逐句播放；用户可暂停/继续，长时间无交互自动停止并写入对话总结。

全程通过 WebSocket 单通道通信，不使用 SSE。不复用 TTSStreamer，直接调用 TTS API 逐句合成+预取。

## Architecture

```
reading.html (全屏播放模式 UI)
    ↕ WS (单通道，双向)
ReadingSession (aion-chat/reading.py)
    → call_sentinel_text (朗读稿生成)
    → httpx 直接调 MiMo TTS API (逐句合成，预取下一句)
```

不新增 REST 端点。所有朗读控制通过 WS 消息收发。

## Data Flow

```
1. 用户点"开始朗读" → WS: {type: "reading_start", book_id, chapter_index}
2. 后端创建 ReadingSession → 加载章节 → 按段落边界切成 ~800 字 chunk
3. chunk[0] → AI 生成朗读稿 → 按句号切成句子 → 合成句[0]，预取句[1]
4. 句[0] ready → WS 推: {type: "reading_audio", url, seq, text, chapter_index, chunk_index, total_chunks}
5. 前端播放音频 → 播完发: {type: "reading_audio_ended"}
6. 后端推句[1]（已 ready），开始预取句[2]
7. 一个 chunk 的所有句子读完 → 取 chunk[1] → 生成朗读稿 → 继续
8. 章节读完 → 自动切下一章（重新生成 context_summary）
9. 用户点暂停 → WS: {type: "reading_pause"} → 后端暂停推送
10. 用户点继续 → WS: {type: "reading_resume"} → 后端继续
11. 用户点停止 → WS: {type: "reading_stop"} → 后端停止 + 写 summary
12. 无交互超过 15 分钟 → 后端自动停止 + 写 summary → WS: {type: "reading_done", reason}
```

## WS Message Protocol

### Client → Server

| type | fields | description |
|------|--------|-------------|
| `reading_start` | `book_id`, `chapter_index`, `conv_id` | 开始朗读 |
| `reading_pause` | — | 暂停 |
| `reading_resume` | — | 继续 |
| `reading_stop` | — | 停止 |
| `reading_audio_ended` | — | 前端音频播完，请求下一句 |

### Server → Client

| type | fields | description |
|------|--------|-------------|
| `reading_audio` | `url`, `seq`, `text`, `chapter_index`, `chunk_index`, `total_chunks` | 一句音频 ready |
| `reading_chunk_start` | `chapter_index`, `chunk_index`, `total_chunks`, `script` | 新 chunk 的朗读稿（用于文本展示） |
| `reading_chapter_start` | `chapter_index`, `chapter_title` | 进入新章节 |
| `reading_done` | `reason`: "user_stop" \| "book_end" \| "timeout" | 朗读结束 |
| `reading_error` | `message` | 出错 |

## ReadingSession

```python
class ReadingSession:
    def __init__(self, book_id, chapter_index, conv_id, ws):
        self.book_id = book_id
        self.chapter_index = chapter_index
        self.conv_id = conv_id
        self._ws = ws  # 发起朗读的那个 WS 连接
        self._paused = False
        self._stopped = False
        self._last_interaction = time.time()
        self._audio_ended_event = asyncio.Event()

    # 核心循环
    async def run(self):
        while not self._stopped:
            chunks = self._split_chapter(chapter_index)
            for chunk in chunks:
                script = await self._generate_script(chunk)
                sentences = self._split_sentences(script)
                await self._read_sentences(sentences)
                if self._stopped: break
            # 章节完 → 下一章
            if not await self._advance_chapter():
                break  # 全书完

    # TTS 预取
    async def _read_sentences(self, sentences):
        next_audio = asyncio.create_task(self._tts_one(sentences[0]))
        for i, sentence in enumerate(sentences):
            audio_url = await next_audio
            if i + 1 < len(sentences):
                next_audio = asyncio.create_task(self._tts_one(sentences[i + 1]))
            await self._push_audio(audio_url, sentence, i)
            await self._wait_audio_ended_or_stop()
            if self._stopped: break

    # 等前端播完或超时停止
    async def _wait_audio_ended_or_stop(self):
        while not self._stopped:
            if self._paused:
                await asyncio.sleep(0.5)
                continue
            self._audio_ended_event.clear()
            try:
                await asyncio.wait_for(self._audio_ended_event.wait(), timeout=60)
                self._last_interaction = time.time()
                return
            except asyncio.TimeoutError:
                if time.time() - self._last_interaction > 900:  # 15 min
                    self._stopped = True
```

## Chunk Splitting

按段落边界切分，每个 chunk ≤800 字。超长单段落按句号二次切分。

```python
def _split_chapter(self, chapter_index) -> list[str]:
    paragraphs = load_paragraphs(self.book_id, chapter_index)
    chunks, buf, count = [], [], 0
    for p in paragraphs:
        if count + len(p) > 800 and buf:
            chunks.append("\n".join(buf))
            buf, count = [], 0
        if len(p) > 800:
            chunks.extend(split_long_paragraph(p))
        else:
            buf.append(p)
            count += len(p)
    if buf:
        chunks.append("\n".join(buf))
    return chunks
```

## Sentence Splitting (for TTS)

朗读稿生成后，按句号/问号/感叹号切分为句子。每句 ≤200 字送 TTS API。吐槽 `「吐槽：xxx」` 作为独立句子。

## TTS Integration

不复用 TTSStreamer。直接在 ReadingSession 里调 MiMo API：

```python
async def _tts_one(self, text: str) -> str | None:
    """合成一句，返回 audio URL。失败返回 None（跳过该句）。"""
    style_hint, clean = extract_hints_and_clean(text)
    # 调 MiMo API，保存 wav，返回 URL
```

从 tts.py 提取 `_extract_hints_and_clean` 为公共函数供 reading.py 使用。

## Annotation Pipeline (朗读稿生成)

每个 chunk 调一次 AI，prompt：

```
你正在为《{book_title}》做有声朗读，当前章节：{chapter_title}。
前情提要：{context_summary}

请直接输出原文，仅做以下少量添加：
- 在需要语气变化的位置插入【】语气标注
- 段落末尾加一句简短的共读吐槽（≤25字），用「吐槽：xxx」格式

注意：尽量保持原文不动。语气标注和吐槽之外，原文措辞不变。
```

## Context Summary

每章开始时生成一次 ≤200 字前情提要（调 sentinel），缓存在 session 内。同一 session 内不重复生成。

## Timeout & Auto-stop

- `_last_interaction` 在每次收到 `audio_ended` / `pause` / `resume` 时刷新
- 每次准备推送下一句前检查：超过 15 分钟无交互 → 停止
- 停止时写 summary 到 messages 表（如果有 conv_id）

## End Summary

朗读结束时调 sentinel 生成 ≤300 字总结，写入 messages 表：

- 今天读了什么、读到哪了
- 1-2 句感想
- 自然口吻

仅当 `conv_id` 非空时写入。

## Frontend: 播放模式 (reading.html)

全屏播放界面，从阅读页面点击"朗读"按钮进入：

- **居中大文本区**：显示当前正在朗读的句子（带语气标注高亮 + 吐槽样式）
- **底部控制栏**：暂停/继续按钮（大按钮，方便触摸）、停止按钮、进度（第 N/M 段）
- **顶部**：书名 + 章节名 + 返回按钮
- **背景**：深色/暗色，适合睡前
- **锁屏友好**：使用 MediaSession API 注册 metadata，支持锁屏界面显示控制按钮

### 播放逻辑

```javascript
ws.onmessage = (e) => {
    const msg = JSON.parse(e.data);
    if (msg.type === "reading_audio") {
        displayText(msg.text);
        playAudio(msg.url, () => {
            ws.send(JSON.stringify({type: "reading_audio_ended"}));
        });
    }
    // ...
};
```

## File Changes

| 文件 | 变更 |
|------|------|
| `aion-chat/reading.py` | **重写** — ReadingSession：WS 驱动、逐句合成+预取、超时停止 |
| `aion-chat/routes/reading.py` | **删除** — 不再需要 REST 端点 |
| `aion-chat/main.py` | **改** — WS handler 转发 reading_* 消息；移除 reading router 挂载 |
| `aion-chat/tts.py` | **小改** — 提取 `extract_hints_and_clean` 为模块级公共函数 |
| `aion-chat/static/reading.html` | **改** — 新增全屏播放模式 UI + WS 交互 |

## Edge Cases

- **前端意外断开**：WS disconnect → session 停止（不写 summary，因为不确定用户状态）
- **TTS 合成失败**：跳过该句，继续下一句，前端不会卡住
- **AI 生成失败**：用原文直接 TTS（无标注版）
- **章节无内容**：跳过，尝试下一章
- **重复点击开始**：同一 book_id 只允许一个 session
- **超长句子（>200 字无标点）**：强制在 200 字处切断
