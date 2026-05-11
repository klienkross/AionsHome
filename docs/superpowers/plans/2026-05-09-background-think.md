# 背景思考（Background Think）实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 AI 在任意回复中可以附带 `[THINK:...]` 标签，后端静默执行思考任务（读日记、查记忆、看活动等），结果存储后在下次对话中自动注入上下文。同时支持 `[THINK_SCHEDULE:...]` 定时思考和 schedule 表的 repeat 重复日程。

**Architecture:** 复用现有 tag 解析 → 异步执行 → 存储的模式。思考执行用 sentinel（Gemini Flash-Lite）做一次轻量 AI 调用，根据指令关键词自动拉取相关数据（日记/记忆/活动）。定时思考复用 schedule 表，新增 `repeat` 字段和 `think` 类型。

**Tech Stack:** Python/FastAPI, aiosqlite, sentinel (Gemini Flash-Lite), 现有 obsidian/memory/activity 模块

---

## 文件变更清单

| 操作 | 文件 | 职责 |
|------|------|------|
| Modify | `aion-chat/database.py` | 新建 `background_thoughts` 表；`schedules` 表加 `repeat` 字段 |
| Modify | `aion-chat/routes/chat.py` | 添加 `[THINK:]` 正则 + 解析 + 异步触发；注入未用过的思考结果到对话上下文 |
| Modify | `aion-chat/schedule.py` | 添加 `[THINK_SCHEDULE:]` 指令解析；`_tick()` 支持 think 类型；`_fire_think()` 方法；repeat 逻辑 |

---

### Task 1: 数据库迁移 — background_thoughts 表 + schedules.repeat 字段

**Files:**
- Modify: `aion-chat/database.py:63-74`

- [ ] **Step 1: 在 `init_db()` 中添加 `background_thoughts` 表和 `schedules.repeat` 字段**

在 `database.py` 的 `init_db()` 函数中，`schedules` 表创建语句之后、`heart_whispers` 之前，添加：

```python
# ── 背景思考表 ──
await db.execute("""
    CREATE TABLE IF NOT EXISTS background_thoughts (
        id TEXT PRIMARY KEY,
        conv_id TEXT,
        msg_id TEXT,
        instruction TEXT NOT NULL,
        result TEXT NOT NULL,
        created_at REAL NOT NULL,
        used INTEGER DEFAULT 0
    )
""")
await db.execute("CREATE INDEX IF NOT EXISTS idx_bg_thoughts_used ON background_thoughts(used, created_at DESC)")
```

在 schedules 表后面的迁移区域添加 `repeat` 字段：

```python
try:
    await db.execute("ALTER TABLE schedules ADD COLUMN repeat TEXT DEFAULT NULL")
except:
    pass
```

- [ ] **Step 2: 验证迁移**

运行：`python -c "import asyncio; from database import init_db; asyncio.run(init_db()); print('OK')"`

确认无报错。

- [ ] **Step 3: Commit**

```
git add aion-chat/database.py
git commit -m "feat: 添加 background_thoughts 表和 schedules.repeat 字段"
```

---

### Task 2: [THINK:] tag 解析 + 异步执行 + 存储

**Files:**
- Modify: `aion-chat/routes/chat.py:24-48` (正则定义区)
- Modify: `aion-chat/routes/chat.py:1040-1160` (tag 检测区)
- Modify: `aion-chat/routes/chat.py:1254-1290` (异步任务派发区)
- Modify: `aion-chat/routes/chat.py` 末尾 (新增 `perform_background_think` 函数)

- [ ] **Step 1: 添加正则模式**

在 `chat.py` 顶部正则定义区（约 line 32 附近）添加：

```python
THINK_CMD_PATTERN = re.compile(r'\[THINK:([^\]]+)\]')
```

- [ ] **Step 2: 在 tag 检测区添加 [THINK:] 检测**

在 `_bg_generate()` 内，ORGANIZE_CMD 检测之后（约 line 1160 后），添加：

```python
# 检测 [THINK:xxx] 背景思考指令
think_matches = THINK_CMD_PATTERN.findall(full_text)
if think_matches:
    full_text = THINK_CMD_PATTERN.sub("", full_text).strip()
```

- [ ] **Step 3: 在异步任务派发区添加触发**

在 `asyncio.create_task(perform_activity_check(...))` 之后（约 line 1277 后），添加：

```python
if think_matches:
    for think_instr in think_matches:
        think_instr = think_instr.strip()
        if think_instr:
            asyncio.create_task(perform_background_think(conv_id, think_instr, ai_msg_id))
```

