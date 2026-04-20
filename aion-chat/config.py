"""
全局配置：路径、常量、settings / worldbook / chat_status 读写
"""

import json, time, re
from pathlib import Path

# ── 路径 ─────────────────────────────────────────
BASE_DIR = Path(__file__).parent
PUBLIC_DIR = BASE_DIR.parent / "public"
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)
DB_PATH = DATA_DIR / "chat.db"
UPLOADS_DIR = DATA_DIR / "uploads"
UPLOADS_DIR.mkdir(exist_ok=True)
CHATS_DIR = DATA_DIR / "chats"
CHATS_DIR.mkdir(exist_ok=True)
SCREENSHOTS_DIR = DATA_DIR / "screenshots"
SCREENSHOTS_DIR.mkdir(exist_ok=True)
MONITOR_LOGS_DIR = DATA_DIR / "monitor_logs"
MONITOR_LOGS_DIR.mkdir(exist_ok=True)
TTS_CACHE_DIR = DATA_DIR / "tts_cache"
TTS_CACHE_DIR.mkdir(exist_ok=True)

SETTINGS_PATH = DATA_DIR / "settings.json"
WORLDBOOK_PATH = DATA_DIR / "worldbook.json"
CHAT_STATUS_PATH = DATA_DIR / "chat_status.json"
CAM_CONFIG_PATH = DATA_DIR / "cam_config.json"
DIGEST_ANCHOR_PATH = DATA_DIR / "digest_anchor.json"
INDEX_PATH = CHATS_DIR / "_index.json"

# ── Settings ─────────────────────────────────────
def load_settings():
    if SETTINGS_PATH.exists():
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    keys = {"gemini_key": "", "siliconflow_key": "", "gemini_free_key": "", "aipro_key": "", "custom_keys": {}}
    txt = BASE_DIR.parent / "所需要的API.txt"
    if txt.exists():
        with open(txt, "r", encoding="utf-8") as f:
            for line in f:
                if "gemini-api" in line.lower():
                    keys["gemini_key"] = line.split("：")[-1].strip()
                elif "硅基流动" in line.lower() and "api" in line.lower():
                    keys["siliconflow_key"] = line.split("：")[-1].strip()
    save_settings(keys)
    return keys

def save_settings(data: dict):
    with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

SETTINGS = load_settings()

def get_key(provider: str) -> str:
    if provider == "gemini":
        return SETTINGS.get("gemini_key", "")
    if provider == "gemini_free":
        return SETTINGS.get("gemini_free_key", "") or SETTINGS.get("gemini_key", "")
    if provider == "aipro":
        return SETTINGS.get("aipro_key", "")
    if provider == "siliconflow":
        return SETTINGS.get("siliconflow_key", "")
    # 自定义端点：provider 传入 key_name，从 custom_keys 映射中取
    return SETTINGS.get("custom_keys", {}).get(provider, "")

# ── Worldbook ────────────────────────────────────
def load_worldbook():
    if WORLDBOOK_PATH.exists():
        try:
            return json.loads(WORLDBOOK_PATH.read_text(encoding='utf-8'))
        except:
            pass
    return {"ai_persona": "", "user_persona": "", "system_prompt": "", "ai_name": "AI", "user_name": "你"}

def save_worldbook(data: dict):
    WORLDBOOK_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')

# ── Chat Status ──────────────────────────────────
def load_chat_status() -> dict:
    if CHAT_STATUS_PATH.exists():
        try:
            return json.loads(CHAT_STATUS_PATH.read_text(encoding='utf-8'))
        except:
            pass
    return {"status": "", "updated_at": 0}

def save_chat_status(status: str):
    data = {"status": status, "updated_at": time.time()}
    CHAT_STATUS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')

# ── Digest Anchor ────────────────────────────────
def load_digest_anchor() -> float:
    """返回上次总结的时间戳锚点，0.0 表示从未总结过"""
    if DIGEST_ANCHOR_PATH.exists():
        try:
            data = json.loads(DIGEST_ANCHOR_PATH.read_text(encoding='utf-8'))
            return float(data.get("last_digest_ts", 0.0))
        except:
            pass
    return 0.0

def save_digest_anchor(ts: float):
    data = {"last_digest_ts": ts, "updated_at": time.time()}
    DIGEST_ANCHOR_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')

