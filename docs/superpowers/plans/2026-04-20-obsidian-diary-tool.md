# Obsidian 日记查看功能 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 bot 能通过 `[OBSIDIAN_READ:]`、`[OBSIDIAN_RECENT:]`、`[OBSIDIAN_SEARCH:]` 三条命令主动读取本地 Obsidian 日记，结果注入上下文后由 AI 二次回复。

**Architecture:** 新建 `aion-chat/obsidian.py` 提供三个读取函数和一个 flash-lite 摘要函数；在 `routes/chat.py` 按现有 POI_SEARCH / 查看动态模式，在三处 abilities 块和三处命令检测块中插入对应逻辑，并新增 `perform_obsidian_check()` 异步函数处理二次 AI 回复。

**Tech Stack:** Python asyncio, httpx (已有), Gemini flash-lite (已用于 instant_digest), aiosqlite (已有)

---

## 文件变更清单

| 操作 | 文件 | 说明 |
|------|------|------|
| 新建 | `aion-chat/obsidian.py` | 读取/搜索/摘要逻辑 |
| 修改 | `aion-chat/routes/chat.py` | 加 3 条 pattern、3 处检测、3 处 ability hint、1 个 perform 函数 |
| 修改 | `aion-chat/data/settings.json` | 加 `obsidian_vault_path` |

---

## Task 1: 创建 `obsidian.py` — 核心读取函数

**Files:**
- Create: `aion-chat/obsidian.py`

- [ ] **Step 1: 写验证脚本**

新建临时文件 `aion-chat/_test_obsidian.py`：

```python
import asyncio, sys, os
sys.path.insert(0, os.path.dirname(__file__))

# 测试前先在 data/settings.json 确保有 obsidian_vault_path 字段
# 或直接 monkey-patch：
import config
config.SETTINGS["obsidian_vault_path"] = "C:/Users/你的用户名/Obsidian/日记"  # 改成实际路径

from obsidian import read_diary, search_diary

async def main():
    # 测试1: 读不存在的日期
    r = await read_diary("1900-01-01")
    assert "暂无日记" in r, f"期望'暂无日记'，得到: {r}"
    print("✓ 读不存在日期: OK")

    # 测试2: vault 路径不存在
    config.SETTINGS["obsidian_vault_path"] = "/不存在的路径"
    r = await read_diary("2026-04-20")
    assert "不存在" in r or "未配置" in r, f"期望错误提示，得到: {r}"
    print("✓ 路径不存在: OK")

    # 测试3: 未配置路径
    config.SETTINGS["obsidian_vault_path"] = ""
    r = await read_diary("2026-04-20")
    assert "未配置" in r, f"期望'未配置'，得到: {r}"
    print("✓ 路径未配置: OK")

    print("\n所有测试通过")

asyncio.run(main())
```

- [ ] **Step 2: 运行验证脚本确认它会失败**

```bash
cd aion-chat && python _test_obsidian.py
```

期望：`ModuleNotFoundError: No module named 'obsidian'`

- [ ] **Step 3: 创建 `aion-chat/obsidian.py`**

