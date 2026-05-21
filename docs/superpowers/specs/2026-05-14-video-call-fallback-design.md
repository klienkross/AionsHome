# 视频通话降级策略设计

## 概述

当 AI 模型不支持视频输入时，自动降级为 STT + 截帧图片 / 纯文字模式，保持视频通话体验不中断。所有预处理（STT、图片描述）统一走 sentinel 哨兵体系。

## 三级降级

| 等级 | 模型能力 | 采集内容 | 发给模型的消息 |
|------|----------|----------|---------------|
| `video` | 支持视频 | 完整视频+音频 | 视频文件（现有流程） |
| `image` | 支持图片 | 音频 + 场景变化截帧 | STT 文字 + 图片序列（带时间戳） |
| `text` | 纯文字 | 音频 + 场景变化截帧 | STT 文字 + sentinel VL 图片描述（纯文字） |

## 1. 模型能力声明

### MODELS 字典扩展

在 `config.py` 的 `MODELS` 字典中，每个模型新增 `"media"` 字段：

```python
MODELS = {
    "gemini-3-flash":    {"provider": "gemini",      "model": "gemini-3-flash-preview",  "media": "video"},
    "gemini-3.1-pro":    {"provider": "gemini",      "model": "gemini-3.1-pro-preview",  "media": "video"},
    "硅基GLM-5.1":       {"provider": "siliconflow", "model": "Pro/zai-org/GLM-5.1",     "media": "image"},
    "claude-opus4.6":    {"provider": "aipro",       "model": "claude-opus-4-6",         "media": "image"},
    "ds":                {"provider": "custom",      "model": "deepseek-v4-pro",         "media": "text"},
    # 未标注 media 的模型默认为 "text"
}
```

### settings.json 覆盖

```json
{
  "video_call_media_override": null
}
```

- `null`：自动根据当前模型的 `media` 字段决定
- `"video"` / `"image"` / `"text"`：强制使用指定模式

### 能力解析

在 `config.py` 新增：

```python
def get_video_call_media_level() -> str:
    override = SETTINGS.get("video_call_media_override")
    if override in ("video", "image", "text"):
        return override
    model_name = SETTINGS.get("default_model") or DEFAULT_MODEL
    model_cfg = MODELS.get(model_name, {})
    return model_cfg.get("media", "text")
```

## 2. sentinel 基础设施扩展：STT 收拢

### 现状：三处重复的 ASR 调用

| 位置 | 函数 | 同步/异步 | 场景 |
|------|------|-----------|------|
| `voice.py:126` | `VoiceWakeup._asr()` | 同步 httpx | 桌面麦克风唤醒/通话 |
| `routes/voice.py:71` | `remote_asr()` | 异步 httpx | 手机端远程 ASR |
| `routes/voice.py:99` | `transcribe_voice_message()` | 异步 httpx | 视频通话/语音消息 ASR |

三处代码几乎一致（URL、model、headers、emoji 清洗），只是同步/异步区别。

### 收拢到 sentinel.py

在 `sentinel.py` 新增两个函数，统一管理 ASR_URL、ASR_MODEL、emoji 清洗：

```python
ASR_URL = "https://api.siliconflow.cn/v1/audio/transcriptions"
ASR_MODEL = "FunAudioLLM/SenseVoiceSmall"

async def transcribe_audio(audio_bytes: bytes, *, filename: str = "audio.wav",
                           mime: str = "audio/wav", timeout: int = 30) -> str | None:
    """异步 STT：routes 和视频通话降级用。"""

def transcribe_audio_sync(audio_bytes: bytes, *, filename: str = "audio.wav",
                          mime: str = "audio/wav", timeout: int = 15) -> str | None:
    """同步 STT：VoiceWakeup 线程用（独立线程，不在 event loop）。"""
```

### 迁移

| 原调用点 | 改为 |
|----------|------|
| `voice.py` `_asr()` 方法体 | `sentinel.transcribe_audio_sync(wav_bytes)` |
| `routes/voice.py` `remote_asr()` | `sentinel.transcribe_audio(content)` |
| `routes/voice.py` `transcribe_voice_message()` | `sentinel.transcribe_audio(content, filename=..., mime=...)` |
| 新增视频通话降级 | `sentinel.transcribe_audio(audio_bytes)` |

原 route 函数保留（HTTP 端点不变），但函数体缩减为读取文件 → 调 sentinel → 返回结果。

### 现有 VL 能力复用

`describe_image_b64()` 已存在（qwen3-vl-flash），text 模式下直接调用，将截帧图片转为文字描述。

### sentinel 在降级中的角色

```
video 模式: sentinel 不参与
image 模式: sentinel.transcribe_audio() → STT 文字
text  模式: sentinel.transcribe_audio() → STT 文字
             sentinel.describe_image_b64() × N → 每帧画面描述
```

