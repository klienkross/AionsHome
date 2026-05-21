# 视频通话降级策略 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 当 AI 模型不支持视频输入时，自动降级为 STT+截帧图片 或 纯文字模式，并将三处重复的 ASR 调用收拢到 sentinel.py。

**Architecture:** 在 MODELS 字典中声明每个模型的媒体能力（video/image/text），settings.json 提供覆盖开关。前端根据 media_level 选择录制策略（完整视频 / 音频+截帧 / 纯音频+截帧），后端新增 `/api/video-call-frames` 路由处理降级数据。所有 STT 调用统一收拢到 sentinel.py。

**Tech Stack:** Python FastAPI, JavaScript (vanilla), Canvas API, 硅基流动 ASR API, DashScope qwen3-vl-flash

---

## File Structure

| 文件 | 职责 | 操作 |
|------|------|------|
| `aion-chat/config.py` | MODELS 加 media 字段；新增 `get_video_call_media_level()` | Modify |
| `aion-chat/sentinel.py` | 新增 `transcribe_audio()` + `transcribe_audio_sync()`；ASR 常量集中 | Modify |
| `aion-chat/voice.py` | `_asr()` 方法体替换为调用 sentinel | Modify |
| `aion-chat/routes/voice.py` | `remote_asr()` / `transcribe_voice_message()` 替换为调用 sentinel | Modify |
| `aion-chat/routes/chat.py` | 新增 `/api/video-call-frames`；WS 消息加 media_level；消息构建；system prompt 适配 | Modify |
| `aion-chat/static/video-call.js` | 三模式录制分支；场景变化检测；UI 模式标签 | Modify |

---

### Task 1: sentinel.py — 新增 STT 函数

**Files:**
- Modify: `aion-chat/sentinel.py` (在文件末尾，`get_embedding` 函数之后追加)

- [ ] **Step 1: 在 sentinel.py 顶部添加 re import 和 ASR 常量**

在 `import asyncio, json, time, struct` 行改为：
```python
import asyncio, json, re, time, struct
```

在 `EMBEDDING_DIMS = 1024` 行之后添加：
```python

# ── ASR（硅基流动 STT） ──────────────────────────
ASR_URL = "https://api.siliconflow.cn/v1/audio/transcriptions"
ASR_MODEL = "FunAudioLLM/SenseVoiceSmall"
_EMOJI_RE = re.compile(
    "[\U0001F600-\U0001F64F\U0001F300-\U0001F5FF\U0001F680-\U0001F6FF"
    "\U0001F1E0-\U0001F1FF\U00002702-\U000027B0\U000024C2-\U000024FF"
    "\U0001F170-\U0001F251"
    "\U0001F900-\U0001F9FF\U0001FA00-\U0001FA6F\U0001FA70-\U0001FAFF"
    "\U00002600-\U000026FF\U0000FE00-\U0000FE0F\U0000200D]+"
)
```

- [ ] **Step 2: 在 sentinel.py 末尾追加异步和同步 STT 函数**

在 `get_embedding` 函数之后追加：

```python


# ── 对外：语音转写（硅基流动 ASR） ─────────────
async def transcribe_audio(
    audio_bytes: bytes,
    *,
    filename: str = "audio.wav",
    mime: str = "audio/wav",
    timeout: int = 30,
) -> str | None:
    """异步 STT：音频字节 → 文字。供 routes 和视频通话降级使用。"""
    key = get_key("siliconflow")
    if not key:
        return None
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                ASR_URL,
                headers={"Authorization": f"Bearer {key}"},
                files={"file": (filename, audio_bytes, mime)},
                data={"model": ASR_MODEL, "language": "zh"},
            )
            resp.raise_for_status()
            raw = resp.json().get("text", "").strip()
            return _EMOJI_RE.sub("", raw).strip() or None
    except Exception:
        return None


def transcribe_audio_sync(
    audio_bytes: bytes,
    *,
    filename: str = "audio.wav",
    mime: str = "audio/wav",
    timeout: int = 15,
) -> str | None:
    """同步 STT：供 VoiceWakeup 线程使用（独立线程，不在 event loop）。"""
    key = get_key("siliconflow")
    if not key:
        return None
    try:
        resp = httpx.post(
            ASR_URL,
            headers={"Authorization": f"Bearer {key}"},
            files={"file": (filename, audio_bytes, mime)},
            data={"model": ASR_MODEL, "language": "zh"},
            timeout=timeout,
        )
        resp.raise_for_status()
        raw = resp.json().get("text", "").strip()
        return _EMOJI_RE.sub("", raw).strip() or None
    except Exception:
        return None
```

