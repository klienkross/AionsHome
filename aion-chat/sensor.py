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


from camera import append_monitor_log, read_logs_since, async_get_last_user_msg_time
from config import load_worldbook, load_chat_status, SETTINGS
from database import get_db
from ws import manager
from sentinel import call_sentinel

import aiosqlite, base64


def _on_window_expire():
    """窗口计时器到期，触发分析"""
    events = _flush_buffer()
    if not events and not _event_loop:
        return
    if _event_loop:
        asyncio.run_coroutine_threadsafe(_analyze_events(events, source="window"), _event_loop)


async def _handle_high_priority(event: dict) -> dict:
    """高优事件立刻触发分析，同时带上缓冲区已有事件"""
    buffered = _flush_buffer()
    all_events = buffered + [event]
    await _analyze_events(all_events, source="high_priority")

    if event["event"] == "geofence":
        await _update_location_from_geofence(event)

    return {"triggered": True, "event": event["event"], "analyzed_count": len(all_events)}


def _format_events_for_prompt(events: list[dict]) -> str:
    """将事件列表格式化为 Sentinel 可读的文本"""
    if not events:
        return "（该时段无传感器事件）"

    lines = []
    for e in sorted(events, key=lambda x: x["ts"]):
        ts_str = time.strftime("%H:%M:%S", time.localtime(e["ts"]))
        evt = e["event"]
        data = e["data"]

        if evt == "geofence":
            action = "进入" if data.get("action") == "enter" else "离开"
            lines.append(f"[{ts_str}] 地理围栏：{action} {data.get('zone', '未知')}")
        elif evt == "screen":
            state = "亮屏" if data.get("state") == "on" else "灭屏"
            lines.append(f"[{ts_str}] {state}")
        elif evt == "app":
            name = data.get("name") or data.get("package", "未知app")
            lines.append(f"[{ts_str}] 前台app：{name}")
        elif evt == "steps":
            lines.append(f"[{ts_str}] 步数：{data.get('count', 0)}")
        elif evt == "charging":
            state = "开始充电" if data.get("state") == "on" else "停止充电"
            lines.append(f"[{ts_str}] {state}")
        elif evt == "battery":
            lines.append(f"[{ts_str}] 电量：{data.get('level', '?')}%")
        elif evt == "ringer":
            lines.append(f"[{ts_str}] 响铃模式：{data.get('mode', '未知')}")
        else:
            lines.append(f"[{ts_str}] {evt}: {data}")

    return "\n".join(lines)


