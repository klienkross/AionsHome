# 传感器事件驱动环境感知系统 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 新建 `sensor.py` 模块，通过 MacroDroid webhook 接收手机传感器数据，实现事件累积窗口 + 地理围栏即时分析，补充现有摄像头监控。

**Architecture:** 新建 `sensor.py` 作为独立模块，注册到现有 webhook 管道的 `sensor` channel。低优事件累积 15 分钟后打包调 Sentinel 分析，地理围栏等高优事件立刻触发分析。分析结果写入现有 monitor_log 体系，地理围栏同时更新 `location_status.json`。

**Tech Stack:** Python 3.11+, asyncio, threading.Timer, 现有 sentinel.py / camera.py / location.py / webhook 管道

---

### Task 1: sensor.py — 事件接收与缓冲区

**Files:**
- Create: `aion-chat/sensor.py`

- [ ] **Step 1: 创建 sensor.py 骨架 — 事件接收与缓冲区管理**

```python
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
```

- [ ] **Step 2: 验证模块可导入**

Run: `cd D:\pyworks\AionsHome\aion-chat && python -c "import sensor; print('ok')"`
Expected: `ok`

- [ ] **Step 3: Commit**

```bash
git add aion-chat/sensor.py
git commit -m "feat: sensor.py 骨架 — 事件接收与缓冲区"
```

---

### Task 2: sensor.py — 窗口到期分析

**Files:**
- Modify: `aion-chat/sensor.py`

- [ ] **Step 1: 添加窗口到期处理与 Sentinel 分析逻辑**

在 `sensor.py` 末尾追加：

```python
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
```

- [ ] **Step 2: 验证模块可导入**

Run: `cd D:\pyworks\AionsHome\aion-chat && python -c "import sensor; print('ok')"`
Expected: `ok`

- [ ] **Step 3: Commit**

```bash
git add aion-chat/sensor.py
git commit -m "feat: sensor.py 窗口到期分析与 Sentinel 集成"
```

---

### Task 3: sensor.py — Core 唤醒与地理围栏位置更新

**Files:**
- Modify: `aion-chat/sensor.py`

- [ ] **Step 1: 添加 Core 唤醒逻辑和地理围栏位置更新**

在 `sensor.py` 末尾追加：

```python
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
```

- [ ] **Step 2: 验证模块可导入**

Run: `cd D:\pyworks\AionsHome\aion-chat && python -c "import sensor; print('ok')"`
Expected: `ok`

- [ ] **Step 3: Commit**

```bash
git add aion-chat/sensor.py
git commit -m "feat: sensor.py Core 唤醒 + 地理围栏位置更新"
```

---

### Task 4: webhook 注册 + event loop 初始化

**Files:**
- Modify: `aion-chat/routes/webhooks.py:23-25`
- Modify: `aion-chat/main.py`（startup 事件中初始化 sensor event loop）

- [ ] **Step 1: 在 webhooks.py 注册 sensor handler**

在 `aion-chat/routes/webhooks.py` 中，修改导入和 `_HANDLERS`：

```python
# 现有导入行之后追加
from sensor import handle_sensor_event

# _HANDLERS 字典中追加一行
_HANDLERS = {
    "phone-activity": handle_night_activity,
    "sensor": handle_sensor_event,
}
```

- [ ] **Step 2: 在 main.py startup 中初始化 sensor event loop**

找到 `main.py` 中的 startup 事件处理（`@app.on_event("startup")` 或 lifespan），在里面追加：

```python
import sensor
sensor.set_event_loop(asyncio.get_event_loop())
```

- [ ] **Step 3: 验证服务启动无报错**

Run: `cd D:\pyworks\AionsHome\aion-chat && python -c "from routes.webhooks import router; print('ok')"`
Expected: `ok`

- [ ] **Step 4: Commit**

```bash
git add aion-chat/routes/webhooks.py aion-chat/main.py
git commit -m "feat: 注册 sensor webhook channel + event loop 初始化"
```

---

### Task 5: location.py 小改 — 支持围栏 state 和关闭状态输出

**Files:**
- Modify: `aion-chat/location.py:235-269`（`format_location_for_prompt` 函数）

- [ ] **Step 1: 修改 format_location_for_prompt**

将 `aion-chat/location.py` 中的 `format_location_for_prompt` 函数替换为：

