"""
设置、世界书、模型列表、TTS 路由
"""

import base64
import json

from fastapi import APIRouter
from fastapi.responses import Response, FileResponse
from pydantic import BaseModel
from typing import Optional

import httpx

import config
from config import SETTINGS, MODELS, save_settings, get_key, get_sentinel_config, load_worldbook, save_worldbook, load_chat_status, TTS_CACHE_DIR

router = APIRouter()

# ── 模型列表 ──────────────────────────────────────
@router.get("/api/models")
async def list_models():
    return [{"key": k, "provider": v["provider"]} for k, v in MODELS.items()]

# ── 设置 ──────────────────────────────────────────
class SettingsUpdate(BaseModel):
    gemini_key: Optional[str] = None
    siliconflow_key: Optional[str] = None
    gemini_free_key: Optional[str] = None
    aipro_key: Optional[str] = None
    dashscope_key: Optional[str] = None
    mimo_key: Optional[str] = None
    netease_music_u: Optional[str] = None
    default_model: Optional[str] = None
    sentinel_base_url: Optional[str] = None
    sentinel_api_key: Optional[str] = None
    sentinel_model: Optional[str] = None
    sentinel_vl_model: Optional[str] = None
    embedding_base_url: Optional[str] = None
    embedding_api_key: Optional[str] = None
    embedding_model: Optional[str] = None
    github_sync_token: Optional[str] = None
    sync_repo: Optional[str] = None
    device_name: Optional[str] = None

@router.get("/api/settings")
async def get_settings():
    def mask(k):
        if not k or len(k) < 8:
            return k
        return k[:4] + "*" * (len(k) - 8) + k[-4:]
    return {
        "gemini_key": SETTINGS.get("gemini_key", ""),
        "siliconflow_key": SETTINGS.get("siliconflow_key", ""),
        "gemini_free_key": SETTINGS.get("gemini_free_key", ""),
        "aipro_key": SETTINGS.get("aipro_key", ""),
        "dashscope_key": SETTINGS.get("dashscope_key", ""),
        "mimo_key": SETTINGS.get("mimo_key", ""),
        "netease_music_u": SETTINGS.get("netease_music_u", ""),
        "default_model": config.DEFAULT_MODEL,
        "sentinel_base_url": SETTINGS.get("sentinel_base_url", ""),
        "sentinel_api_key": SETTINGS.get("sentinel_api_key", ""),
        "sentinel_model": SETTINGS.get("sentinel_model", ""),
        "embedding_base_url": SETTINGS.get("embedding_base_url", ""),
        "embedding_api_key": SETTINGS.get("embedding_api_key", ""),
        "embedding_model": SETTINGS.get("embedding_model", ""),
        "gemini_key_masked": mask(SETTINGS.get("gemini_key", "")),
        "siliconflow_key_masked": mask(SETTINGS.get("siliconflow_key", "")),
        "gemini_free_key_masked": mask(SETTINGS.get("gemini_free_key", "")),
        "aipro_key_masked": mask(SETTINGS.get("aipro_key", "")),
        "dashscope_key_masked": mask(SETTINGS.get("dashscope_key", "")),
        "mimo_key_masked": mask(SETTINGS.get("mimo_key", "")),
        "netease_music_u_masked": mask(SETTINGS.get("netease_music_u", "")),
        "sentinel_base_url": SETTINGS.get("sentinel_base_url", ""),
        "sentinel_api_key": SETTINGS.get("sentinel_api_key", ""),
        "sentinel_api_key_masked": mask(SETTINGS.get("sentinel_api_key", "")),
        "sentinel_model": SETTINGS.get("sentinel_model", ""),
        "sentinel_vl_model": SETTINGS.get("sentinel_vl_model", ""),
        "embedding_base_url": SETTINGS.get("embedding_base_url", ""),
        "embedding_api_key": SETTINGS.get("embedding_api_key", ""),
        "embedding_api_key_masked": mask(SETTINGS.get("embedding_api_key", "")),
        "embedding_model": SETTINGS.get("embedding_model", ""),
        "github_sync_token": SETTINGS.get("github_sync_token", ""),
        "github_sync_token_masked": mask(SETTINGS.get("github_sync_token", "")),
        "sync_repo": SETTINGS.get("sync_repo", ""),
        "device_name": SETTINGS.get("device_name", ""),
    }