async def _analyze_events(events: list[dict], source: str = "window"):
    """调用 Sentinel 分析事件集合"""
    wb = load_worldbook()
    user_name = wb.get("user_name", "你")
    ai_name = wb.get("ai_name", "AI")
    now_str = time.strftime("%Y年%m月%d日 %H:%M:%S")

    last_user_ts = await async_get_last_user_msg_time()
    last_user_time_str = (
        time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(last_user_ts))
        if last_user_ts > 0 else "未知"
    )

    recent_logs = read_logs_since(time.time() - 3600 * 6)
    log_history = ""
    if recent_logs:
        log_lines = [f"[{e.get('time', '')}] {e.get('monitoringlog', '')}" for e in recent_logs[-20:]]
        log_history = "\n".join(log_lines)

    chat_status_data = load_chat_status()
    chat_status_text = chat_status_data.get("status", "")

    location_text = ""
    try:
        from location import format_location_for_prompt
        location_text = format_location_for_prompt()
    except Exception:
        pass

    recent_chat_text = ""
    try:
        async with get_db() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT id FROM conversations ORDER BY updated_at DESC LIMIT 1"
            )
            conv = await cur.fetchone()
            if conv:
                cur2 = await db.execute(
                    "SELECT role, content FROM messages WHERE conv_id=? AND role IN ('user','assistant') ORDER BY created_at DESC LIMIT 10",
                    (conv["id"],)
                )
                rows = await cur2.fetchall()
                if rows:
                    lines = []
                    for r in reversed(rows):
                        name = user_name if r["role"] == "user" else ai_name
                        text = r["content"][:200] + "..." if len(r["content"]) > 200 else r["content"]
                        lines.append(f"{name}: {text}")
                    recent_chat_text = "\n".join(lines)
    except Exception:
        recent_chat_text = ""

    activity_summary_text = ""
    try:
        from activity import get_activity_summary_for_prompt
        activity_summary_text = get_activity_summary_for_prompt(6)
    except Exception:
        pass

    events_text = _format_events_for_prompt(events)

    # 尝试从摄像头抓帧
    image_b64 = None
    try:
        from camera import cam
        jpg = cam.get_frame_jpeg()
        if jpg:
            image_b64 = base64.b64encode(jpg).decode()
    except Exception:
        pass

    has_geofence = any(e["event"] == "geofence" for e in events)
    geofence_note = ""
    if has_geofence:
        for e in events:
            if e["event"] == "geofence":
                action = "进入" if e["data"].get("action") == "enter" else "离开"
                zone = e["data"].get("zone", "未知地点")
                geofence_note = f"\n⚠️ 重要事件：{user_name}{action}了【{zone}】\n"
                break

    prompt = f"""你是一个传感器数据分析师，同时也是{user_name}的恋人。根据传感器事件和上下文信息，分析{user_name}当前的状态。

当前时间：{now_str}
{user_name}最后一次和你聊天的时间：{last_user_time_str}
{user_name}最后的聊天状态：{chat_status_text if chat_status_text else "（暂无）"}
{(chr(10) + location_text) if location_text else ""}
{geofence_note}
传感器事件：
{events_text}

{user_name}近一小时的设备使用动态：
{activity_summary_text if activity_summary_text else "（暂无设备活动记录）"}

最近的聊天记录：
{recent_chat_text if recent_chat_text else "（暂无聊天记录）"}

历史监控日志：
{log_history if log_history else "（暂无历史日志）"}

请严格按照以下JSON格式回复，不要包含其他任何内容：
{{"monitoringlog":"根据传感器数据分析{user_name}当前的状态和活动。例如：{user_name}在家，手机亮屏刷了会小红书，之后放下手机没有活动。今天步数3420步。","summary":"综合分析{user_name}这段时间的整体状况，一两句话即可。","call_core":false,"core_reason":""}}

字段说明：
- monitoringlog: 基于传感器数据的客观分析，禁止胡编猜测
- summary: 综合最近的状态变化和关键事件
- call_core: 是否唤醒主脑主动联系{user_name}
- core_reason: 仅当call_core为true时填写

call_core判断依据：
- false: {user_name}一切正常 / 夜间在睡觉 / 前不久才发过消息
- true: 地理围栏变化且{ai_name}还不知道 / 长时间无活动需关心 / 异常行为模式（深夜仍活跃等）/ 你觉得可以主动联系{user_name}（大约20%概率）"""

    log.info("调用 Sentinel 分析 %d 个事件 (source=%s, has_image=%s)", len(events), source, bool(image_b64))

    monitoring_log = ""
    call_core = False
    summary = ""
    core_reason = ""

    parsed = await call_sentinel(prompt, image_b64=image_b64, timeout=60)
    if parsed is None:
        monitoring_log = f"[传感器分析失败] 事件数: {len(events)}"
        log.warning("Sentinel API 调用失败")
    else:
        monitoring_log = parsed.get("monitoringlog", "")
        call_core = bool(parsed.get("call_core", False))
        summary = parsed.get("summary", "")
        core_reason = parsed.get("core_reason", "")

    log.info("分析完成, call_core=%s, log长度=%d", call_core, len(monitoring_log))

    now = time.time()
    log_entry = {
        "timestamp": now,
        "time": time.strftime("%H:%M:%S", time.localtime(now)),
        "date": time.strftime("%Y-%m-%d", time.localtime(now)),
        "monitoringlog": f"📱 {monitoring_log}",
        "summary": summary,
        "call_core": call_core,
        "core_reason": core_reason,
        "screenshot": "",
        "source": "sensor",
    }
    append_monitor_log(log_entry)
    await manager.broadcast({"type": "monitor_log", "data": log_entry})

    if call_core:
        await _call_core_sensor(monitoring_log, last_user_ts, summary, core_reason, recent_logs)