- [ ] **Step 3: 验证 sentinel.py 语法正确**

Run: `python -c "import sentinel"` (在 aion-chat 目录下)
Expected: 无报错

- [ ] **Step 4: Commit**

```
git add aion-chat/sentinel.py
git commit -m "feat: sentinel 新增 transcribe_audio / transcribe_audio_sync STT 函数"
```

---

### Task 2: 迁移 voice.py 和 routes/voice.py 的 ASR 调用

**Files:**
- Modify: `aion-chat/voice.py:126-145`
- Modify: `aion-chat/routes/voice.py:1-127`

- [ ] **Step 1: 修改 voice.py 的 `_asr` 方法**

将 `voice.py` 第 126-145 行的 `_asr` 方法替换为：

```python
    def _asr(self, audio) -> str:
        """调硅基流动 ASR"""
        from sentinel import transcribe_audio_sync
        wav = self._to_wav(audio)
        return transcribe_audio_sync(wav) or ""
```

同时删除 voice.py 顶部的 ASR 常量（第 26-27 行）：
```python
ASR_URL = "https://api.siliconflow.cn/v1/audio/transcriptions"
ASR_MODEL = "FunAudioLLM/SenseVoiceSmall"
```

- [ ] **Step 2: 修改 routes/voice.py**

将整个文件替换为以下内容（保留所有原有端点，但 ASR 逻辑改为调用 sentinel）：

```python
"""
语音唤醒路由：开关控制 + 状态查询 + AI说话通知 + 远程ASR
"""

from fastapi import APIRouter, UploadFile, File
from pydantic import BaseModel

from voice import voice
from sentinel import transcribe_audio

router = APIRouter()


class VoiceToggle(BaseModel):
    enabled: bool
    wake_word: str = "老公"


class AISpeakingNotify(BaseModel):
    speaking: bool


@router.get("/api/voice/status")
async def voice_status():
    return {
        "enabled": voice.enabled,
        "in_call": voice.in_call,
        "ai_speaking": voice.ai_speaking,
        "wake_word": voice.wake_word,
    }


@router.post("/api/voice/toggle")
async def voice_toggle(body: VoiceToggle):
    if body.enabled:
        voice.start(body.wake_word)
    else:
        voice.stop()
    return {"ok": True, "enabled": voice.enabled}


@router.post("/api/voice/ai-speaking")
async def voice_ai_speaking(body: AISpeakingNotify):
    """前端通知：AI TTS 播放状态"""
    voice.notify_ai_speaking(body.speaking)
    return {"ok": True}


@router.post("/api/voice/cam-check-start")
async def voice_cam_check_start():
    """前端通知：AI 触发了 CAM_CHECK"""
    voice.notify_cam_check_start()
    return {"ok": True}


@router.post("/api/voice/remote-asr")
async def remote_asr(file: UploadFile = File(...)):
    """远程 ASR：接收手机端录音，调硅基流动 ASR 返回文本"""
    content = await file.read()
    print(f"[RemoteASR] Received {len(content)} bytes, filename={file.filename}")
    text = await transcribe_audio(content)
    if text:
        print(f"[RemoteASR] Result: '{text}'")
        return {"text": text}
    return {"text": "", "error": "transcription failed"}


@router.post("/api/voice/transcribe")
async def transcribe_voice_message(file: UploadFile = File(...)):
    """语音消息转写：接收上传的音频文件，调硅基流动 ASR 返回文本"""
    content = await file.read()
    mime = file.content_type or "audio/webm"
    ext = file.filename.rsplit(".", 1)[-1] if file.filename and "." in file.filename else "webm"
    print(f"[VoiceTranscribe] Received {len(content)} bytes, mime={mime}, ext={ext}")
    text = await transcribe_audio(content, filename=f"voice.{ext}", mime=mime)
    if text:
        print(f"[VoiceTranscribe] Result: '{text}'")
        return {"text": text}
    return {"text": "", "error": "transcription failed"}
```

- [ ] **Step 3: 验证导入正确**

Run: `python -c "from routes.voice import router"` (在 aion-chat 目录下)
Expected: 无报错

- [ ] **Step 4: Commit**

```
git add aion-chat/voice.py aion-chat/routes/voice.py
git commit -m "refactor: voice ASR 调用收拢到 sentinel.transcribe_audio"
```

---

### Task 3: config.py — MODELS media 字段 + get_video_call_media_level()

**Files:**
- Modify: `aion-chat/config.py:135-162`

- [ ] **Step 1: 给 MODELS 每个模型添加 media 字段**

将 `config.py` 第 135-160 行的 MODELS 字典替换为：