```python
"""
Obsidian 日记读取：read_diary / read_recent / search_diary / summarize_diary
"""

import json
from datetime import date, timedelta
from pathlib import Path

import httpx

from config import SETTINGS, get_key


def _vault() -> Path | None:
    p = SETTINGS.get("obsidian_vault_path", "").strip()
    return Path(p) if p else None


async def read_diary(date_str: str) -> str:
    vault = _vault()
    if not vault:
        return "Obsidian 日记路径未配置，请在 settings.json 中添加 obsidian_vault_path。"
    if not vault.exists():
        return f"日记目录不存在，请检查路径配置：{vault}"
    f = vault / f"{date_str}.md"
    if not f.exists():
        return f"{date_str} 暂无日记。"
    return f.read_text(encoding="utf-8")


async def search_diary(keyword: str) -> str:
    vault = _vault()
    if not vault:
        return "Obsidian 日记路径未配置，请在 settings.json 中添加 obsidian_vault_path。"
    if not vault.exists():
        return f"日记目录不存在，请检查路径配置：{vault}"
    keyword_lower = keyword.lower()
    hits = []
    for md in sorted(vault.glob("*.md"), reverse=True):
        try:
            lines = md.read_text(encoding="utf-8").splitlines()
        except Exception:
            continue
        matched = [l for l in lines if keyword_lower in l.lower()]
        if matched:
            hits.append(f"📅 {md.stem}\n" + "\n".join(f"  {l.strip()}" for l in matched[:3]))
        if len(hits) >= 10:
            break
    if not hits:
        return f"未找到含「{keyword}」的日记。"
    return f"搜索「{keyword}」共找到 {len(hits)} 篇日记：\n\n" + "\n\n".join(hits)


async def summarize_diary(date_str: str, content: str) -> str:
    """调用 Gemini flash-lite 提取日记实质内容（跳过模板头），失败则降级截取。"""
    gemini_key = get_key("gemini_free")
    if not gemini_key:
        return _fallback_summary(content)
    prompt = (
        f"以下是 {date_str} 的日记，可能有固定模板头部（如天气、习惯打卡等）。"
        f"请跳过模板内容，用100字以内提取今天实际发生的事和心情。若无实质内容则回复"（无记录）"。\n\n{content}"
    )
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"gemini-2.0-flash-lite:generateContent?key={gemini_key}"
    )
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(url, json={"contents": [{"role": "user", "parts": [{"text": prompt}]}]})
            resp.raise_for_status()
            return resp.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
    except Exception:
        return _fallback_summary(content)


def _fallback_summary(content: str) -> str:
    """摘要失败时降级：跳过开头连续的短行（模板行），取第一段实质内容。"""
    lines = content.splitlines()
    result = []
    skipping = True
    for line in lines:
        stripped = line.strip()
        if skipping and (not stripped or len(stripped) < 20 or stripped.startswith("#")):
            continue
        skipping = False
        result.append(stripped)
        if len(" ".join(result)) > 200:
            break
    return " ".join(result)[:200] if result else content[:200]


async def read_recent(n: int) -> str:
    vault = _vault()
    if not vault:
        return "Obsidian 日记路径未配置，请在 settings.json 中添加 obsidian_vault_path。"
    if not vault.exists():
        return f"日记目录不存在，请检查路径配置：{vault}"
    n = max(1, min(14, n))
    today = date.today()
    entries = []
    for i in range(n):
        d = today - timedelta(days=i)
        date_str = d.strftime("%Y-%m-%d")
        f = vault / f"{date_str}.md"
        if not f.exists():
            continue
        content = f.read_text(encoding="utf-8")
        summary = await summarize_diary(date_str, content)
        entries.append(f"📅 {date_str}：{summary}")
    if not entries:
        return f"最近 {n} 天暂无日记。"
    return f"最近 {n} 天日记摘要：\n\n" + "\n\n".join(entries)
```

- [ ] **Step 4: 运行验证脚本**

```bash
cd aion-chat && python _test_obsidian.py
```

期望输出：
```
✓ 读不存在日期: OK
✓ 路径不存在: OK
✓ 路径未配置: OK

所有测试通过
```

- [ ] **Step 5: 删除临时测试文件并提交**

```bash
rm aion-chat/_test_obsidian.py
cd d:/pyworks/AionsHome
git add aion-chat/obsidian.py
git commit -m "feat: 新增 obsidian.py — 日记读取/搜索/摘要核心模块"
```

---

## Task 2: 在 `settings.json` 添加 vault 路径

**Files:**
- Modify: `aion-chat/data/settings.json`

- [ ] **Step 1: 添加 `obsidian_vault_path` 字段**

打开 `aion-chat/data/settings.json`，在现有 JSON 对象中添加（路径改成实际 Obsidian 日记文件夹）：

```json
"obsidian_vault_path": "D:/Obsidian/Daily Notes"
```

（注意：Windows 路径用正斜杠 `/` 或双反斜杠 `\\`）

- [ ] **Step 2: 验证 Python 能读到**

```bash
cd aion-chat && python -c "from config import SETTINGS; print(SETTINGS.get('obsidian_vault_path'))"
```

期望输出：你填写的路径字符串（非 None / 空）

> ⚠️ `settings.json` 在 `.gitignore` 中，不会被提交，无需 commit。

---

## Task 3: 在 `routes/chat.py` 顶部添加 3 条正则和 import

**Files:**
- Modify: `aion-chat/routes/chat.py`（顶部 pattern 区块）

- [ ] **Step 1: 添加 import 和 3 条正则**

