"""
传感器事件驱动环境感知：MacroDroid webhook → 事件累积 → Sentinel 分析
"""

import time, threading, logging, asyncio

log = logging.getLogger("sensor")

HIGH_PRIORITY_EVENTS = {"geofence"}
WINDOW_SECONDS = 15 * 60

_buffer: list[dict] = []
_buffer_lock = threading.Lock()
_window_timer: threading.Timer | None = None
_event_loop: asyncio.AbstractEventLoop | None = None


def set_event_loop(loop: asyncio.AbstractEventLoop):
    global _event_loop
    _event_loop = loop


def _normalize_event(payload: dict) -> dict:
    """标准化事件格式，补充服务端时间戳"""
    return {
        "event": payload.get("event", "unknown"),
        "data": payload.get("data", {}),
        "ts": payload.get("ts") or time.time(),
        "received_at": time.time(),
    }


async def handle_sensor_event(payload: dict) -> dict:
    """webhook handler 入口，由 routes/webhooks.py 调用"""
    event = _normalize_event(payload)
    event_type = event["event"]

    if event_type in HIGH_PRIORITY_EVENTS:
        return await _handle_high_priority(event)
    else:
        _buffer_event(event)
        return {"buffered": True, "event": event_type, "buffer_size": len(_buffer)}


def _buffer_event(event: dict):
    """将低优事件追加到缓冲区，必要时启动窗口计时器"""
    global _window_timer
    with _buffer_lock:
        _buffer.append(event)
        size = len(_buffer)

    if _window_timer is None or not _window_timer.is_alive():
        _window_timer = threading.Timer(WINDOW_SECONDS, _on_window_expire)
        _window_timer.daemon = True
        _window_timer.start()
        log.info("窗口计时器已启动（%ds），当前缓冲 %d 事件", WINDOW_SECONDS, size)


def _flush_buffer() -> list[dict]:
    """取出并清空缓冲区"""
    global _window_timer
    with _buffer_lock:
        events = list(_buffer)
        _buffer.clear()
    if _window_timer and _window_timer.is_alive():
        _window_timer.cancel()
    _window_timer = None
    return events