- [ ] **Step 4: 实现 `perform_background_think()` 函数**

在 `chat.py` 末尾（`perform_obsidian_check` 函数附近），添加：

```python
async def perform_background_think(conv_id: str, instruction: str, msg_id: str):
    """后台静默执行思考任务，结果存入 background_thoughts 表。"""
    from sentinel import call_sentinel_text
    from obsidian import read_recent, read_diary, search_diary
    from activity import get_activity_summary_for_prompt
    from memory_cards import search as search_memory_cards
    from datetime import date

    wb = load_worldbook()
    user_name = wb.get("user_name", "用户")

    # 根据指令关键词决定拉取哪些数据
    instr_lower = instruction.lower()
    data_parts = []

    # 日记
    if any(k in instr_lower for k in ("日记", "diary", "obsidian", "记录")):
        try:
            diary = await read_recent(3)
            data_parts.append(f"【最近3天日记摘要】\n{diary}")
        except Exception:
            pass

    # 活动
    if any(k in instr_lower for k in ("活动", "动态", "activity", "使用", "设备")):
        try:
            activity = get_activity_summary_for_prompt(6)
            if activity:
                data_parts.append(f"【最近1小时活动】\n{activity}")
        except Exception:
            pass

    # 记忆
    if any(k in instr_lower for k in ("记忆", "memory", "回忆", "回顾")):
        try:
            from memory import recall_memories
            recalled, _ = await recall_memories(instruction[:200])
            if recalled:
                mem_lines = "\n".join(f"- {m['content']}" for m in recalled[:10])
                data_parts.append(f"【相关记忆】\n{mem_lines}")
        except Exception:
            pass

    # 没命中任何关键词 → 默认拉日记
    if not data_parts:
        try:
            diary = await read_recent(3)
            data_parts.append(f"【最近3天日记摘要】\n{diary}")
        except Exception:
            pass

    data_block = "\n\n".join(data_parts) if data_parts else "（无可用数据）"

    prompt = (
        f"你是{user_name}的AI伴侣的后台思考引擎。\n"
        f"请根据以下指令和数据，进行分析思考，输出简洁的思考结论（200字以内）。\n"
        f"不要输出寒暄或格式标记，只输出思考结果。\n\n"
        f"【思考指令】{instruction}\n\n"
        f"【可用数据】\n{data_block}"
    )

    try:
        result = await call_sentinel_text(prompt, timeout=30)
    except Exception as e:
        print(f"[THINK] 思考执行失败: {e}")
        return

    if not result or not result.strip():
        return

    # 存入 background_thoughts 表
    thought_id = f"think_{int(time.time()*1000)}"
    now = time.time()
    async with get_db() as db:
        await db.execute(
            "INSERT INTO background_thoughts (id, conv_id, msg_id, instruction, result, created_at, used) VALUES (?,?,?,?,?,?,0)",
            (thought_id, conv_id, msg_id, instruction, result.strip(), now),
        )
        await db.commit()
    print(f"[THINK] 思考完成: {instruction[:30]}... → {result.strip()[:50]}...")
```

- [ ] **Step 5: 验证编译无误**

运行：`python -c "from routes.chat import router; print('OK')"`

- [ ] **Step 6: Commit**

```
git add aion-chat/routes/chat.py
git commit -m "feat: 添加 [THINK:] 背景思考 tag 解析和异步执行"
```

---

### Task 3: 思考结果注入对话上下文

**Files:**
- Modify: `aion-chat/routes/chat.py:970-1001` (背景记忆注入区)

- [ ] **Step 1: 在背景记忆注入区后面，添加背景思考结果注入**

在 `history.append({"role": "assistant", "content": "收到，我会自然地参考这些记忆。"})` 之后（约 line 1001），添加：

```python
# 6. 注入未使用的背景思考结果
async with get_db() as db:
    db.row_factory = __import__('aiosqlite').Row
    cur = await db.execute(
        "SELECT id, instruction, result FROM background_thoughts WHERE used=0 ORDER BY created_at DESC LIMIT 3"
    )
    thoughts = [dict(r) for r in await cur.fetchall()]
if thoughts:
    thought_lines = "\n".join(f"- 关于「{t['instruction'][:30]}」: {t['result']}" for t in thoughts)
    thought_block = f"[背景思考]\n你之前在后台思考过以下内容，可以在合适时机自然提起：\n{thought_lines}"
    history.append({"role": "user", "content": thought_block})
    history.append({"role": "assistant", "content": "收到，我会在合适时机自然提及。"})
    # 标记为已使用
    thought_ids = [t["id"] for t in thoughts]
    async with get_db() as db:
        await db.execute(
            f"UPDATE background_thoughts SET used=1 WHERE id IN ({','.join('?' * len(thought_ids))})",
            thought_ids,
        )
        await db.commit()
```

