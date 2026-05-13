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

GEOFENCE_DEBOUNCE_SECONDS = 60
_geofence_timers: dict[str, threading.Timer] = {}
_geofence_pending: dict[str, dict] = {}


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


def _to_activity_entry(event: dict) -> dict:
    """将传感器事件转换为 activity_log 条目"""
    ts = event["ts"]
    evt = event["event"]
    data = event["data"]

    if evt == "geofence":
        action = "进入" if data.get("action") == "enter" else "离开"
        app, title = "地理围栏", f"{action} {data.get('zone', '?')}"
    elif evt == "screen":
        app, title = ("亮屏" if data.get("state") == "on" else "灭屏"), ""
    elif evt == "app":
        app, title = (data.get("name") or data.get("package", "未知app")), "前台"
    elif evt == "steps":
        app, title = "步数", f"{data.get('count', 0)} 步"
    elif evt == "charging":
        app, title = "充电", ("开始充电" if data.get("state") == "on" else "停止充电")
    elif evt == "battery":
        app, title = "电量", f"{data.get('level', '?')}%"
    elif evt == "ringer":
        modes = {"silent": "静音", "vibrate": "振动", "normal": "正常"}
        app, title = "响铃", modes.get(data.get("mode", ""), data.get("mode", ""))
    else:
        app, title = evt, str(data)

    return {
        "timestamp": ts,
        "time": time.strftime("%H:%M:%S", time.localtime(ts)),
        "date": time.strftime("%Y-%m-%d", time.localtime(ts)),
        "device": "phone",
        "app": app,
        "title": title,
    }


async def handle_sensor_event(payload: dict) -> dict:
    """webhook handler 入口，由 routes/webhooks.py 调用"""
    event = _normalize_event(payload)
    event_type = event["event"]

    try:
        from activity import append_activity_log
        entry = _to_activity_entry(event)
        append_activity_log(entry)
        await manager.broadcast({"type": "activity_log", "data": entry})
    except Exception:
        pass

    if event_type == "geofence":
        zone = event["data"].get("zone", "unknown")
        _debounce_geofence(zone, event)
        return {"debounced": True, "zone": zone, "wait_seconds": GEOFENCE_DEBOUNCE_SECONDS}
    elif event_type in HIGH_PRIORITY_EVENTS:
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


def _debounce_geofence(zone: str, event: dict):
    """对同一 zone 的围栏事件做 60 秒去抖，只处理最后一次"""
    if zone in _geofence_timers:
        _geofence_timers[zone].cancel()
    _geofence_pending[zone] = event
    timer = threading.Timer(GEOFENCE_DEBOUNCE_SECONDS, _fire_geofence, args=[zone])
    timer.daemon = True
    timer.start()
    _geofence_timers[zone] = timer
    log.info("地理围栏去抖：zone=%s action=%s，%ds 后生效", zone, event["data"].get("action"), GEOFENCE_DEBOUNCE_SECONDS)


def _fire_geofence(zone: str):
    """去抖窗口结束，检查状态是否真的变了再触发分析"""
    event = _geofence_pending.pop(zone, None)
    _geofence_timers.pop(zone, None)
    if not event or not _event_loop:
        return

    data = event["data"]
    action = data.get("action", "enter")
    new_state = "at_home" if zone == "home" and action == "enter" else (
        f"at_{zone}" if action == "enter" else "outside"
    )

    try:
        from location import load_location_status
        current_state = load_location_status().get("state", "unknown")
    except Exception:
        current_state = "unknown"

    if new_state == current_state:
        log.info("地理围栏去抖结束：zone=%s 状态未变（%s），跳过分析", zone, current_state)
        return

    log.info("地理围栏去抖结束：zone=%s 状态变更 %s → %s，触发分析", zone, current_state, new_state)
    asyncio.run_coroutine_threadsafe(_handle_high_priority(event), _event_loop)


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

    prompt = f"""你是一个传感器数据分析师。根据传感器事件和上下文信息，客观记录{user_name}当前的状态。

注意：短时间内切换多个app是正常的手机使用习惯，不要过度解读为"碎片化行为"或"注意力分散"。

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
- monitoringlog: 基于传感器数据的客观记录，只写事实，禁止推测情绪或心理状态
- summary: 综合最近的状态变化和关键事件，一两句话
- call_core: 是否唤醒主脑主动联系{user_name}
- core_reason: 仅当call_core为true时填写，限一句话

call_core判断依据（默认false，只有明确理由才设true）：
- false: {user_name}正常使用手机 / 夜间在睡觉 / 前不久才发过消息 / 没有显著变化
- true: 地理围栏变化且{ai_name}还不知道 / 超过2小时无任何活动需关心 / 深夜2点后仍活跃"""

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