# ── 文件索引 ─────────────────────────────────────
def load_file_index():
    if INDEX_PATH.exists():
        try:
            return json.loads(INDEX_PATH.read_text(encoding='utf-8'))
        except:
            return {}
    return {}

def save_file_index(idx):
    INDEX_PATH.write_text(json.dumps(idx, ensure_ascii=False, indent=2), encoding='utf-8')

def sanitize_filename(name):
    return re.sub(r'[\\/:*?"<>|\n\r]', '_', name).strip().rstrip('.')

# ── 模型配置 ─────────────────────────────────────
# 添加自定义第三方 OpenAI 兼容端点（如 Claude 中转站）：
#   1) 在 data/settings.json 里加 "custom_keys": {"<key_name>": "sk-xxxx"}
#   2) 在下方 MODELS 里加一条：
#        "显示名": {"provider": "custom", "model": "<上游模型名>",
#                    "base_url": "https://xxx/v1", "key_name": "<上面的key_name>"}
#   base_url 填到 /v1 层（代码会自动拼 /chat/completions）。
MODELS = {
    "硅基GLM-5":        {"provider": "siliconflow", "model": "Pro/zai-org/GLM-5"},
    "硅基GLM-5.1":      {"provider": "siliconflow", "model": "Pro/zai-org/GLM-5.1"},
    "硅基Kimi-K2.5":    {"provider": "siliconflow", "model": "Pro/moonshotai/Kimi-K2.5"},
    "gemini-3.1-flash-lite": {"provider": "gemini", "model": "gemini-3.1-flash-lite-preview"},
    "gemini-2.5-pro":        {"provider": "gemini", "model": "gemini-2.5-pro"},
    "gemini-3-flash":        {"provider": "gemini", "model": "gemini-3-flash-preview"},
    "gemini-3.1-pro":        {"provider": "gemini", "model": "gemini-3.1-pro-preview"},
    "claude-sonnet-4-6":  {"provider": "aipro", "model": "claude-sonnet-4-6"},
    "claude-opus4.6":    {"provider": "aipro", "model": "claude-opus-4-6"},
    "claude-opus4.6T":    {"provider": "aipro", "model": "claude-opus-4-6-thinking"},
    "哈基米3.1pro":    {"provider": "aipro", "model": "gemini-3.1-pro-high"},
    "哈基米2.5pro":    {"provider": "aipro", "model": "gemini-2.5-pro"},

    # 自定义第三方 OpenAI 兼容端点示例（删掉注释#即可启用，填好 base_url 与 key_name）
    # "自定义-claude":  {"provider": "custom", "model": "claude-sonnet-4-6", "base_url": "https://your-relay.example.com/v1", "key_name": "myrelay"},
}

DEFAULT_MODEL = "gemini-3-flash"

# ── 摄像头默认配置 ───────────────────────────────
DEFAULT_CAM_CFG = {
    "camera_index": 0,
    "auto_interval_min": 10,
    "auto_interval_max": 20,
    "max_screenshots": 200,
    "monitor_enabled": False,
    "quiet_hours_enabled": False,
    "quiet_hours_start": "00:00",
    "quiet_hours_end": "09:00",
}

def load_cam_config() -> dict:
    if CAM_CONFIG_PATH.exists():
        with open(CAM_CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        # 兼容旧配置：将 auto_interval（秒）迁移为 min/max（分钟）
        if "auto_interval" in cfg and "auto_interval_min" not in cfg:
            old_minutes = max(1, cfg.pop("auto_interval", 600) // 60)
            cfg["auto_interval_min"] = old_minutes
            cfg["auto_interval_max"] = old_minutes
        elif "auto_interval" in cfg:
            cfg.pop("auto_interval", None)
        for k, v in DEFAULT_CAM_CFG.items():
            cfg.setdefault(k, v)
        return cfg
    return dict(DEFAULT_CAM_CFG)

def save_cam_config(cfg: dict):
    with open(CAM_CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)

# ── 允许上传的文件类型 ────────────────────────────
ALLOWED_TYPES = {'image/jpeg', 'image/png', 'image/gif', 'image/webp',
                 'video/mp4', 'video/webm', 'video/quicktime'}
