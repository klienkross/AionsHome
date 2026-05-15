"""
ntfy.sh JSON 流桥接：订阅公网 topic，将消息转发给 sensor 管道
"""

import asyncio, json, logging, secrets
from logging.handlers import RotatingFileHandler
import aiohttp
from config import SETTINGS, save_settings, DATA_DIR

log = logging.getLogger("ntfy_bridge")

_task: asyncio.Task | None = None
_connected = False

# 这些异常是网络/协议层的正常瞬断，降为 DEBUG 避免刷屏
_TRANSIENT_ERRORS = (
    "TransferEncoding",       # chunked 流被服务端截断
    "ServerDisconnected",     # 服务端主动关闭
    "ClientConnector",        # 网络不通、SSL 握手超时等
    "TimeoutError",
    "ConnectionReset",
)


def _is_transient(exc: Exception) -> bool:
    name = type(exc).__name__
    return any(k in name for k in _TRANSIENT_ERRORS)


def _mask_topic(topic: str) -> str:
    """保留前4位，其余替换为*，避免日志泄漏频道名"""
    if len(topic) <= 4:
        return "****"
    return topic[:4] + "*" * (len(topic) - 4)


def _ensure_topic() -> str:
    topic = SETTINGS.get("ntfy_topic", "")
    if not topic:
        topic = f"aions-sensor-{secrets.token_hex(6)}"
        SETTINGS["ntfy_topic"] = topic
        save_settings(SETTINGS)
        log.info("自动生成 ntfy topic: %s", _mask_topic(topic))
    return topic


async def _subscribe_loop():
    global _connected
    from sensor import handle_sensor_event

    topic = _ensure_topic()
    url = f"https://ntfy.sh/{topic}/json"
    masked_url = f"https://ntfy.sh/{_mask_topic(topic)}/json"
    backoff = 10

    while True:
        try:
            log.info("连接 ntfy JSON 流: %s", masked_url)
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=None, sock_read=None)) as resp:
                    _connected = True
                    backoff = 10
                    log.info("ntfy 已连接")
                    async for raw_line in resp.content:
                        text = raw_line.decode("utf-8", errors="replace").strip()
                        if not text:
                            continue
                        try:
                            envelope = json.loads(text)
                        except json.JSONDecodeError:
                            continue
                        if envelope.get("event") != "message":
                            continue
                        msg = envelope.get("message", "")
                        try:
                            payload = json.loads(msg)
                        except json.JSONDecodeError:
                            log.warning("ntfy 消息非法 JSON (已省略内容)")
                            continue
                        if "event" not in payload:
                            log.warning("ntfy 消息缺少 event 字段 (已省略内容)")
                            continue
                        log.info("ntfy 收到事件: %s", payload.get("event"))
                        try:
                            await handle_sensor_event(payload)
                        except Exception as e:
                            log.warning("sensor 处理异常: %s", e)
        except asyncio.CancelledError:
            _connected = False
            return
        except Exception as e:
            _connected = False
            lvl = logging.DEBUG if _is_transient(e) else logging.WARNING
            log.log(lvl, "ntfy 断开: %s, %ds 后重连", e, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)


def _setup_file_log():
    """挂载滚动文件 handler，最大 512 KB，保留 2 个旧文件"""
    log_path = DATA_DIR / "ntfy.log"
    handler = RotatingFileHandler(
        log_path, maxBytes=512 * 1024, backupCount=2, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(handler)
    log.info("ntfy 日志文件: %s", log_path)


def start(loop: asyncio.AbstractEventLoop):
    global _task
    if not SETTINGS.get("ntfy_enabled", False):
        log.info("ntfy 桥接未启用 (ntfy_enabled=false)")
        return
    _setup_file_log()
    _task = loop.create_task(_subscribe_loop())
    log.info("ntfy 桥接已启动")


def stop():
    global _task
    if _task and not _task.done():
        _task.cancel()
    _task = None


def get_status() -> dict:
    return {
        "enabled": SETTINGS.get("ntfy_enabled", False),
        "topic": SETTINGS.get("ntfy_topic", ""),
        "connected": _connected,
    }