在 `routes/chat.py` 第 38-40 行附近（`POI_SEARCH_PATTERN` 定义处）**之后**插入：

```python
OBSIDIAN_READ_PATTERN   = re.compile(r'\[OBSIDIAN_READ:(\d{4}-\d{2}-\d{2})\]')
OBSIDIAN_RECENT_PATTERN = re.compile(r'\[OBSIDIAN_RECENT:(\d+)\]')
OBSIDIAN_SEARCH_PATTERN = re.compile(r'\[OBSIDIAN_SEARCH:([^\]]+)\]')
```

- [ ] **Step 2: 验证语法无误**

```bash
cd aion-chat && python -c "from routes.chat import OBSIDIAN_READ_PATTERN, OBSIDIAN_RECENT_PATTERN, OBSIDIAN_SEARCH_PATTERN; print('OK')"
```

期望：`OK`

- [ ] **Step 3: 提交**

```bash
cd d:/pyworks/AionsHome
git add aion-chat/routes/chat.py
git commit -m "feat: chat.py 添加 OBSIDIAN 三条命令正则"
```

---

## Task 4: 在 `chat.py` 中 3 处命令检测块插入 Obsidian 检测

**Files:**
- Modify: `aion-chat/routes/chat.py`（3 处 streaming 结束后的命令检测区）

命令检测发生在 streaming 结束后、插入 DB 之前。共有 3 处（行约 481、954、1733），每处都需要添加相同的 3 行检测代码。

每处找到 `poi_matches = POI_SEARCH_PATTERN.findall(full_text)` 这行，在其**正上方**插入：

```python
            # 检测 Obsidian 日记指令
            obsidian_read_match = OBSIDIAN_READ_PATTERN.search(full_text)
            obsidian_recent_match = OBSIDIAN_RECENT_PATTERN.search(full_text)
            obsidian_search_match = OBSIDIAN_SEARCH_PATTERN.search(full_text)
            if obsidian_read_match:
                full_text = OBSIDIAN_READ_PATTERN.sub("", full_text).strip()
            if obsidian_recent_match:
                full_text = OBSIDIAN_RECENT_PATTERN.sub("", full_text).strip()
            if obsidian_search_match:
                full_text = OBSIDIAN_SEARCH_PATTERN.sub("", full_text).strip()
```

每处找到 `if poi_matches:` 触发块之后（`asyncio.create_task(perform_poi_check(...))` 那行之后），添加：

```python
            # [OBSIDIAN_*] 日记查看 → 读取内容后自动追加一轮回复
            if obsidian_read_match or obsidian_recent_match or obsidian_search_match:
                asyncio.create_task(perform_obsidian_check(
                    conv_id, model_key,
                    obsidian_read_match.group(1) if obsidian_read_match else None,
                    int(obsidian_recent_match.group(1)) if obsidian_recent_match else None,
                    obsidian_search_match.group(1) if obsidian_search_match else None,
                ))
```

- [ ] **Step 1: 在第 1 处（约行 481）添加检测代码（共 2 段，见上）**

- [ ] **Step 2: 在第 2 处（约行 954）添加检测代码**

- [ ] **Step 3: 在第 3 处（约行 1733）添加检测代码**

- [ ] **Step 4: 验证语法**

```bash
cd aion-chat && python -c "import routes.chat; print('OK')"
```

期望：`OK`（无报错）

- [ ] **Step 5: 提交**

```bash
cd d:/pyworks/AionsHome
git add aion-chat/routes/chat.py
git commit -m "feat: chat.py 3 处命令检测块添加 OBSIDIAN 指令检测"
```

---

## Task 5: 新增 `perform_obsidian_check()` 函数

**Files:**
- Modify: `aion-chat/routes/chat.py`（在 `perform_activity_check` 函数定义之后追加）

- [ ] **Step 1: 在 `perform_activity_check` 函数末尾之后（约行 1470 附近）追加新函数**

先确认 `perform_activity_check` 结束的行号：

```bash
cd aion-chat && grep -n "def perform_activity_check\|def perform_poi_check\|^# ──" routes/chat.py | grep -A2 "perform_activity"
```

在该函数末尾（`print(...)` 或 `export_conversation(...)` 后）追加：