```python
MODELS = {
    "硅基GLM-5.1":      {"provider": "siliconflow", "model": "Pro/zai-org/GLM-5.1", "media": "image"},
    "硅基GLM-5":        {"provider": "siliconflow", "model": "Pro/zai-org/GLM-5", "media": "image"},
    "硅基Kimi-K2.5":    {"provider": "siliconflow", "model": "Pro/moonshotai/Kimi-K2.5", "media": "text"},
    "硅基Kimi2.6":      {"provider": "siliconflow", "model": "Pro/moonshotai/Kimi-K2.6", "media": "text"},
    "gemini-3.1-flash-lite": {"provider": "gemini", "model": "gemini-3.1-flash-lite-preview", "media": "video"},
    "gemini-2.5-pro":        {"provider": "gemini", "model": "gemini-2.5-pro", "media": "video"},
    "gemini-3-flash":        {"provider": "gemini", "model": "gemini-3-flash-preview", "media": "video"},
    "gemini-3.1-pro":        {"provider": "gemini", "model": "gemini-3.1-pro-preview", "media": "video"},
    "claude-sonnet-4-6":  {"provider": "aipro", "model": "claude-sonnet-4-6", "media": "image"},
    "claude-opus4.6":    {"provider": "aipro", "model": "claude-opus-4-6", "media": "image"},
    "claude-opus4.6T":    {"provider": "aipro", "model": "claude-opus-4-6-thinking", "media": "image"},
    "哈基米opus4.7": {"provider": "aipro", "model": "claude-opus-4-7", "media": "image"},
    "哈基米opus4.6":  {"provider": "aipro", "model": "claude-opus-4-6", "media": "image"},
    "哈基米gpt-5.5":    {"provider": "aipro", "model": "gemini-3.1-pro-high", "media": "video"},
    "哈基米3.1pro":     {"provider": "aipro", "model": "gemini-3.1-pro-high", "media": "video"},
    "哈基米2.5pro":    {"provider": "aipro", "model": "gemini-2.5-pro", "media": "video"},
    "CLI-2.5pro":       {"provider": "gemini_cli", "model": "gemini-2.5-pro", "media": "video"},
    "CLI-3.1pro":       {"provider": "gemini_cli", "model": "gemini-3.1-pro-preview", "media": "video"},
    "CLI-2.5flash":     {"provider": "gemini_cli", "model": "gemini-2.5-flash", "media": "video"},
    "Codex":            {"provider": "codex_cli",  "model": "", "media": "text"},
    "CLI-Claude":       {"provider": "claude_cli", "model": "", "media": "text"},

    # 自定义第三方 OpenAI 兼容端点示例（删掉注释#即可启用，填好 base_url 与 key_name）
    "ds":  {"provider": "custom", "model": "deepseek-v4-pro", "base_url": "https://api.deepseek.com/v1", "key_name": "ds_key", "media": "text"},
}
```

注意：media 标注依据——
- Gemini 系列支持视频 → `"video"`
- 哈基米走 aipro 代理转发 Gemini → `"video"`
- Claude 系列支持图片不支持视频 → `"image"`
- 硅基 GLM 支持图片 → `"image"`
- 硅基 Kimi 不确定 → 保守给 `"text"`
- DeepSeek / Codex / CLI-Claude → `"text"`

用户可以后续按实际能力调整。

- [ ] **Step 2: 在 DEFAULT_MODEL 行之后添加 get_video_call_media_level()**

在 `config.py` 的 `DEFAULT_MODEL = ...` 行之后添加：

```python


def get_video_call_media_level() -> str:
    """返回当前视频通话的媒体降级等级：video / image / text"""
    override = SETTINGS.get("video_call_media_override")
    if override in ("video", "image", "text"):
        return override
    model_name = SETTINGS.get("default_model") or DEFAULT_MODEL
    model_cfg = MODELS.get(model_name, {})
    return model_cfg.get("media", "text")
```

- [ ] **Step 3: 验证**

Run: `python -c "from config import get_video_call_media_level; print(get_video_call_media_level())"` (在 aion-chat 目录下)
Expected: 输出 "video" / "image" / "text" 之一（取决于当前 default_model）

- [ ] **Step 4: Commit**

```
git add aion-chat/config.py
git commit -m "feat: MODELS 添加 media 能力声明，新增 get_video_call_media_level()"
```

---

### Task 4: routes/chat.py — WS 消息携带 media_level + 新增 /api/video-call-frames

**Files:**
- Modify: `aion-chat/routes/chat.py`

- [ ] **Step 1: 在 chat.py 顶部导入区添加 config 导入**

