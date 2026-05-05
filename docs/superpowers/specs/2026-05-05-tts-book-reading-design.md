# TTS Book Reading Feature Design

## Overview

在现有 Book 和 TTS 系统之上，添加 AI 朗读交互功能。用户打开一本书后，AI 逐段生成带语气标注的朗读稿并 TTS 播放；每段读完后等待用户插话，10s 无响应自动继续，连续多次无响应逐步升级提示直至自动晚安退出。

## Architecture

```
ReadingSession (新模块 aion-chat/reading.py)
  ├── 持有: book_id, current_chapter, current_segment, timer, context_summary
  ├── 调用: sentinel (前情提要), ai_providers.stream_ai (朗读稿生成)
  └── 复用: TTSStreamer (音频合成), ConnectionManager (WS推送)

REST API (aion-chat/routes/reading.py 或扩展 routes/book.py)
  ├── POST /api/books/{book_id}/read/start
  ├── POST /api/books/{book_id}/read/stop
  ├── POST /api/books/{book_id}/read/continue
  └── GET  /api/books/{book_id}/read/status
```

不复用现有 chat 管道。ReadingSession 独立管理朗读状态。

## State Machine

```
IDLE --[start]--> READING --[segment done + TTS flushed]--> WAITING
                     ↑                                          │
                     │                          ┌───────────────┤
                     │                          │               │
                     │                    user sends msg   10s timeout
                     │                          │               │
                     │                      CHATTING        auto_counter++
                     │                          │               │
                     │                      [AI replies]        │
                     │                          │               │
                     └──────────────────────────┘               │
                                                                 │
                     counter=3 → "睡着了？" (continue reading)   │
                     counter=6 → "还没醒？那我继续读了~"          │
                     counter=9 → "晚安~" → IDLE                  │
                     
                     user sends any msg → counter = 0
```

**定时器逻辑**: 单一 10s 定时器，每次超时 `auto_counter` 自增。用户发任意消息、或进入任何非 WAITING 状态时归零。

## Annotation Pipeline (朗读稿生成)

每次进入新章节时:

```
1. Sentinel 总结
   - 输入: 前 N 章摘要 (从 book_annotations 读取已有 summary) + 朗读期间的对话摘要
   - 输出: ≤200字 前情提要 (情节 + 人物 + 氛围 + 用户关注的焦点)

2. 每段朗读稿生成 (进入 READING 状态时)
   - Prompt 输入:
     - 书名 / 章节名
     - 前情提要
     - 当前段原文 (book_chapters.paragraphs / text_content 按 segment 切片)
     - 指令: "口语化改写，在【】内标注详细语气（可用舞台描述风格），段末加 ≤25字个人吐槽可自带【】语气，用「吐槽：xxx」格式"
   - 输出: 带标注的朗读稿
   - token 估算: 系统指令 ~200 + 前情 ~200 + 原文 ~800 + 输出 ~300 ≈ 1500 tokens/段
```

**语气标记约定**:
- `【语气描述】` — 放在句首或句中，标注朗读语调。不限于简单情绪词，支持复杂的舞台描述，如【压低声音，带着一丝狡黠】【故作镇定，但微微颤抖】【像在读一封迟到了十年的信】
- `「吐槽：xxx」` — 段末吐槽，≤25字，TTS 朗读。吐槽内也可用【】标注语气，如「吐槽：【憋笑】这段也太中二了」
- MiMo TTS 对丰富风格指令的响应良好，不设硬性格式约束

**吐槽风格约束**（通过哨兵上下文传递）:
- 哨兵总结包含用户最近的说话风格和关注的梗
- 每段朗读稿生成时，前情提要保证吐槽的语气一致性

## Token Control Strategy

朗读期间的对话 (CHATTING 状态):

- 上下文窗口: 当前书的 书名+章节名 + 前情提要 + 最近 5 轮对话 + 当前段摘要
- 不加载完整聊天历史
- 不触发 RAG/记忆召回
- fast_mode 风格: 仅调用 sentinel_text，跳过重试和复杂后处理