@router.put("/api/settings")
async def update_settings(body: SettingsUpdate):
    if body.gemini_key is not None:
        SETTINGS["gemini_key"] = body.gemini_key
    if body.siliconflow_key is not None:
        SETTINGS["siliconflow_key"] = body.siliconflow_key
    if body.gemini_free_key is not None:
        SETTINGS["gemini_free_key"] = body.gemini_free_key
    if body.aipro_key is not None:
        SETTINGS["aipro_key"] = body.aipro_key
    if body.dashscope_key is not None:
        SETTINGS["dashscope_key"] = body.dashscope_key
    if body.mimo_key is not None:
        SETTINGS["mimo_key"] = body.mimo_key
    if body.sentinel_base_url is not None:
        SETTINGS["sentinel_base_url"] = body.sentinel_base_url
    if body.sentinel_api_key is not None:
        SETTINGS["sentinel_api_key"] = body.sentinel_api_key
    if body.sentinel_model is not None:
        SETTINGS["sentinel_model"] = body.sentinel_model
    if body.embedding_base_url is not None:
        SETTINGS["embedding_base_url"] = body.embedding_base_url
    if body.embedding_api_key is not None:
        SETTINGS["embedding_api_key"] = body.embedding_api_key
    if body.embedding_model is not None:
        SETTINGS["embedding_model"] = body.embedding_model
    if body.netease_music_u is not None:
        old_mu = SETTINGS.get("netease_music_u", "")
        SETTINGS["netease_music_u"] = body.netease_music_u
        if body.netease_music_u != old_mu:
            # MUSIC_U 变更，重新登录 pyncm
            try:
                from music import reload_login
                reload_login()
            except Exception:
                pass
    if body.default_model is not None and body.default_model in MODELS:
        SETTINGS["default_model"] = body.default_model
        config.DEFAULT_MODEL = body.default_model
    for key in ("sentinel_base_url", "sentinel_api_key", "sentinel_model",
                "sentinel_vl_model", "embedding_base_url", "embedding_api_key",
                "embedding_model", "github_sync_token", "sync_repo", "device_name"):
        val = getattr(body, key, None)
        if val is not None:
            SETTINGS[key] = val
    save_settings(SETTINGS)
    return {"ok": True}

# ── 温度设置 ──────────────────────────────────────
class TempUpdate(BaseModel):
    temperature: float

@router.put("/api/settings/temperature")
async def update_temperature(body: TempUpdate):
    SETTINGS["temperature"] = body.temperature
    save_settings(SETTINGS)
    return {"ok": True}

# ── 视频通话开关 ──────────────────────────────────
@router.get("/api/settings/video-call")
async def get_video_call_setting():
    return {"video_call_enabled": SETTINGS.get("video_call_enabled", True)}

class VideoCallToggle(BaseModel):
    enabled: bool

@router.put("/api/settings/video-call")
async def update_video_call_setting(body: VideoCallToggle):
    SETTINGS["video_call_enabled"] = body.enabled
    save_settings(SETTINGS)
    return {"ok": True, "video_call_enabled": body.enabled}

# ── AI 生图开关 ───────────────────────────────────
@router.get("/api/settings/image-gen")
async def get_image_gen_setting():
    return {"image_gen_enabled": SETTINGS.get("image_gen_enabled", False)}

class ImageGenToggle(BaseModel):
    enabled: bool

@router.put("/api/settings/image-gen")
async def update_image_gen_setting(body: ImageGenToggle):
    SETTINGS["image_gen_enabled"] = body.enabled
    save_settings(SETTINGS)
    return {"ok": True, "image_gen_enabled": body.enabled}

# ── Gemini CLI 工具调用开关 ─────────────────────────
@router.get("/api/settings/gemini-cli-tools")
async def get_gemini_cli_tools_setting():
    return {"gemini_cli_tools_enabled": SETTINGS.get("gemini_cli_tools_enabled", False)}

class GeminiCliToolsToggle(BaseModel):
    enabled: bool

@router.put("/api/settings/gemini-cli-tools")
async def update_gemini_cli_tools_setting(body: GeminiCliToolsToggle):
    SETTINGS["gemini_cli_tools_enabled"] = body.enabled
    save_settings(SETTINGS)
    return {"ok": True, "gemini_cli_tools_enabled": body.enabled}

# ── 桌宠开关 ──────────────────────────────────────
@router.get("/api/settings/pet")
async def get_pet_setting():
    return {"pet_enabled": SETTINGS.get("pet_enabled", False)}

class PetToggle(BaseModel):
    enabled: bool

@router.put("/api/settings/pet")
async def update_pet_setting(body: PetToggle):
    SETTINGS["pet_enabled"] = body.enabled
    save_settings(SETTINGS)
    return {"ok": True, "pet_enabled": body.enabled}

# ── 记忆 Links 展开开关 ──────────────────────────
@router.get("/api/settings/recall-links")
async def get_recall_links_setting():
    return {"recall_use_links": SETTINGS.get("recall_use_links", False)}

