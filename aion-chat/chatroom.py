"""
聊天室核心逻辑：Connor 代理调用、跨窗口上下文构建、AI 互聊控制、聊天室记忆管理
"""

import json, time, struct, asyncio, shutil
from typing import Optional
from pathlib import Path

import aiosqlite, httpx

from config import DATA_DIR, DEFAULT_MODEL, SETTINGS, load_worldbook
from database import get_db
from memory import get_embedding, cosine_similarity, _pack_embedding, _unpack_embedding, _keyword_match_score
from ai_providers import call_claude_cli, CLI_STATUS_PREFIX, _build_cli_prompt
from context_builder import build_ability_block, build_memory_blocks, fetch_merged_timeline, render_merged_timeline

# ── Connor-Codex 服务配置 ──
CHATROOM_CONFIG_PATH = DATA_DIR / "chatroom_config.json"

_DEFAULT_CONFIG = {
    "connor_url": "http://127.0.0.1:8787",
    "connor_poll_interval": 1.0,
    "connor_poll_timeout": 480,  # 8 分钟，与 Connor 后端 CODEX_TIMEOUT_MS 保持一致
}


def load_chatroom_config() -> dict:
    if CHATROOM_CONFIG_PATH.exists():
        try:
            return {**_DEFAULT_CONFIG, **json.loads(CHATROOM_CONFIG_PATH.read_text(encoding="utf-8"))}
        except Exception:
            pass
    return dict(_DEFAULT_CONFIG)


def save_chatroom_config(data: dict):
    CHATROOM_CONFIG_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# ══════════════════════════════════════════════════
#  Connor 代理调用
# ══════════════════════════════════════════════════

async def send_to_connor(text: str, images: list[dict] = None) -> Optional[str]:
    """发送消息给 Connor-Codex 服务并通过 SSE /api/stream 等待回复。
    只有 POST 失败或 health 检测失败才返回 None（代表服务不可用）。
    任务超时（8分钟）仍返回 None，调用方可据此提示"任务仍在处理"。
    """
    cfg = load_chatroom_config()
    base = cfg["connor_url"].rstrip("/")
    timeout = cfg.get("connor_poll_timeout", 480)

    # 1. 发送用户消息，拿到 task_id
    payload = {"text": text}
    if images:
        payload["images"] = images
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(f"{base}/api/messages", json=payload)
            if resp.status_code != 200:
                return None
            sent = resp.json().get("message", {})
            task_id = sent.get("id")
    except Exception:
        return None

    # 2. 连接 SSE /api/stream，监听 message 事件等待 assistant 回复
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10, read=timeout + 30)) as client:
            async with client.stream("GET", f"{base}/api/stream") as sse_resp:
                buffer = ""
                deadline = asyncio.get_event_loop().time() + timeout
                async for raw_bytes in sse_resp.aiter_bytes():
                    buffer += raw_bytes.decode("utf-8", errors="replace")
                    # 按 SSE 协议解析：事件以双换行分隔
                    while "\n\n" in buffer:
                        block, buffer = buffer.split("\n\n", 1)
                        event_type = ""
                        data_lines = []
                        for line in block.split("\n"):
                            if line.startswith("event: "):
                                event_type = line[7:].strip()
                            elif line.startswith("data: "):
                                data_lines.append(line[6:])
                        if not data_lines:
                            continue
                        try:
                            data = json.loads("".join(data_lines))
                        except (json.JSONDecodeError, ValueError):
                            continue

                        # 监听 message 事件：匹配 taskId 的 assistant 回复
                        if event_type == "message" and data.get("role") == "assistant":
                            if data.get("taskId") == task_id:
                                return data.get("text", "")

                    # 检查超时
                    if asyncio.get_event_loop().time() > deadline:
                        return _CONNOR_TIMEOUT_SENTINEL
    except Exception:
        pass

    # 3. SSE 连接断开后，回退到单次查询，可能任务已经完成了
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"{base}/api/messages")
            msgs = resp.json().get("messages", [])
            for m in reversed(msgs):
                if m.get("role") == "assistant" and m.get("taskId") == task_id:
                    if m.get("status") != "running":
                        return m.get("text", "")
    except Exception:
        pass

    return _CONNOR_TIMEOUT_SENTINEL