注意：这段代码要放在 `if body.fast_mode:` 的 else 分支里面，和背景记忆/RAG 召回在同一个块内。

- [ ] **Step 2: Commit**

```
git add aion-chat/routes/chat.py
git commit -m "feat: 将背景思考结果注入对话上下文"
```

---

### Task 4: Prompt 能力声明

**Files:**
- Modify: `aion-chat/routes/chat.py:821-854` (abilities 构建区)

- [ ] **Step 1: 在 abilities 列表中添加 THINK 说明**

在 `ORGANIZE` 那行 `abilities.append(...)` 之后，添加：

```python
abilities.append(f"[THINK:想法] — 当你想在后台默默思考一件事时使用（如回顾日记趋势、整理近期规律、分析{user_name}的状态变化）。结果不会发送给{user_name}，但你之后可以自然引用。例：[THINK:看看最近一周的日记，有没有什么值得关心的事]")
```

- [ ] **Step 2: Commit**

```
git add aion-chat/routes/chat.py
git commit -m "feat: 在系统能力中声明 [THINK:] 背景思考指令"
```

---

### Task 5: [THINK_SCHEDULE:] 定时思考指令

**Files:**
- Modify: `aion-chat/schedule.py:24-29` (正则定义区)
- Modify: `aion-chat/schedule.py:661-736` (`process_schedule_commands` 函数)

- [ ] **Step 1: 添加正则和指令解析**

在 `schedule.py` 正则定义区添加：

```python
THINK_SCHEDULE_CMD = re.compile(r"\[THINK_SCHEDULE:(\d{1,2}:\d{2})\|(\w+)\|(.+?)\]")
```

格式：`[THINK_SCHEDULE:HH:MM|daily|思考指令]`，其中 repeat 目前只支持 `daily`。

在 `process_schedule_commands()` 函数中，`SCHEDULE_LIST_CMD` 处理之前，添加：

```python
# [THINK_SCHEDULE:HH:MM|repeat|content]
for match in THINK_SCHEDULE_CMD.finditer(full_text):
    try:
        raw_time, repeat_type, content = match.group(1), match.group(2), match.group(3)
        # 构造今天或明天的触发时间
        now_dt = datetime.now()
        hour, minute = map(int, raw_time.split(":"))
        trigger_dt = now_dt.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if trigger_dt <= now_dt:
            trigger_dt = trigger_dt.replace(day=trigger_dt.day + 1)
        dt = trigger_dt.strftime("%Y-%m-%d %H:%M")
        if content.strip():
            await _add_schedule("think", dt, content.strip(), repeat=repeat_type if repeat_type != "once" else None)
            if conv_id:
                repeat_label = "（每天）" if repeat_type == "daily" else ""
                await _sys_msg(conv_id, f"{ai_name} 设置了 {raw_time} 的定时思考{repeat_label}：{content.strip()}")
    except Exception as e:
        log.error("THINK_SCHEDULE processing error: %s", e)
text = THINK_SCHEDULE_CMD.sub("", text)
```

- [ ] **Step 2: 修改 `_add_schedule()` 支持 repeat 参数**

```python
async def _add_schedule(stype: str, trigger_at: str, content: str, repeat: str = None):
    sid = f"sch_{int(time.time()*1000)}"
    now = time.time()
    trigger_at = trigger_at.replace("T", " ")
    async with get_db() as db:
        await db.execute(
            "INSERT INTO schedules (id, type, trigger_at, content, created_at, status, repeat) VALUES (?,?,?,?,?,?,?)",
            (sid, stype, trigger_at, content, now, "active", repeat),
        )
        await db.commit()
    await manager.broadcast({"type": "schedule_changed"})
```

- [ ] **Step 3: Commit**

```
git add aion-chat/schedule.py
git commit -m "feat: 添加 [THINK_SCHEDULE:] 定时思考指令解析"
```

---

### Task 6: ScheduleManager 支持 think 类型 + repeat 逻辑

**Files:**
- Modify: `aion-chat/schedule.py:108-125` (`_tick` 方法)
- Modify: `aion-chat/schedule.py` (新增 `_fire_think` 方法)