from ai_providers import stream_ai
from memory import recall_memories
from config import DEFAULT_MODEL
from tts import TTSStreamer


async def _call_core_sensor(trigger_log: str, last_user_ts: float, summary: str = "", core_reason: str = "", cached_logs: list = None):
    """传感器分析触发 Core 唤醒"""
    wb = load_worldbook()
    user_name = wb.get("user_name", "你")
    ai_name = wb.get("ai_name", "AI")

    if last_user_ts > 0:
        elapsed = time.time() - last_user_ts
        hours = int(elapsed // 3600)
        minutes = int((elapsed % 3600) // 60)
        time_ago = f"{hours}小时{minutes}分钟" if hours > 0 else f"{minutes}分钟"
    else:
        time_ago = "很长时间"

    if cached_logs is not None:
        all_logs = cached_logs[-24:]
    else:
        all_logs = read_logs_since(last_user_ts if last_user_ts > 0 else time.time() - 3600 * 6)
        all_logs = all_logs[-24:]
    recent_detail = "\n".join([f"[{e.get('time', '')}] {e.get('monitoringlog', '')}" for e in all_logs[-5:]])
    if not recent_detail:
        recent_detail = trigger_log

    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM conversations ORDER BY updated_at DESC LIMIT 1")
        conv = await cur.fetchone()
        if not conv:
            return
        conv_id = conv["id"]
        model_key = conv["model"] or DEFAULT_MODEL

        cur = await db.execute(
            "SELECT role, content, attachments FROM messages WHERE conv_id=? AND role IN ('user','assistant') ORDER BY created_at DESC LIMIT 20",
            (conv_id,)
        )
        rows = await cur.fetchall()
        history = []
        for r in reversed(rows):
            d = dict(r)
            d["attachments"] = []
            history.append(d)

    prefix = []
    if wb.get("ai_persona"):
        prefix.append({"role": "user", "content": f"[系统设定 - AI人设]\n{wb['ai_persona']}"})
        prefix.append({"role": "assistant", "content": "收到，我会按照设定扮演角色。"})
    if wb.get("user_persona"):
        prefix.append({"role": "user", "content": f"[系统设定 - 用户信息]\n{wb['user_persona']}"})
        prefix.append({"role": "assistant", "content": "收到，我会记住你的信息。"})

    core_parts = [f"【{user_name}】已经{time_ago}没有和你说话了。"]
    if core_reason:
        core_parts.append(f"哨兵唤醒你的原因：{core_reason}")
    if summary:
        core_parts.append(f"这段时间{user_name}的整体状况：{summary}")
    core_parts.append(f"最新传感器分析：{trigger_log}")
    core_parts.append(f"最近的监控记录：\n{recent_detail}")

    try:
        from location import format_location_for_prompt
        loc_info = format_location_for_prompt()
        if loc_info:
            core_parts.append(f"\n{loc_info}")
    except Exception:
        pass

    core_prompt = "\n".join(core_parts)

    recalled, _ = await recall_memories(core_prompt[:300])
    mem_inject = []
    if recalled:
        mem_lines = "\n".join([f"- {m['content']}" for m in recalled])
        mem_inject = [
            {"role": "user", "content": f"[相关记忆]\n你脑海中与当前话题相关的记忆：\n{mem_lines}"},
            {"role": "assistant", "content": "收到，我会自然地参考这些记忆。"},
        ]

    messages = prefix + mem_inject + history + [{"role": "user", "content": core_prompt}]

    await manager.broadcast({"type": "core_alert", "data": {"source": "sensor", "reason": core_reason or trigger_log[:80]}})
    await asyncio.sleep(5)

    core_msg_id = f"msg_{int(time.time() * 1000)}_sr"
    sensor_tts = None
    if manager.any_tts_enabled():
        tts_voice = manager.get_tts_voice()
        if tts_voice:
            sensor_tts = TTSStreamer(core_msg_id, tts_voice, manager)

    full_text = ""
    try:
        _temp = SETTINGS.get("temperature")
        async for chunk in stream_ai(messages, model_key, temperature=_temp):
            full_text += chunk
            if sensor_tts:
                sensor_tts.feed(chunk)
    except Exception as e:
        full_text = f"[Core 回复失败] {e}"

    if not full_text.strip():
        return

    now = time.time()
    trigger_msg_id = f"msg_{int(now * 1000)}_st"
    async with get_db() as db:
        await db.execute(
            "INSERT INTO messages (id, conv_id, role, content, created_at, attachments) VALUES (?,?,?,?,?,?)",
            (trigger_msg_id, conv_id, "cam_trigger", core_prompt, now, "[]"),
        )
        sys_now = time.time()
        sys_msg_id = f"msg_{int(sys_now * 1000)}_ss"
        sys_content = f"📱 传感器检测到异常，拉响警报！"
        await db.execute(
            "INSERT INTO messages (id, conv_id, role, content, created_at, attachments) VALUES (?,?,?,?,?,?)",
            (sys_msg_id, conv_id, "system", sys_content, sys_now, "[]"),
        )
        await db.commit()
    sys_msg = {
        "id": sys_msg_id, "conv_id": conv_id, "role": "system",
        "content": sys_content, "created_at": sys_now, "attachments": [],
    }
    await manager.broadcast({"type": "msg_created", "data": sys_msg})

    async with get_db() as db:
        now2 = time.time()
        await db.execute(
            "INSERT INTO messages (id, conv_id, role, content, created_at, attachments) VALUES (?,?,?,?,?,?)",
            (core_msg_id, conv_id, "assistant", full_text, now2, "[]"),
        )
        await db.execute("UPDATE conversations SET updated_at=? WHERE id=?", (now2, conv_id))
        await db.commit()

    core_msg = {
        "id": core_msg_id, "conv_id": conv_id, "role": "assistant",
        "content": full_text, "created_at": now2, "attachments": [],
    }
    await manager.broadcast({"type": "msg_created", "data": core_msg})

    if sensor_tts:
        try:
            await sensor_tts.flush()
        except Exception:
            pass

    from routes.files import export_conversation
    await export_conversation(conv_id)

    core_log = {
        "timestamp": now2,
        "time": time.strftime("%H:%M:%S", time.localtime(now2)),
        "date": time.strftime("%Y-%m-%d", time.localtime(now2)),
        "monitoringlog": f"🧠 Core因传感器事件被唤醒并回复：{full_text[:80]}...",
        "call_core": False,
        "screenshot": "",
        "source": "sensor",
    }
    append_monitor_log(core_log)
    await manager.broadcast({"type": "monitor_log", "data": core_log})


async def _update_location_from_geofence(event: dict):
    """地理围栏事件更新 location_status.json 的 state 字段"""
    from location import load_location_status, save_location_status

    data = event["data"]
    zone = data.get("zone", "unknown")
    action = data.get("action", "enter")

    status = load_location_status()
    old_state = status.get("state", "unknown")

    if action == "enter":
        if zone == "home":
            new_state = "at_home"
        else:
            new_state = f"at_{zone}"
    else:
        new_state = "outside"

    status["state"] = new_state
    status["state_changed_at"] = time.time()
    save_location_status(status)

    log.info("地理围栏更新位置状态: %s → %s (zone=%s, action=%s)", old_state, new_state, zone, action)