# 超时哨兵值：区分"服务不可用(None)"和"任务仍在处理(超时)"
_CONNOR_TIMEOUT_SENTINEL = "__CONNOR_STILL_PROCESSING__"


async def check_connor_online() -> bool:
    """检查 Connor 是否可用（Claude CLI 存在即视为在线）"""
    return shutil.which("claude") is not None


# ══════════════════════════════════════════════════
#  Connor Codex CLI 直接调用
# ══════════════════════════════════════════════════

_CONNOR_PERSONA_PATH = Path(__file__).parent.parent / "Connor-Codex" / "persona.md"


def get_connor_name() -> str:
    return SETTINGS.get("connor_name", "Connor")


def _read_connor_persona() -> str:
    """读取 Connor 的人设文件"""
    if _CONNOR_PERSONA_PATH.exists():
        return _CONNOR_PERSONA_PATH.read_text(encoding="utf-8").strip()
    return ""


def _build_connor_messages(prompt: str) -> list[dict]:
    """将 Connor prompt 包装为 messages 列表，注入 persona 作为 system"""
    persona = _read_connor_persona()
    messages = []
    if persona:
        messages.append({"role": "system", "content": persona})
    messages.append({"role": "user", "content": prompt})
    return messages


async def stream_connor_cli(prompt: str = None, *, messages: list[dict] = None):
    """流式调用 Codex CLI 获取 Connor 回复，yield text chunks 和 CLI_STATUS_PREFIX 状态。
    可传入纯文本 prompt（旧方式）或完整 messages 列表（保留附件图片）。"""
    if messages is None:
        messages = _build_connor_messages(prompt)
    else:
        # 注入 persona 作为 system（如果 messages 中没有）
        if not any(m["role"] == "system" for m in messages):
            persona = _read_connor_persona()
            if persona:
                messages = [{"role": "system", "content": persona}] + messages
    async for chunk in call_claude_cli(messages, "", None):
        yield chunk


async def simple_connor_cli_call(prompt: str) -> Optional[str]:
    """非流式调用 Codex CLI，返回完整回复文本（用于记忆总结等）"""
    full_text = ""
    async for chunk in stream_connor_cli(prompt):
        if not chunk.startswith(CLI_STATUS_PREFIX):
            full_text += chunk
    return full_text.strip() or None


# ══════════════════════════════════════════════════
#  跨窗口上下文构建
# ══════════════════════════════════════════════════

async def get_main_chat_recent(minutes: int = 30, limit: int = 40) -> list[dict]:
    """从主聊天获取近 N 分钟的消息"""
    cutoff = time.time() - minutes * 60
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT role, content, created_at FROM messages "
            "WHERE created_at > ? AND role IN ('user', 'assistant') "
            "ORDER BY created_at DESC LIMIT ?",
            (cutoff, limit),
        )
        rows = await cur.fetchall()
    return [dict(r) for r in reversed(rows)]


async def get_connor_1v1_recent(minutes: int = 30, limit: int = 40) -> list[dict]:
    """从 Connor 1v1 聊天室获取近 N 分钟的消息"""
    cutoff = time.time() - minutes * 60
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        # 找到 connor_1v1 类型的房间
        cur = await db.execute(
            "SELECT id FROM chatroom_rooms WHERE type = 'connor_1v1' ORDER BY created_at ASC LIMIT 1"
        )
        room = await cur.fetchone()
        if not room:
            return []
        cur = await db.execute(
            "SELECT sender, content, created_at FROM chatroom_messages "
            "WHERE room_id = ? AND created_at > ? "
            "ORDER BY created_at DESC LIMIT ?",
            (room["id"], cutoff, limit),
        )
        rows = await cur.fetchall()
    return [dict(r) for r in reversed(rows)]