在 chat.py 中找到现有的 `from config import ...` 行，追加 `get_video_call_media_level`。

例如，如果现有行是：
```python
from config import get_key, MODELS, UPLOADS_DIR, ...
```
改为：
```python
from config import get_key, MODELS, UPLOADS_DIR, ..., get_video_call_media_level
```

同时确保有 sentinel 导入（在文件顶部导入区）：
```python
from sentinel import transcribe_audio, describe_image_b64
```

- [ ] **Step 2: 修改 _delayed_video_call 中的 WS 消息携带 media_level**

找到 `_delayed_video_call` 函数（约第 1858 行），在函数体开头获取 media_level 并注入 vc_data：

将：
```python
async def _delayed_video_call(vc_data: dict, delay: float = 3.0):
    """等待用户阅读完回复后，定向推送视频来电到最后发消息的客户端"""
    await asyncio.sleep(delay)
```

改为：
```python
async def _delayed_video_call(vc_data: dict, delay: float = 3.0):
    """等待用户阅读完回复后，定向推送视频来电到最后发消息的客户端"""
    await asyncio.sleep(delay)
    vc_data["media_level"] = get_video_call_media_level()
```

- [ ] **Step 3: 修改 video_call_init_sys_msg 端点返回 media_level**

找到 `/api/video-call-init-sys-msg` 路由（约第 1881 行），将：

```python
@router.post("/api/video-call-init-sys-msg")
async def video_call_init_sys_msg(body: VideoCallInitSysMsg):
    await _video_call_outgoing_sys_msg(body.conv_id)
    return {"ok": True}
```

改为：

```python
@router.post("/api/video-call-init-sys-msg")
async def video_call_init_sys_msg(body: VideoCallInitSysMsg):
    await _video_call_outgoing_sys_msg(body.conv_id)
    return {"ok": True, "media_level": get_video_call_media_level()}
```

- [ ] **Step 4: 在 video_call_init_sys_msg 之后新增 /api/video-call-frames 路由**

在 `video_call_init_sys_msg` 函数之后、下一个路由之前插入：

```python

@router.post("/api/video-call-frames")
async def video_call_frames(
    conv_id: str = Form(...),
    audio: UploadFile = File(...),
    frames_meta: str = Form("[]"),
):
    """image/text 降级模式：接收音频+截帧，STT转写后构建消息发给模型"""
    import base64
    media_level = get_video_call_media_level()

    # 解析帧元数据
    try:
        meta_list = json.loads(frames_meta)
    except Exception:
        meta_list = []

    # 读取音频 → STT
    audio_bytes = await audio.read()
    stt_text = await transcribe_audio(audio_bytes) or "[语音识别失败]"

    # 读取帧图片
    frame_files = []
    i = 0
    while True:
        field_name = f"frame_{i}"
        # FastAPI 不支持动态文件字段名，改用 request 直接解析
        break
    # 实际实现：通过 request.form() 获取帧数据
    # 此处需要改用 Starlette Request 对象
    return {"ok": True, "stt_text": stt_text, "media_level": media_level}
```

**注意**：上面的路由是占位骨架。动态数量的文件字段在 FastAPI 中需要用 `Request` 对象处理。完整实现如下：

```python
from starlette.requests import Request as StarletteRequest

@router.post("/api/video-call-frames")
async def video_call_frames(request: StarletteRequest):
    """image/text 降级模式：接收音频+截帧，STT转写+消息构建"""
    import base64
    media_level = get_video_call_media_level()
    form = await request.form()

    conv_id = form.get("conv_id", "")
    frames_meta_raw = form.get("frames_meta", "[]")
    try:
        meta_list = json.loads(frames_meta_raw)
    except Exception:
        meta_list = []

    # 读取音频 → STT
    audio_file = form.get("audio")
    stt_text = "[语音识别失败]"
    if audio_file:
        audio_bytes = await audio_file.read()
        stt_text = await transcribe_audio(audio_bytes) or "[语音识别失败]"

    # 读取帧图片
    frames = []
    for i, meta in enumerate(meta_list):
        frame_file = form.get(f"frame_{i}")
        if frame_file:
            frame_bytes = await frame_file.read()
            b64 = base64.b64encode(frame_bytes).decode()
            frames.append({"b64": b64, "ts_ms": meta.get("ts_ms", 0)})

    # 构建消息内容
    if media_level == "text" and frames:
        # text 模式：用 sentinel VL 把图片转文字描述
        descriptions = []
        for f in frames:
            desc = await describe_image_b64(f["b64"])
            ts_sec = f["ts_ms"] / 1000
            if desc:
                descriptions.append(f"[画面 {ts_sec:.1f}s]：{desc}")
            # VL 失败则跳过该帧
        scene_text = "\n".join(descriptions)
        content = f"[用户在视频通话中说]：\"{stt_text}\""
        if scene_text:
            content += f"\n\n{scene_text}"
    elif media_level == "image" and frames:
        # image 模式：构建多模态消息（文字 + 图片序列）
        content = [{"type": "text", "text": f"[用户说]：\"{stt_text}\"\n\n[同时画面]："}]
        for f in frames:
            ts_sec = f["ts_ms"] / 1000
            content.append({"type": "text", "text": f"- {ts_sec:.1f}s:"})
            content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{f['b64']}"}})
    else:
        # 无帧或 video 模式不应走这个端点
        content = f"[用户在视频通话中说]：\"{stt_text}\""

    # 检查挂断关键词
    hangup_keywords = ["再见", "拜拜", "挂断", "结束通话", "挂了"]
    is_hangup = any(kw in stt_text for kw in hangup_keywords)

    # 构建附件记录（用于数据库持久化，保持与 video_clip 格式一致）
    att = {
        "type": "video_call_frames",
        "transcript": stt_text,
        "frame_count": len(frames),
        "media_level": media_level,
    }

    return {
        "ok": True,
        "content": content,
        "stt_text": stt_text,
        "is_hangup": is_hangup,
        "attachment": att,
        "media_level": media_level,
    }
```