```python

# ── [OBSIDIAN_*] 日记查看 → 读取内容后自动追加一轮 Core 回复 ─────
async def perform_obsidian_check(
    conv_id: str,
    model_key: str,
    read_date: str | None,
    recent_n: int | None,
    search_kw: str | None,
):
    from obsidian import read_diary, read_recent, search_diary

    wb = load_worldbook()
    user_name = wb.get("user_name", "用户")
    ai_name = wb.get("ai_name", "AI")

    # 1. 获取日记内容
    if read_date:
        diary_text = await read_diary(read_date)
        sys_label = f"{ai_name}查看了{user_name} {read_date} 的日记"
        prompt_hint = f"你刚才查看了{user_name} {read_date} 的日记，内容如下：\n\n{diary_text}\n\n请根据日记内容自然地和{user_name}聊聊，不要再说\"让我看一下\"之类的话，直接根据内容回应。"
    elif recent_n:
        recent_n = max(1, min(14, recent_n))
        diary_text = await read_recent(recent_n)
        sys_label = f"{ai_name}查看了{user_name}最近{recent_n}天的日记"
        prompt_hint = f"你刚才查看了{user_name}最近{recent_n}天的日记摘要：\n\n{diary_text}\n\n请根据这些摘要自然地和{user_name}聊聊，不要再说\"让我看一下\"之类的话，直接根据内容回应。"
    elif search_kw:
        diary_text = await search_diary(search_kw)
        sys_label = f"{ai_name}搜索了{user_name}日记中含「{search_kw}」的内容"
        prompt_hint = f"你刚才搜索了{user_name}日记中含「{search_kw}」的内容，搜索结果如下：\n\n{diary_text}\n\n请根据搜索结果自然地和{user_name}聊聊，不要再说\"让我搜一下\"之类的话，直接根据内容回应。"
    else:
        return

    # 2. 构建 prefix（人设 + 系统提示）
    prefix = []
    if wb.get("ai_persona"):
        prefix.append({"role": "user", "content": f"[系统设定 - AI人设]\n{wb['ai_persona']}"})
        prefix.append({"role": "assistant", "content": "收到，我会按照设定扮演角色。"})
    if wb.get("user_persona"):
        prefix.append({"role": "user", "content": f"[系统设定 - 用户信息]\n{wb['user_persona']}"})
        prefix.append({"role": "assistant", "content": "收到，我会记住你的信息。"})
    if wb.get("system_prompt"):
        prefix.append({"role": "user", "content": f"[系统提示]\n{wb['system_prompt']}"})
        prefix.append({"role": "assistant", "content": "收到，我会遵循这些规则。"})

    # 3. 取最近对话上下文
    import aiosqlite
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT role, content FROM messages WHERE conv_id=? AND role IN ('user','assistant') ORDER BY created_at DESC LIMIT 6",
            (conv_id,)
        )
        rows = await cur.fetchall()
    recent = [{"role": r["role"], "content": r["content"], "attachments": []} for r in reversed(rows)]

    messages = prefix + recent + [{"role": "user", "content": prompt_hint}]

    # 4. 预生成 msg_id + TTS
    msg_id = f"msg_{int(time.time()*1000)}_obs"
    obs_tts = None
    if manager.any_tts_enabled():
        tts_voice = manager.get_tts_voice()
        if tts_voice:
            obs_tts = TTSStreamer(msg_id, tts_voice, manager)

    # 5. 流式调用 AI
    full_text = ""
    try:
        _temp = SETTINGS.get("temperature")
        async for chunk in stream_ai(messages, model_key, temperature=_temp):
            full_text += chunk
            if obs_tts:
                obs_tts.feed(chunk)
    except Exception as e:
        full_text = f"[日记读取完成但回复生成失败] {e}"

    if not full_text.strip():
        return

    # 6. 插入 system 提示消息 + AI 回复
    sys_now = time.time()
    sys_msg_id = f"msg_{int(sys_now*1000)}_obs_sys"
    async with get_db() as db:
        await db.execute(
            "INSERT INTO messages (id, conv_id, role, content, created_at, attachments) VALUES (?,?,?,?,?,?)",
            (sys_msg_id, conv_id, "system", sys_label, sys_now, "[]")
        )
        await db.commit()
    sys_msg = {"id": sys_msg_id, "conv_id": conv_id, "role": "system",
               "content": sys_label, "created_at": sys_now, "attachments": []}
    await manager.broadcast({"type": "msg_created", "data": sys_msg})

    now = time.time()
    async with get_db() as db:
        await db.execute(
            "INSERT INTO messages (id, conv_id, role, content, created_at, attachments) VALUES (?,?,?,?,?,?)",
            (msg_id, conv_id, "assistant", full_text, now, "[]")
        )
        await db.execute("UPDATE conversations SET updated_at=? WHERE id=?", (now, conv_id))
        await db.commit()

    ai_msg = {"id": msg_id, "conv_id": conv_id, "role": "assistant",
              "content": full_text, "created_at": now, "attachments": []}
    await manager.broadcast({"type": "msg_created", "data": ai_msg})
    if obs_tts:
        try:
            await obs_tts.flush()
        except Exception:
            pass
    await export_conversation(conv_id)
    print(f"[OBSIDIAN] 日记读取完成，已自动追加回复")
```