class RecallLinksToggle(BaseModel):
    enabled: bool

@router.put("/api/settings/recall-links")
async def update_recall_links_setting(body: RecallLinksToggle):
    SETTINGS["recall_use_links"] = body.enabled
    save_settings(SETTINGS)
    return {"ok": True, "recall_use_links": body.enabled}

# ── 世界书 ────────────────────────────────────────
class WorldBookUpdate(BaseModel):
    ai_persona: str = ""
    user_persona: str = ""
    system_prompt: str = ""
    ai_name: str = "AI"
    user_name: str = "你"

@router.get("/api/worldbook")
async def get_worldbook():
    return load_worldbook()

@router.put("/api/worldbook")
async def update_worldbook(body: WorldBookUpdate):
    save_worldbook({"ai_persona": body.ai_persona, "user_persona": body.user_persona,
                    "system_prompt": body.system_prompt, "ai_name": body.ai_name, "user_name": body.user_name})
    return {"ok": True}

# ── 聊天状态 ──────────────────────────────────────
@router.get("/api/chat_status")
async def get_chat_status_api():
    return load_chat_status()

# ── TTS 语音合成 ──────────────────────────────────
class TTSRequest(BaseModel):
    text: str
    voice: str = ""
    msg_id: Optional[str] = None

@router.post("/api/tts")
async def tts_synthesize(body: TTSRequest):
    key = get_key("mimo")
    if not key:
        return Response(content=json.dumps({"error": "未配置 MiMo API Key"}), status_code=400, media_type="application/json")
    if not body.text.strip():
        return Response(content=json.dumps({"error": "文本不能为空"}), status_code=400, media_type="application/json")
    voice = body.voice or "Milo"
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                "https://api.xiaomimimo.com/v1/chat/completions",
                headers={"api-key": key, "Content-Type": "application/json"},
                json={
                    "model": "mimo-v2.5-tts",
                    "messages": [{"role": "assistant", "content": body.text.strip()}],
                    "audio": {"format": "wav", "voice": voice},
                }
            )
        if resp.status_code != 200:
            return Response(content=json.dumps({"error": f"TTS API 错误: {resp.status_code}"}), status_code=502, media_type="application/json")
        data = resp.json()
        audio_base64 = data["choices"][0]["message"]["audio"]["data"]
        audio_bytes = base64.b64decode(audio_base64)
        # 如果提供了 msg_id，将音频缓存到服务器
        if body.msg_id:
            import re
            safe_id = re.sub(r'[^a-zA-Z0-9_\-]', '', body.msg_id)
            if safe_id:
                cache_path = TTS_CACHE_DIR / f"{safe_id}.wav"
                cache_path.write_bytes(audio_bytes)
        return Response(content=audio_bytes, media_type="audio/wav")
    except Exception as e:
        return Response(content=json.dumps({"error": str(e)}), status_code=500, media_type="application/json")

@router.head("/api/tts/audio/{msg_id}")
@router.get("/api/tts/audio/{msg_id}")
async def tts_audio(msg_id: str):
    import re
    safe_id = re.sub(r'[^a-zA-Z0-9_\-]', '', msg_id)
    if not safe_id:
        return Response(status_code=404)
    # 先尝试 .wav，再尝试 .mp3（兼容旧缓存）
    for ext in (".wav", ".mp3"):
        cache_path = TTS_CACHE_DIR / f"{safe_id}{ext}"
        if cache_path.exists():
            media = "audio/wav" if ext == ".wav" else "audio/mpeg"
            return FileResponse(cache_path, media_type=media, filename=f"{safe_id}{ext}")
    return Response(status_code=404)

@router.get("/api/tts/voices")
async def tts_voice_list():
    """返回 MiMo 预置音色列表"""
    voices = [
        {"uri": "Milo", "customName": "Milo (英文男)", "language": "English", "gender": "male"},
        {"uri": "Dean", "customName": "Dean (英文男)", "language": "English", "gender": "male"},
        {"uri": "Mia", "customName": "Mia (英文女)", "language": "English", "gender": "female"},
        {"uri": "Chloe", "customName": "Chloe (英文女)", "language": "English", "gender": "female"},
        {"uri": "冰糖", "customName": "冰糖 (中文女)", "language": "中文", "gender": "female"},
        {"uri": "茉莉", "customName": "茉莉 (中文女)", "language": "中文", "gender": "female"},
        {"uri": "苏打", "customName": "苏打 (中文男)", "language": "中文", "gender": "male"},
        {"uri": "白桦", "customName": "白桦 (中文男)", "language": "中文", "gender": "male"},
    ]
    return {"voices": voices}