- [ ] **Step 1: 在 `_tick()` 中查询 think 类型日程**

在 `due_monitors` 查询之后添加：

```python
cur = await db.execute(
    "SELECT * FROM schedules WHERE status='active' AND type='think' AND trigger_at <= ?",
    (now_iso,),
)
due_thinks = [dict(r) for r in await cur.fetchall()]
```

在 `for item in due_monitors:` 循环之后添加：

```python
for item in due_thinks:
    await self._fire_think(item)
```

- [ ] **Step 2: 实现 `_fire_think()` 方法**

在 `_fire_monitor` 方法之后添加：

```python
async def _fire_think(self, item: dict):
    sid = item["id"]
    content = item["content"]
    trigger_at = item["trigger_at"]
    repeat = item.get("repeat")
    log.info("firing think %s: %s @%s", sid, content, trigger_at)

    if repeat == "daily":
        # 推到明天同一时间
        try:
            dt = datetime.strptime(trigger_at, "%Y-%m-%d %H:%M")
            next_dt = dt + __import__('datetime').timedelta(days=1)
            next_trigger = next_dt.strftime("%Y-%m-%d %H:%M")
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute(
                    "UPDATE schedules SET trigger_at=? WHERE id=?",
                    (next_trigger, sid),
                )
                await db.commit()
        except Exception as e:
            log.error("think repeat reschedule failed: %s", e)
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("UPDATE schedules SET status='triggered' WHERE id=?", (sid,))
                await db.commit()
    else:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("UPDATE schedules SET status='triggered' WHERE id=?", (sid,))
            await db.commit()

    await manager.broadcast({"type": "schedule_changed"})

    # 获取最新对话的 conv_id
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT id FROM conversations ORDER BY updated_at DESC LIMIT 1")
        conv = await cur.fetchone()
        if not conv:
            return
        conv_id = conv["id"]

    # 复用 chat.py 的 perform_background_think
    from routes.chat import perform_background_think
    await perform_background_think(conv_id, content, msg_id=None)
```

- [ ] **Step 3: Commit**

```
git add aion-chat/schedule.py
git commit -m "feat: ScheduleManager 支持 think 类型触发和 daily 重复日程"
```

---

### Task 7: Prompt 中声明 THINK_SCHEDULE + 在日程列表中展示

**Files:**
- Modify: `aion-chat/routes/chat.py:821-854` (abilities 区)
- Modify: `aion-chat/schedule.py` (`build_schedule_prompt` 函数)

- [ ] **Step 1: 在 chat.py abilities 中添加 THINK_SCHEDULE 说明**

在 THINK 能力说明之后添加：

```python
abilities.append("[THINK_SCHEDULE:HH:MM|daily|内容] — 设置每天定时思考。例：[THINK_SCHEDULE:22:00|daily|回顾今天的日记]")
```

- [ ] **Step 2: 在 `build_schedule_prompt()` 中支持 think 类型显示**

找到 `build_schedule_prompt` 函数，在类型标签映射中添加 think：

```python
type_labels = {"alarm": "闹铃", "reminder": "日程", "monitor": "定时监控", "think": "定时思考"}
```

如果有 repeat 字段，在展示时附加 `（每天）` 标记。

- [ ] **Step 3: Commit**

```
git add aion-chat/routes/chat.py aion-chat/schedule.py
git commit -m "feat: Prompt 声明定时思考能力并在日程列表中展示"
```

---

### Task 8: 端到端验证

- [ ] **Step 1: 启动服务器**

```
cd aion-chat && python main.py
```

- [ ] **Step 2: 测试 [THINK:] 即时思考**

发一条消息触发 AI 回复，手动在数据库插入一条测试：

```sql
INSERT INTO background_thoughts (id, conv_id, msg_id, instruction, result, created_at, used)
VALUES ('test1', 'conv_xxx', NULL, '测试思考', '这是测试结果', 1715200000, 0);
```

确认下一次发消息时，背景思考结果被注入到上下文中，且 `used` 被标记为 1。

- [ ] **Step 3: 测试定时思考**

```sql
INSERT INTO schedules (id, type, trigger_at, content, created_at, status, repeat)
VALUES ('sch_test', 'think', '2026-05-09 23:59', '看看今天日记', 1715200000, 'active', 'daily');
```

确认触发后 trigger_at 被推到次日，且 background_thoughts 表有新记录。

- [ ] **Step 4: Commit（如有修复）**