朗读稿生成 (READING 状态):

- 仅当前段原文 + 前情提要 + 系���指令
- 不加载任何对话历史

## Session End & Summary

朗读结束（晚安退出 / 全书读完 / 用户停止）时，AI 向主对话写入一条总结消息:

```
调用 Sentinel 生成总结:
  输入:
    - 书名 / 本次读的章节范围
    - 朗读期间的对话摘要（已由 ReadingSession 维护）
    - 关键吐槽和用户反应
  输出:
    - 写入主对话的消息（≤300字），包含:
      - 今天读了什么、读到哪了
      - 1-2 句个人感想或印象深刻的地方
      - 自然的口吻，像刚读完书在跟朋友随口聊
```

- 写入位置: `POST /api/conversations/{conv_id}/send` 或直接写 DB（`messages` 表）
- 目的: 朗读经历不被"遗忘"，下次聊天时 bot 知道这段上下文
- 不额外消耗朗读 token 预算，总结在退出时一次性生成

## TTS Integration

复用现有 `TTSStreamer`:
- 每个朗读段创建新的 TTSStreamer 实例
- `feed()` 送入完整朗读稿 (一次性 feed 而非流式)
- `flush()` 等合成完成
- 生成的 WAV 通过现有 WS 事件推送到前端 (`tts_chunk` + `tts_done`)
- 吐槽部分正常朗读

## API Design

### POST /api/books/{book_id}/read/start
```json
// Request
{ "chapter_index": 0 }
// voice 从当前 WS 连接的 TTS 状态读取，与聊天声线保持一致

// Response (SSE stream, 复用现有 SSE 格式)
// event: reading_start → { chapter, total_segments, summary }
// event: segment → { segment_index, text, tts_url, has_spit }
// event: waiting → { auto_counter }
// event: auto_continue → {}
// event: sleep_check → { message, counter }
// event: done → { reason: "book_end" | "user_stop" | "goodnight" }
```

### POST /api/books/{book_id}/read/stop
```json
// Request: {}
// Response: { status: "stopped" }
```

### POST /api/books/{book_id}/read/continue
手动跳过等待，立即读下一段。

### GET /api/books/{book_id}/read/status
```json
// Response: { book_id, chapter, segment, state, auto_counter }
```

## Frontend (reading.html)

现有 `reading.html` 已有书籍展示和进度跟踪。需要新增：

- **朗读控制栏**: 开始/暂停/继续按钮，语音选择，播放进度
- **TTS 播放器**: 监听 WS `tts_chunk` 事件，播放音频
- **"聆听中"提示**: 在 WAITING 状态显示剩余等待秒数（前端可选，后端不依赖）
- **消息输入框**: 始终可用，发送消息即进入 CHATTING
- **吐槽样式**: 「吐槽：xxx」用斜体/气泡展示
- **朗读稿显示**: 原文区和朗读稿区并列或切换

## File Changes Summary

| 文件 | 变更 |
|---|---|
| `aion-chat/reading.py` | **新建** — ReadingSession 类 |
| `aion-chat/routes/reading.py` | **新建** — 朗读 REST API |
| `aion-chat/routes/book.py` | 小改 — 注册 reading router |
| `aion-chat/main.py` | 小改 — 挂载 reading router |
| `aion-chat/static/reading.html` | 改 — 朗读控制 UI + TTS 播放 |
| `aion-chat/tts.py` | 不改 — 直接复用 TTSStreamer |
| `aion-chat/ai_providers.py` | 不改 — 复用 stream_ai / sentinel |
| `aion-chat/sentinel.py` | 不改 — 复用 call_sentinel_text |

## Edge Cases

- **段过长 (>2000字)**: 按句号切分，分多次 TTS feed，但只生成一次朗读稿
- **网络中断**: WS 断开后 ReadingSession 继续运行，重连后推送当前状态
- **快速切换书**: 停止当前朗读再开始新书
- **章节末段**: 读完自动问"继续下一章吗？"，进入长等待
- **全书读完**: 生成简短总结，结束朗读
