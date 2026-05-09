"""
ntfy.sh JSON 流桥接：订阅公网 topic，将消息转发给 sensor 管道
"""

import asyncio, json, logging, secrets
import aiohttp
from config import SETTINGS, save_settings

log = logging.getLogger("ntfy_bridge")

_task: asyncio.Task | None = None
_connected = False


def _ensure_topic() -> str:
    topic = SETTINGS.get("ntfy_topic", "")
    if not topic:
        topic = f"aions-sensor-{secrets.token_hex(6)}"
        SETTINGS["ntfy_topic"] = topic
        save_settings(SETTINGS)
        log.info("自动生成 ntfy topic: %s", topic)
    return topic


async def _subscribe_loop():
    global _connected
    from sensor import handle_sensor_event

    topic = _ensure_topic()
    url = f"https://ntfy.sh/{topic}/json"
    backoff = 10

    while True:
        try:
            log.info("连接 ntfy JSON 流: %s", url)
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
                            log.warning("ntfy 消息非法 JSON: %s", msg[:100])
                            continue
                        if "event" not in payload:
                            log.warning("ntfy 消息缺少 event 字段: %s", msg[:100])
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
            log.warning("ntfy 断开: %s, %ds 后重连", e, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)


def start(loop: asyncio.AbstractEventLoop):
    global _task
    if not SETTINGS.get("ntfy_enabled", False):
        log.info("ntfy 桥接未启用 (ntfy_enabled=false)")
        return
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