## 3. 前端录制策略

### 模式通知

后端通过 WebSocket `video_call_ring` 消息携带 `media_level` 字段：

```json
{"type": "video_call_ring", "data": {"conv_id": "...", "media_level": "image"}}
```

用户主动发起通话时，`/api/video-call-init-sys-msg` 响应中也返回 `media_level`。

### video 模式

现有流程不变：MediaRecorder 录视频+音频 → 上传 MP4/WebM。

### image 模式

按住录制时同时启动两条管线：

1. **音频管线**：MediaRecorder 录纯音频（webm/wav）
2. **截帧管线**：每 ~500ms 从 `<video>` 元素用 Canvas 抓帧，做场景变化检测

松手后打包上传到 `/api/video-call-frames`。

### text 模式

前端行为与 image 模式相同（录音频 + 截帧），摄像头画面正常显示。区别仅在后端：图片不直接发给模型，而是先经 sentinel VL 描述转为文字。

### 场景变化检测算法

前端 Canvas 实现：

1. 每 500ms 抓一帧，缩小到 160×120
2. 与上一帧逐像素 RGB 差值求和，算出差异百分比
3. 超过阈值（15%）才保留
4. 第一帧始终保留
5. 单次录制最多 5 帧，超出时均匀采样

Android Native Bridge 场景：从 CameraBridge 推来的 base64 帧走同样的 Canvas 差异逻辑。

### UI 提示

通话界面右上角显示降级模式标签：

- `video`：不显示
- `image`：显示 "📷 图片模式"
- `text`：显示 "🎤 语音模式"

## 4. 后端消息构建

### 新增路由

```
POST /api/video-call-frames
Body: multipart/form-data
  - audio: 音频文件
  - frames_meta: JSON [{"ts_ms": 0}, {"ts_ms": 2300}, ...]
  - frame_0, frame_1, ...: JPEG 图片
  - conv_id: 对话 ID
```

### 处理流程

```
收到 /api/video-call-frames
  │
  ├─ sentinel.transcribe_audio(audio) → stt_text
  │
  ├─ 查 media_level
  │   ├─ "image" → 保留原始图片
  │   └─ "text"  → 对每帧调用 sentinel.describe_image_b64() → 文字描述
  │
  └─ 构建消息 → 发给 AI 模型
```

### 消息格式

**image 模式**（多模态：文字 + 图片）：

```python
content = [
    {"type": "text", "text": "[用户说]：\"你看这个东西\"\n\n[同时画面]："},
    {"type": "text", "text": "- 0.0s:"},
    {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,..."}},
    {"type": "text", "text": "- 2.3s:"},
    {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,..."}},
]
```

**text 模式**（纯文字）：

```python
content = (
    "[用户在视频通话中说]：\"你看这个东西\"\n\n"
    "[画面 0.0s]：用户手持一本红色封面的书，在书桌前\n"
    "[画面 2.3s]：用户翻开书，展示内页的插图"
)
```

### 系统提示词适配

视频通话 system prompt 根据 media_level 追加说明：

- `video`：不变
- `image`：「用户通过摄像头发来了带时间戳的画面截图，请结合语音内容和画面理解用户意图」
- `text`：「当前为语音+画面描述模式，画面描述由系统自动生成，可能不完全准确。如用户提到指代词请结合画面描述理解」

## 5. 错误处理

- **STT 失败**：不中断通话，发送 `"[语音识别失败]"` 占位，图片/描述仍正常发送
- **VL 描述失败**（text 模式）：跳过该帧描述，其余帧正常
- **全部截帧无变化**：只发 STT 文字，不附图片/描述

## 6. 普通语音消息复用

sentinel.transcribe_audio() 作为通用 STT 入口，未来普通聊天中发送语音消息时也可复用：

```
用户录制语音 → 上传音频 → sentinel.transcribe_audio() → 文字 → 作为用户消息发送
```

这是后续扩展方向，本次设计先在视频通话降级中实现并验证 STT sentinel 接口。

## 影响范围

| 文件 | 改动 |
|------|------|
| `config.py` | MODELS 加 media 字段；新增 `get_video_call_media_level()` |
| `sentinel.py` | 新增 `transcribe_audio()` + `transcribe_audio_sync()`；ASR 常量集中 |
| `voice.py` | `_asr()` 方法体替换为调用 `sentinel.transcribe_audio_sync()` |
| `routes/voice.py` | `remote_asr()` / `transcribe_voice_message()` 替换为调用 `sentinel.transcribe_audio()` |
| `routes/chat.py` | 新增 `/api/video-call-frames`；WS 消息加 media_level；消息构建分支 |
| `static/video-call.js` | 录制模式分支；场景变化检测；UI 模式标签 |
| `data/settings.json` | 新增 `video_call_media_override` 字段 |