def format_cross_context(messages: list[dict], label: str) -> str:
    """将跨窗口消息格式化为上下文文本"""
    if not messages:
        return ""
    wb = load_worldbook()
    _ai = wb.get("ai_name", "Aion")
    _name_map = {"user": wb.get("user_name", "用户"), "assistant": _ai, "aion": _ai, "connor": get_connor_name()}
    lines = [f"[{label} - 近期对话摘要]"]
    for m in messages:
        ts = time.strftime("%H:%M", time.localtime(m.get("created_at", 0)))
        role = m.get("role") or m.get("sender", "unknown")
        name = _name_map.get(role, role)
        text = (m.get("content") or "")[:300]
        lines.append(f"  [{ts}] {name}: {text}")
    return "\n".join(lines)


# ══════════════════════════════════════════════════
#  聊天室记忆系统
# ══════════════════════════════════════════════════

async def recall_chatroom_memories(
    query_text: str,
    room_id: str = "",
    scope: str = "group",
    query_keywords: list[str] = None,
    top_k: int = 5,
    threshold: float = 0.45,
) -> list[dict]:
    """从聊天室记忆表中召回相关记忆（所有房间共享）"""
    query_emb = await get_embedding(query_text)
    if not query_emb:
        return []

    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM chatroom_memories WHERE embedding IS NOT NULL",
        )
        rows = await cur.fetchall()

    scored = []
    for row in rows:
        mem = dict(row)
        mem_emb = _unpack_embedding(mem["embedding"])
        vec_sim = cosine_similarity(query_emb, mem_emb)
        kw_raw = mem.get("keywords", "") or ""
        # 兼容旧格式：逗号分隔字符串 → JSON 数组字符串
        if kw_raw and not kw_raw.strip().startswith("["):
            kw_raw = json.dumps([k.strip() for k in kw_raw.replace("、", ",").split(",") if k.strip()], ensure_ascii=False)
        kw_score = _keyword_match_score(query_keywords or [], kw_raw) if query_keywords else 0
        importance = mem.get("importance", 0.5)
        final = vec_sim * 0.6 + kw_score * 0.3 + importance * 0.1
        if final >= threshold:
            mem["score"] = round(final, 4)
            scored.append(mem)

    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[:top_k]


async def recall_main_chat_memories(
    query_text: str,
    query_keywords: list[str] = None,
    top_k: int = 3,
) -> list[dict]:
    """从主聊天记忆表中召回相关记忆（只读引用）"""
    from memory import recall_memories
    matched, _ = await recall_memories(query_text, query_keywords, top_k=top_k)
    return matched


async def save_chatroom_memory(
    room_id: str,
    scope: str,
    content: str,
    keywords: str = "",
    importance: float = 0.5,
    source_start_ts: float = None,
    source_end_ts: float = None,
    unresolved: int = 0,
    valence: float = 0.0,
    arousal: float = 0.0,
) -> Optional[str]:
    """保存一条聊天室记忆"""
    emb = await get_embedding(content)
    emb_blob = _pack_embedding(emb) if emb else None
    mem_id = f"crm_{int(time.time() * 1000)}"
    now = time.time()
    async with get_db() as db:
        await db.execute(
            "INSERT INTO chatroom_memories "
            "(id, room_id, scope, content, keywords, importance, embedding, source_start_ts, source_end_ts, created_at, unresolved, valence, arousal) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (mem_id, room_id, scope, content, keywords, importance, emb_blob, source_start_ts, source_end_ts, now, unresolved, valence, arousal),
        )
        await db.commit()
    return mem_id