需要在文件顶部添加 `from starlette.requests import Request as StarletteRequest` 导入（如果尚未存在）。

- [ ] **Step 5: 验证语法**

Run: `python -c "from routes.chat import router"` (在 aion-chat 目录下)
Expected: 无报错

- [ ] **Step 6: Commit**

```
git add aion-chat/routes/chat.py
git commit -m "feat: WS 消息携带 media_level，新增 /api/video-call-frames 路由"
```

---

### Task 5: routes/chat.py — system prompt 适配

**Files:**
- Modify: `aion-chat/routes/chat.py`

- [ ] **Step 1: 在视频通话能力声明处添加降级模式说明**

在 chat.py 中搜索 `video_call_enabled`（出现 3 次，约第 716、1233、2614 行），每处都是类似的代码：

```python
    if SETTINGS.get("video_call_enabled", True):
        abilities.append(f"[视频电话] — 当你想和{user_name}进行视频聊天看看对方当前状态时可以用该指令发起视频通话。")
```

在**每处**后面追加以下代码块：

```python
        _media_lvl = get_video_call_media_level()
        if _media_lvl == "image":
            abilities.append("⚠ 当前视频通话为图片模式：用户发来的内容是语音转写文字+带时间戳的画面截图，请结合语音内容和画面理解用户意图。")
        elif _media_lvl == "text":
            abilities.append("⚠ 当前视频通话为语音模式：用户发来的是语音转写文字+系统自动生成的画面描述（可能不完全准确）。如用户提到「这个」「那个」等指代词，请结合画面描述理解。")
```

- [ ] **Step 2: 验证语法**

Run: `python -c "from routes.chat import router"` (在 aion-chat 目录下)
Expected: 无报错

- [ ] **Step 3: Commit**

```
git add aion-chat/routes/chat.py
git commit -m "feat: system prompt 根据 media_level 追加视频通话降级说明"
```

---

### Task 6: video-call.js — 场景变化检测 + 三模式录制

**Files:**
- Modify: `aion-chat/static/video-call.js`

- [ ] **Step 1: 添加 media_level 状态变量和场景检测相关变量**

在 video-call.js 顶部状态声明区（约第 9-43 行），在 `const MAX_RECORD_SECONDS = 60;` 之后添加：

```javascript

  // ── 降级模式 ──
  let _mediaLevel = 'video';     // 当前媒体降级等级：video / image / text
  let _capturedFrames = [];      // image/text 模式下截取的帧 [{blob, ts_ms}]
  let _lastFrameData = null;     // 上一帧的 ImageData（用于场景变化检测）
  let _frameCaptureTimer = null; // 截帧定时器
  const MAX_FRAMES = 5;
  const FRAME_INTERVAL_MS = 500;
  const SCENE_CHANGE_THRESHOLD = 0.15;
  const THUMB_W = 160;
  const THUMB_H = 120;
```

- [ ] **Step 2: 添加场景变化检测函数**

在 `_getAiName()` 函数之后、`_createElement` 函数之前添加：