```python
def format_location_for_prompt() -> str:
    """格式化当前位置状态，供哨兵/Core prompt 使用"""
    status = load_location_status()
    if status.get("state") == "unknown" or status.get("updated_at", 0) == 0:
        # 即使位置系统关闭，如果有围栏推送的 state 也输出
        state = status.get("state", "unknown")
        if state != "unknown":
            state_label = _resolve_state_label(state)
            return f"当前位置状态：{state_label}"
        return ""

    cfg = load_location_config()

    # 位置系统关闭时，仍输出围栏推送的 state（但不输出坐标/地址/天气）
    if not cfg.get("enabled"):
        state = status.get("state", "unknown")
        if state != "unknown":
            state_label = _resolve_state_label(state)
            return f"当前位置状态：{state_label}"
        return ""

    lines = []
    state_label = _resolve_state_label(status["state"])
    lines.append(f"当前位置状态：{state_label}")

    if status.get("address"):
        lines.append(f"当前位置：{status['address']}")

    if status.get("distance_from_home", 0) > 0:
        d = status["distance_from_home"]
        d_str = f"{d / 1000:.1f}km" if d >= 1000 else f"{int(d)}m"
        lines.append(f"距离家：{d_str}")

    w = status.get("weather", {})
    if w:
        weather_text = f"天气：{w.get('weather', '')} {w.get('temperature', '')}°C"
        if w.get("humidity"):
            weather_text += f" 湿度{w['humidity']}%"
        if w.get("winddirection"):
            weather_text += f" {w['winddirection']}风{w.get('windpower', '')}级"
        lines.append(weather_text)

    if status.get("updated_at"):
        lines.append(f"位置更新时间：{time.strftime('%H:%M:%S', time.localtime(status['updated_at']))}")

    return "\n".join(lines)


def _resolve_state_label(state: str) -> str:
    """将 state 字段转为中文标签，支持自定义 zone 名"""
    known = {"at_home": "在家", "outside": "外出中", "unknown": "未知"}
    if state in known:
        return known[state]
    if state.startswith("at_"):
        zone = state[3:]
        zone_labels = {
            "office": "在公司", "gym": "在健身房", "school": "在学校",
        }
        return zone_labels.get(zone, f"在{zone}")
    return state
```

- [ ] **Step 2: 验证函数正常**

Run: `cd D:\pyworks\AionsHome\aion-chat && python -c "from location import format_location_for_prompt, _resolve_state_label; print(_resolve_state_label('at_home'), _resolve_state_label('at_gym'), _resolve_state_label('at_office'), _resolve_state_label('outside'))"`
Expected: `在家 在健身房 在公司 外出中`

- [ ] **Step 3: Commit**

```bash
git add aion-chat/location.py
git commit -m "feat: location 支持围栏 state 标签 + 关闭状态仍输出围栏数据"
```

---

### Task 6: 端到端手动测试

**Files:**
- No new files

- [ ] **Step 1: 启动服务**

Run: `cd D:\pyworks\AionsHome\aion-chat && python main.py`

- [ ] **Step 2: 测试低优事件缓冲**

用 curl 或 PowerShell 发送测试请求：

```powershell
$body = '{"event":"screen","data":{"state":"on"}}' 
Invoke-RestMethod -Uri "http://localhost:8000/api/webhooks/sensor" -Method POST -Body $body -ContentType "application/json"
```

Expected: 返回 `{"ok":true,"channel":"sensor","handler":{"buffered":true,"event":"screen","buffer_size":1}}`

- [ ] **Step 3: 测试地理围栏高优事件**

```powershell
$body = '{"event":"geofence","data":{"zone":"home","action":"enter"}}'
Invoke-RestMethod -Uri "http://localhost:8000/api/webhooks/sensor" -Method POST -Body $body -ContentType "application/json"
```

Expected: 返回 `{"ok":true,"channel":"sensor","handler":{"triggered":true,...}}`，同时 monitor_log 出现新条目，`location_status.json` 的 state 变为 `at_home`。

- [ ] **Step 4: 检查 monitor-logs 页面**

打开 `http://localhost:8000/monitor-logs.html`，确认传感器分析日志（带 📱 前缀）正常显示。

- [ ] **Step 5: Commit（如有修复）**

```bash
git add -A
git commit -m "fix: 端到端测试修复"
```