async def digest_chatroom(room_id: str = None, model_key: str = None) -> dict:
    """对 Connor 的所有消息（1v1 + 群聊）统一进行总结，生成原子记忆卡片存入 chatroom_memories。"""
    from datetime import datetime
    from digest_v2 import _digest_group_to_cards
    from memory import _split_into_groups_smart

    anchor_key = "connor_unified"
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT anchor_ts FROM chatroom_digest_anchors WHERE room_id = ?", (anchor_key,))
        row = await cur.fetchone()
        anchor_ts = row["anchor_ts"] if row else 0

        # ── Connor 1v1 消息 ──
        cur = await db.execute(
            "SELECT id FROM chatroom_rooms WHERE type = 'connor_1v1' ORDER BY updated_at DESC LIMIT 1"
        )
        connor_room = await cur.fetchone()
        msgs = []
        if connor_room:
            cur = await db.execute(
                "SELECT sender, content, created_at FROM chatroom_messages "
                "WHERE room_id = ? AND created_at > ? AND sender != 'system' "
                "ORDER BY created_at ASC",
                (connor_room["id"], anchor_ts),
            )
            for r in await cur.fetchall():
                d = dict(r)
                d["_source"] = "private"
                d["role"] = "assistant" if d["sender"] == "connor" else "user"
                msgs.append(d)

        # ── 群聊消息 ──
        cur = await db.execute(
            "SELECT id FROM chatroom_rooms WHERE type = 'group' ORDER BY updated_at DESC LIMIT 1"
        )
        group_room = await cur.fetchone()
        if group_room:
            cur = await db.execute(
                "SELECT sender, content, created_at FROM chatroom_messages "
                "WHERE room_id = ? AND created_at > ? AND sender != 'system' "
                "ORDER BY created_at ASC",
                (group_room["id"], anchor_ts),
            )
            for r in await cur.fetchall():
                d = dict(r)
                d["_source"] = "group"
                d["role"] = "assistant" if d["sender"] == "connor" else "user"
                msgs.append(d)

        msgs.sort(key=lambda x: x["created_at"])

    if len(msgs) < 8:
        return {"ok": False, "message": f"消息不足（{len(msgs)}条），至少需要 8 条"}

    wb = load_worldbook()
    user_name = wb.get("user_name", "用户")
    ai_name = wb.get("ai_name", "AI")
    connor_name = get_connor_name()

    persona_block = ""
    if wb.get("ai_persona"):
        persona_block += f"[{ai_name}的人设]\n{wb['ai_persona']}\n\n"
    if wb.get("user_persona"):
        persona_block += f"[{user_name}的信息]\n{wb['user_persona']}\n\n"

    store_room_id = connor_room["id"] if connor_room else (group_room["id"] if group_room else "connor_unified")
    name_map = {"user": user_name, "aion": ai_name, "connor": connor_name}

    groups = await _split_into_groups_smart(msgs, connor_name, user_name)
    total_new = 0

    for group in groups:
        source_start_ts = group[0]["created_at"]
        source_end_ts = group[-1]["created_at"]

        group_start = datetime.fromtimestamp(source_start_ts).strftime("%Y年%m月%d日 %H:%M")
        group_end = datetime.fromtimestamp(source_end_ts).strftime("%Y年%m月%d日 %H:%M")
        date_header = f"[对话时间范围: {group_start} ~ {group_end}]\n"
        sources = set(m.get("_source", "private") for m in group)
        has_mixed = len(sources) > 1
        lines = []
        for m in group:
            ts = datetime.fromtimestamp(m["created_at"]).strftime("%m-%d %H:%M")
            name = name_map.get(m["sender"], m["sender"])
            tag = f"[{'群聊' if m.get('_source') == 'group' else '私聊'}]" if has_mixed else ""
            lines.append(f"[{ts}]{tag} {name}: {m['content'][:300]}")
        messages_text = date_header + "\n".join(lines)

        cards = await _digest_group_to_cards(
            messages_text, user_name, connor_name, persona_block, simple_connor_cli_call
        )
        if not cards:
            async with get_db() as db:
                await db.execute(
                    "INSERT OR REPLACE INTO chatroom_digest_anchors (room_id, anchor_ts) VALUES (?, ?)",
                    (anchor_key, source_end_ts),
                )
                await db.commit()
            continue

        for ac in cards:
            kw_json = json.dumps(ac.get("keywords", []), ensure_ascii=False)
            await save_chatroom_memory(
                room_id=store_room_id,
                scope="connor",
                content=ac["content"],
                keywords=kw_json,
                importance=ac.get("importance", 0.5),
                source_start_ts=source_start_ts,
                source_end_ts=source_end_ts,
                unresolved=1 if ac.get("unresolved") else 0,
                valence=max(-1.0, min(1.0, float(ac.get("valence", 0.0)))),
                arousal=max(-1.0, min(1.0, float(ac.get("arousal", 0.0)))),
            )
            total_new += 1

        async with get_db() as db:
            await db.execute(
                "INSERT OR REPLACE INTO chatroom_digest_anchors (room_id, anchor_ts) VALUES (?, ?)",
                (anchor_key, source_end_ts),
            )
            await db.commit()

    return {"ok": True, "message": f"已处理 {len(msgs)} 条消息（{len(groups)} 组），生成 {total_new} 张卡片"}