```javascript

  // ── 场景变化检测 ──
  function _captureFrame(videoEl) {
    const canvas = document.createElement('canvas');
    canvas.width = THUMB_W;
    canvas.height = THUMB_H;
    const ctx = canvas.getContext('2d');
    ctx.drawImage(videoEl, 0, 0, THUMB_W, THUMB_H);
    const imageData = ctx.getImageData(0, 0, THUMB_W, THUMB_H);

    // 与上一帧比较
    if (_lastFrameData) {
      let diff = 0;
      const len = imageData.data.length;
      for (let i = 0; i < len; i += 4) {
        diff += Math.abs(imageData.data[i] - _lastFrameData.data[i]);
        diff += Math.abs(imageData.data[i+1] - _lastFrameData.data[i+1]);
        diff += Math.abs(imageData.data[i+2] - _lastFrameData.data[i+2]);
      }
      const maxDiff = (len / 4) * 3 * 255;
      const ratio = diff / maxDiff;
      if (ratio < SCENE_CHANGE_THRESHOLD) {
        return; // 变化不够大，跳过
      }
    }

    _lastFrameData = imageData;

    // 导出为 JPEG blob
    canvas.toBlob((blob) => {
      if (blob && _capturedFrames.length < MAX_FRAMES) {
        _capturedFrames.push({
          blob,
          ts_ms: Date.now() - _recordStartTime,
        });
      }
    }, 'image/jpeg', 0.7);
  }

  function _startFrameCapture() {
    _capturedFrames = [];
    _lastFrameData = null;
    const videoEl = document.getElementById('vcUserVideo')
                 || document.querySelector('#videoCallOverlay video');
    if (!videoEl) return;

    // 立即捕获第一帧
    _captureFrame(videoEl);

    _frameCaptureTimer = setInterval(() => {
      if (_videoRecording) _captureFrame(videoEl);
    }, FRAME_INTERVAL_MS);
  }

  function _stopFrameCapture() {
    if (_frameCaptureTimer) {
      clearInterval(_frameCaptureTimer);
      _frameCaptureTimer = null;
    }
  }

  // 帧数超限时均匀采样
  function _sampleFrames(frames, max) {
    if (frames.length <= max) return frames;
    const step = (frames.length - 1) / (max - 1);
    const sampled = [];
    for (let i = 0; i < max; i++) {
      sampled.push(frames[Math.round(i * step)]);
    }
    return sampled;
  }
```

- [ ] **Step 3: 修改 _startRecord 函数，根据 _mediaLevel 分支**

找到 `_startRecord` 函数（约第 545 行），找到浏览器 MediaRecorder 分支（`} else {` 之后，约第 586 行）。

**对于 image/text 模式**，不录视频，只录音频+截帧。将浏览器分支改为：

```javascript
    } else {
      // ── 浏览器录制 ──
      try {
        if (_mediaLevel === 'video') {
          // video 模式：完整视频+音频（现有流程）
          const tracks = [];
          if (_cameraStream) tracks.push(..._cameraStream.getVideoTracks());
          if (_voiceStream) tracks.push(..._voiceStream.getAudioTracks());

          if (tracks.length === 0) {
            console.error('[VideoCall] No tracks for recording');
            _cancelRecord();
            return;
          }

          const combined = new MediaStream(tracks);
          _videoRecorder = new MediaRecorder(combined, {
            mimeType: MediaRecorder.isTypeSupported('video/webm;codecs=vp9,opus')
              ? 'video/webm;codecs=vp9,opus'
              : 'video/webm'
          });
          _videoRecorder.ondataavailable = (e) => { if (e.data.size > 0) _videoChunks.push(e.data); };
          _videoRecorder.start(500);

          // 同时录纯音频（用于 ASR）
          if (_voiceStream && _voiceStream.getAudioTracks().length > 0) {
            const audioStream = new MediaStream(_voiceStream.getAudioTracks());
            _audioForASR = new MediaRecorder(audioStream, {
              mimeType: MediaRecorder.isTypeSupported('audio/webm;codecs=opus')
                ? 'audio/webm;codecs=opus' : 'audio/webm'
            });
            _audioForASR.ondataavailable = (e) => { if (e.data.size > 0) _audioChunks.push(e.data); };
            _audioForASR.start(500);
          }
        } else {
          // image/text 模式：只录音频 + 截帧
          if (_voiceStream && _voiceStream.getAudioTracks().length > 0) {
            const audioStream = new MediaStream(_voiceStream.getAudioTracks());
            _audioForASR = new MediaRecorder(audioStream, {
              mimeType: MediaRecorder.isTypeSupported('audio/webm;codecs=opus')
                ? 'audio/webm;codecs=opus' : 'audio/webm'
            });
            _audioForASR.ondataavailable = (e) => { if (e.data.size > 0) _audioChunks.push(e.data); };
            _audioForASR.start(500);
          }
          _startFrameCapture();
        }

        console.log(`[VideoCall] Browser recording started (mode: ${_mediaLevel})`);
      } catch (e) {
        console.error('[VideoCall] MediaRecorder error:', e);
        _cancelRecord();
        return;
      }
    }
```