- [ ] **Step 2: 验证语法**

```bash
cd aion-chat && python -c "import routes.chat; print('OK')"
```

期望：`OK`

- [ ] **Step 3: 提交**

```bash
cd d:/pyworks/AionsHome
git add aion-chat/routes/chat.py
git commit -m "feat: chat.py 添加 perform_obsidian_check 函数"
```

---

## Task 6: 在 3 处 abilities 块添加 Obsidian 能力提示

**Files:**
- Modify: `aion-chat/routes/chat.py`（3 处 `abilities = []` 块）

3 处 abilities 块分别在约行 309、712、1537。每处找到 `abilities.append(f"[MEMORY:内容]` 那行，在其**正前面**插入：

```python
    if SETTINGS.get("obsidian_vault_path"):
        abilities.append(f"[OBSIDIAN_READ:YYYY-MM-DD] — 查看{user_name}指定日期的Obsidian日记全文。[OBSIDIAN_RECENT:N] — 查看最近N天日记摘要（N最大14）。[OBSIDIAN_SEARCH:关键词] — 搜索日记中含某关键词的内容（最多返回10篇）。当{user_name}提到日记、某天发生的事、想回顾过去时主动使用，使用后系统会自动读取并将内容发给你，查看前不要编造内容。")
```

- [ ] **Step 1: 在第 1 处（约行 333，`[MEMORY:]` 前面）添加**

- [ ] **Step 2: 在第 2 处（约行 734，`[MEMORY:]` 前面）添加**

- [ ] **Step 3: 在第 3 处（约行 1563，`[MEMORY:]` 前面）添加**

- [ ] **Step 4: 验证语法**

```bash
cd aion-chat && python -c "import routes.chat; print('OK')"
```

期望：`OK`

- [ ] **Step 5: 提交**

```bash
cd d:/pyworks/AionsHome
git add aion-chat/routes/chat.py
git commit -m "feat: chat.py 3 处 abilities 块添加 Obsidian 能力提示"
```

---

## Task 7: 端对端手动测试

- [ ] **Step 1: 启动服务**

```bash
cd aion-chat && python main.py
```

确认启动无报错。

- [ ] **Step 2: 测试 OBSIDIAN_READ**

在聊天界面发送："帮我看看今天的日记" 或 "看看4月20号的日记"。

期望：
1. bot 回复中无 `[OBSIDIAN_READ:...]` 文字（命令已被清除）
2. 聊天界面出现 system 消息："xxx查看了xxx 2026-04-20 的日记"
3. bot 追加一条根据日记内容的回复

- [ ] **Step 3: 测试 OBSIDIAN_RECENT**

发送："帮我回顾一下最近一周的日记"。

期望：
1. system 消息："xxx查看了xxx最近7天的日记"
2. bot 追加摘要回复（每篇用 flash-lite 提取实质内容）

- [ ] **Step 4: 测试 OBSIDIAN_SEARCH**

发送："帮我搜一下日记里提到跑步的内容"。

期望：
1. system 消息："xxx搜索了xxx日记中含「跑步」的内容"
2. bot 追加搜索结果回复

- [ ] **Step 5: 测试错误情况**

将 `settings.json` 中 `obsidian_vault_path` 改为空字符串，重启服务，发送日记相关消息。

期望：bot 回复包含"路径未配置"提示，不崩溃。

改回正确路径后重启验证恢复正常。