# ══════════════════════════════════════════════════
#  群聊上下文构建
# ══════════════════════════════════════════════════

async def build_aion_group_context(
    room_id: str,
    room_messages: list[dict],
    aion_persona: str,
    context_minutes: int = 30,
    query_text: str = "",
    query_keywords: list[str] = None,
    *,
    digest_result: dict = None,
) -> list[dict]:
    """为 Aion 在群聊中构建完整上下文（含系统能力、记忆召回、时间感知）。
    room_messages 仅用于提取 recent_for_digest，实际消息历史由统一时间线构建。"""
    history = []

    # 0. 注入世界书（和主聊天一致的人设）
    wb = load_worldbook()
    user_name = wb.get("user_name", "用户")
    ai_name = wb.get("ai_name", "Aion")
    connor_name = get_connor_name()
    if wb.get("ai_persona"):
        history.append({"role": "user", "content": f"[系统设定 - AI人设]\n{wb['ai_persona']}"})
        history.append({"role": "assistant", "content": "收到，我会按照设定扮演角色。"})
    if wb.get("user_persona"):
        history.append({"role": "user", "content": f"[系统设定 - 用户信息]\n{wb['user_persona']}"})
        history.append({"role": "assistant", "content": "收到，我会记住你的信息。"})
    if wb.get("system_prompt"):
        history.append({"role": "user", "content": f"[系统提示]\n{wb['system_prompt']}"})
        history.append({"role": "assistant", "content": "收到，我会遵循这些规则。"})

    # 1. 注入房间补充人设
    if aion_persona:
        history.append({"role": "user", "content": f"[群聊补充设定]\n{aion_persona}"})
        history.append({"role": "assistant", "content": "收到，我会按照设定参与群聊。"})

    # 2. 注入系统能力
    ability_block = await build_ability_block(user_name, who="aion")
    history.append({"role": "user", "content": ability_block})
    history.append({"role": "assistant", "content": "好的，需要时我会使用这些指令。"})

    # 3. 构建 recent_messages 用于 instant_digest
    recent_for_digest = []
    for msg in room_messages[-6:]:
        sender = msg.get("sender", "user")
        role = "assistant" if sender == "aion" else "user"
        recent_for_digest.append({"role": role, "content": msg.get("content", "")[:200]})
    actual_recent = [m for m in recent_for_digest if m["role"] in ("user", "assistant")][-3:]

    # 4. 记忆召回（使用共享模块，Aion 读主记忆库 + 聊天室记忆）
    async def _chatroom_recall(query, keywords):
        return await recall_chatroom_memories(query, room_id, "group", keywords, top_k=3)

    mem_result = await build_memory_blocks(
        query_text,
        recent_messages=actual_recent,
        use_main_memories=True,
        chatroom_recall_fn=_chatroom_recall,
        digest_result=digest_result,
    )

    history.append({"role": "user", "content": mem_result["time_block"]})
    history.append({"role": "assistant", "content": "收到，我会在合适的时候自然提及。"})

    if mem_result["memory_block"]:
        history.append({"role": "user", "content": mem_result["memory_block"]})
        history.append({"role": "assistant", "content": "收到，我会自然地参考这些记忆。"})

    # 5. 群聊说明
    history.append({"role": "user", "content": (
        "[群聊说明]\n"
        f"你现在在一个三人群聊中，参与者：用户（{user_name}）、你（{ai_name}）、{connor_name}。\n"
        f"{connor_name} 是另一个 AI 伴侣。请自然地参与群聊对话，可以回应用户也可以和 {connor_name} 交流。\n"
        "回复时直接说话即可，不需要加前缀标记自己的身份。\n"
        "以下对话记录按时间线排列，可能包含私聊和群聊的混合内容。"
    )})
    history.append({"role": "assistant", "content": "明白了。"})

    # 6. 统一时间线（合并私聊 + 群聊消息）
    merged = await fetch_merged_timeline("aion", len(room_messages), room_id=room_id)
    timeline_history = render_merged_timeline(merged, "aion")
    history.extend(timeline_history)

    return history, mem_result.get("digest_result", {})