- [ ] **Step 4: 修改 _stopRecord 函数，image/text 模式走新端点**

找到 `_stopRecord` 函数（约第 636 行）。在 `_processing = true;` 之后、`try {` 块内，根据 _mediaLevel 分支处理。

将整个 try 块替换为：

```javascript
    try {
      if (_mediaLevel !== 'video') {
        // ── image / text 降级模式 ──
        _stopFrameCapture();
        let audioBlob;

        if (_useNativeCamera) {
          if (_nativeAudioFrames.length > 0) {
            audioBlob = _buildWavFromNativeChunks(_nativeAudioFrames);
            _nativeAudioFrames = [];
          }
        } else {
          audioBlob = await _stopMediaRecorder(_audioForASR, _audioChunks);
          _audioForASR = null;
        }

        if (!audioBlob || audioBlob.size < 100) {
          _processing = false;
          _setRecordBtnDisabled(false);
          _updateStatus('等待录制...');
          return;
        }

        _updateStatus('识别中...');

        // 准备帧数据
        const frames = _sampleFrames(_capturedFrames, MAX_FRAMES);
        _capturedFrames = [];

        // 上传到 /api/video-call-frames
        const form = new FormData();
        form.append('conv_id', _convId || currentConvId);
        form.append('audio', audioBlob, 'vc_audio.webm');
        const metaList = frames.map(f => ({ ts_ms: f.ts_ms }));
        form.append('frames_meta', JSON.stringify(metaList));
        frames.forEach((f, i) => form.append(`frame_${i}`, f.blob, `frame_${i}.jpg`));

        const resp = await fetch('/api/video-call-frames', { method: 'POST', body: form });
        const result = await resp.json();

        if (!result.ok) {
          _updateStatus('处理失败');
          return;
        }

        // 检查挂断
        if (result.is_hangup) {
          // 发送最后一条消息并挂断
          const att = { type: 'video_call_frames', transcript: result.stt_text, media_level: result.media_level };
          await _sendToChat(typeof result.content === 'string' ? result.content : result.stt_text, att);
          _hangup();
          return;
        }

        // 发送消息给模型
        _aiSpeaking = true;
        _updateStatus('AI 思考中...');
        const att = { type: 'video_call_frames', transcript: result.stt_text, media_level: result.media_level };
        // content 可能是字符串（text模式）或数组（image模式），前端只发 stt_text 作为消息文本
        // 实际多模态内容由后端 /api/video-call-frames 返回并注入
        await _sendToChat(result.stt_text, att);

      } else {
        // ── video 模式（现有流程） ──
        let videoBlob, audioBlob;

        if (_useNativeCamera && window.AionVideo) {
          const b64 = window.AionVideo.stopRecord();
          if (!b64) { _processing = false; _setRecordBtnDisabled(false); return; }
          const bin = atob(b64);
          const arr = new Uint8Array(bin.length);
          for (let i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i);
          videoBlob = new Blob([arr], { type: 'video/mp4' });

          if (_nativeAudioFrames.length > 0) {
            audioBlob = _buildWavFromNativeChunks(_nativeAudioFrames);
            _nativeAudioFrames = [];
          }
        } else {
          videoBlob = await _stopMediaRecorder(_videoRecorder, _videoChunks);
          _videoRecorder = null;
          audioBlob = await _stopMediaRecorder(_audioForASR, _audioChunks);
          _audioForASR = null;
        }

        if (!videoBlob || videoBlob.size < 1000) {
          _processing = false;
          _setRecordBtnDisabled(false);
          _updateStatus('等待录制...');
          return;
        }

        _updateStatus('上传中...');
        const videoUrl = await _uploadFile(videoBlob, 'video_clip');
        if (!videoUrl) {
          _processing = false;
          _setRecordBtnDisabled(false);
          _updateStatus('上传失败');
          return;
        }

        _updateStatus('识别中...');
        let transcript = '';
        if (audioBlob && audioBlob.size > 100) {
          transcript = await _transcribeAudio(audioBlob);
        }

        const hangupWords = ['再见', '拜拜', '挂断', '结束通话', '挂了'];
        if (transcript && hangupWords.some(kw => transcript.includes(kw))) {
          const att = { type: 'video_clip', url: videoUrl, duration: Math.round(duration), transcript };
          await _sendToChat('', att);
          _hangup();
          return;
        }

        _aiSpeaking = true;
        _updateStatus('AI 思考中...');
        const att = { type: 'video_clip', url: videoUrl, duration: Math.round(duration), transcript };
        await _sendToChat(transcript, att);
      }
    } catch (e) {
      console.error('[VideoCall] Record process error:', e);
      _updateStatus('⚠ 处理出错');
    } finally {
      _processing = false;
      _resetInactivityTimer();
    }
```