async def build_connor_group_context(
    room_id: str,
    room_messages: list[dict],
    connor_persona: str,
    context_minutes: int = 30,
    query_text: str = "",
    query_keywords: list[str] = None,
    *,
    digest_result: dict = None,
) -> list[dict]:
    """为 Connor 在群聊中构建完整上下文（含系统能力、记忆召回、时间感知）。
    room_messages 仅用于提取 recent_for_digest，实际消息历史由统一时间线构建。
    返回 (history, digest_result)。"""
    history = []

    wb = load_worldbook()
    user_name = wb.get("user_name", "用户")
    ai_name = wb.get("ai_name", "Aion")
    connor_name = get_connor_name()

    # 0. Connor 人设
    connor_full_persona = connor_persona or _read_connor_persona()
    if connor_full_persona:
        history.append({"role": "user", "content": f"[系统设定 - 你的角色设定]\n{connor_full_persona}"})
        history.append({"role": "assistant", "content": "收到，我会按照设定扮演角色。"})
    if wb.get("user_persona"):
        history.append({"role": "user", "content": f"[系统设定 - 用户信息]\n{wb['user_persona']}"})
        history.append({"role": "assistant", "content": "收到，我会记住用户的信息。"})

    # 1. 注入系统能力
    ability_block = await build_ability_block(user_name, who="connor")
    history.append({"role": "user", "content": ability_block})
    history.append({"role": "assistant", "content": "好的，需要时我会使用这些指令。"})

    # 2. 构建 recent_messages 用于 instant_digest
    recent_for_digest = []
    for msg in room_messages[-6:]:
        sender = msg.get("sender", "user")
        role = "assistant" if sender == "connor" else "user"
        recent_for_digest.append({"role": role, "content": msg.get("content", "")[:200]})
    actual_recent = [m for m in recent_for_digest if m["role"] in ("user", "assistant")][-3:]

    # 3. 记忆召回（Connor 只读聊天室记忆，不读 Aion 主记忆库）
    async def _chatroom_recall(query, keywords):
        return await recall_chatroom_memories(query, room_id, "connor", keywords, top_k=5)

    mem_result = await build_memory_blocks(
        query_text,
        recent_messages=actual_recent,
        use_main_memories=False,
        chatroom_recall_fn=_chatroom_recall,
        digest_result=digest_result,
    )

    history.append({"role": "user", "content": mem_result["time_block"]})
    history.append({"role": "assistant", "content": "收到。"})

    if mem_result["memory_block"]:
        history.append({"role": "user", "content": mem_result["memory_block"]})
        history.append({"role": "assistant", "content": "收到，我会自然地参考这些记忆。"})

    # 4. 群聊说明
    history.append({"role": "user", "content": (
        "[群聊说明]\n"
        f"你现在在一个三人群聊中，参与者：用户（{user_name}）、{ai_name}（另一个AI）、你（{connor_name}）。\n"
        f"请自然地参与群聊对话，可以回应用户也可以和 {ai_name} 交流。\n"
        "回复时直接说话即可，不需要加前缀标记。\n"
        "以下对话记录按时间线排列，可能包含私聊和群聊的混合内容。"
    )})
    history.append({"role": "assistant", "content": "明白了。"})

    # 5. 统一时间线（合并 Connor 1v1 + 群聊消息）
    merged = await fetch_merged_timeline("connor", len(room_messages), room_id=room_id)
    timeline_history = render_merged_timeline(merged, "connor")
    history.extend(timeline_history)

    return history, mem_result.get("digest_result", {})


async def build_connor_1v1_context(
    room_id: str,
    room_messages: list[dict],
    connor_persona: str,
    query_text: str = "",
    query_keywords: list[str] = None,
) -> list[dict]:
    """为 Connor 1v1 聊天构建 messages 列表（含跨窗口上下文、附件图片）"""
    messages = []

    wb = load_worldbook()
    user_name = wb.get("user_name", "用户")

    # 角色设定、用户信息、能力、记忆等作为前缀消息对
    if connor_persona:
        messages.append({"role": "user", "content": f"[你的角色设定]\n{connor_persona}"})
        messages.append({"role": "assistant", "content": "收到，我会按照设定扮演角色。"})

    if wb.get("user_persona"):
        messages.append({"role": "user", "content": f"[用户信息]\n{wb['user_persona']}"})
        messages.append({"role": "assistant", "content": "收到，我会记住用户的信息。"})

    ability_block = await build_ability_block(user_name, who="connor")
    messages.append({"role": "user", "content": ability_block})
    messages.append({"role": "assistant", "content": "好的，需要时我会使用这些指令。"})

    if query_text:
        mems = await recall_chatroom_memories(query_text, room_id, "connor", query_keywords, top_k=5)
        if mems:
            mem_text = "[相关记忆]\n" + "\n".join(f"- {m['content'][:200]}" for m in mems)
            messages.append({"role": "user", "content": mem_text})
            messages.append({"role": "assistant", "content": "收到，我会自然地参考这些记忆。"})

    messages.append({"role": "user", "content": (
        "[私聊说明]\n"
        "你现在在和用户的私聊窗口中。\n"
        "以下对话记录按时间线排列，可能包含私聊和群聊的混合内容，让你了解完整上下文。"
    )})
    messages.append({"role": "assistant", "content": "明白了。"})

    # 统一时间线（合并 Connor 1v1 + 群聊消息，保留附件）
    merged = await fetch_merged_timeline("connor", len(room_messages))
    timeline_history = render_merged_timeline(merged, "connor")
    messages.extend(timeline_history)

    return messages


# ══════════════════════════════════════════════════
#  Connor 自动总结（1 小时无新消息自动触发，涵盖私聊+群聊）
# ══════════════════════════════════════════════════

_connor_last_msg_ts: float = 0.0       # 最后一条 Connor 相关消息的时间
_connor_digest_armed: bool = False     # 是否有待总结的新消息


def connor_1v1_on_message():
    """Connor 相关聊天产生新消息时调用（私聊或群聊），重置 1 小时冷却"""
    global _connor_last_msg_ts, _connor_digest_armed
    _connor_last_msg_ts = time.time()
    _connor_digest_armed = True


async def _connor_1v1_auto_digest_loop():
    """后台循环：每 5 分钟检查一次，若 Connor 相关聊天已 1 小时无新消息则自动总结"""
    global _connor_digest_armed
    while True:
        await asyncio.sleep(5 * 60)
        try:
            if not _connor_digest_armed:
                continue
            if _connor_last_msg_ts == 0:
                continue
            elapsed = time.time() - _connor_last_msg_ts
            if elapsed < 60 * 60:
                continue
            print(f"[chatroom_auto_digest] Connor 相关聊天已 {elapsed/60:.0f} 分钟无新消息，开始自动总结")
            result = await digest_chatroom()
            print(f"[chatroom_auto_digest] {result.get('message', '')}")
            # 总结完成后解除 armed，避免没有新消息时重复总结
            _connor_digest_armed = False
        except asyncio.CancelledError:
            break
        except Exception as e:
            print(f"[chatroom_auto_digest] ❌ 异常: {e}")