- [ ] **Step 5: 修改 _cancelRecord 函数，停止帧捕获**

找到 `_cancelRecord` 函数（约第 724 行），在函数开头的 `_clearRecordUI();` 之后添加：

```javascript
    _stopFrameCapture();
    _capturedFrames = [];
```

- [ ] **Step 6: Commit**

```
git add aion-chat/static/video-call.js
git commit -m "feat: video-call.js 三模式录制分支 + 场景变化检测"
```

---

### Task 7: video-call.js — 接收 media_level + UI 模式标签

**Files:**
- Modify: `aion-chat/static/video-call.js`

- [ ] **Step 1: 修改 aiInitiate 接收 media_level**

找到 `aiInitiate` 函数（约第 1062 行），在 `_convId = data.conv_id || currentConvId;` 之后添加：

```javascript
    _mediaLevel = data.media_level || 'video';
```

- [ ] **Step 2: 修改 userInitiate 获取 media_level**

找到 `userInitiate` 函数（约第 1018 行），将 fetch 调用改为捕获返回值：

```javascript
  function userInitiate() {
    if (_active || _ringing) return;
    _convId = currentConvId;

    // 插入「你拨打了视频电话」系统消息 + 获取 media_level
    if (_convId) {
      fetch('/api/video-call-init-sys-msg', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ conv_id: _convId })
      }).then(r => r.json()).then(data => {
        _mediaLevel = data.media_level || 'video';
      }).catch(e => console.error('[VideoCall] init sys msg failed:', e));
    }
```

（后续代码保持不变）

- [ ] **Step 3: 在 _showCallUI 中添加模式标签**

找到 `_showCallUI` 函数，在函数末尾（`_active = true;` 之前或之后）添加模式标签：

```javascript
    // 模式标签
    if (_mediaLevel !== 'video') {
      const badge = _createElement('div', {
        id: 'vcModeBadge',
        textContent: _mediaLevel === 'image' ? '📷 图片模式' : '🎤 语音模式',
      }, {
        position: 'absolute', top: '12px', right: '12px',
        background: 'rgba(0,0,0,0.6)', color: '#fff',
        padding: '4px 10px', borderRadius: '12px',
        fontSize: '13px', zIndex: '100001',
      });
      _overlay.appendChild(badge);
    }
```

- [ ] **Step 4: 在 _hangup 函数中重置 _mediaLevel**

找到 `_hangup` 函数（约第 924 行），在 `_active = false;` 之后添加：

```javascript
    _mediaLevel = 'video';
```

- [ ] **Step 5: Commit**

```
git add aion-chat/static/video-call.js
git commit -m "feat: 接收 media_level + 显示降级模式标签"
```

---

### Task 8: 集成测试 — 端到端验证

- [ ] **Step 1: 验证所有 Python 导入无报错**

Run (在 aion-chat 目录下):
```
python -c "from config import get_video_call_media_level; from sentinel import transcribe_audio, transcribe_audio_sync; from routes.voice import router; from routes.chat import router; print('All imports OK')"
```
Expected: `All imports OK`

- [ ] **Step 2: 验证 settings.json 覆盖机制**

Run:
```python
python -c "
from config import SETTINGS, get_video_call_media_level
print('Auto:', get_video_call_media_level())
SETTINGS['video_call_media_override'] = 'text'
print('Override text:', get_video_call_media_level())
SETTINGS['video_call_media_override'] = None
print('Reset:', get_video_call_media_level())
"
```
Expected: 三行输出，第二行应为 `Override text: text`

- [ ] **Step 3: 手动测试（启动服务）**

1. 启动 aion-chat 服务
2. 在 settings 中将 `video_call_media_override` 设为 `"image"`
3. 发起/接听视频通话
4. 确认右上角显示 "📷 图片模式" 标签
5. 按住录制→松开，确认走 `/api/video-call-frames` 端点
6. 切换为 `"text"` 模式重复测试，确认标签变为 "🎤 语音模式"
7. 切换为 `null` 恢复自动模式

- [ ] **Step 4: Commit 最终状态**

```
git add -A
git commit -m "chore: 视频通话降级策略集成完成"
```
