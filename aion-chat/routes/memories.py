"""
记忆库 CRUD API + 手动总结 + 原文追溯
"""

import time

from fastapi import APIRouter, Query
from pydantic import BaseModel
from typing import Optional

from database import get_db
from ws import manager
from memory import get_embedding, _pack_embedding, manual_digest
from config import load_digest_anchor, save_digest_anchor

router = APIRouter()

class MemoryCreate(BaseModel):
    content: str
    type: str = "event"

class MemoryUpdate(BaseModel):
    content: str
    type: Optional[str] = None
    keywords: Optional[str] = None
    importance: Optional[float] = None
    unresolved: Optional[int] = None

@router.get("/api/memories")
async def list_memories():
    import aiosqlite
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT id, content, type, created_at, source_conv, keywords, importance, source_start_ts, source_end_ts, unresolved "
            "FROM memories ORDER BY created_at DESC"
        )
        rows = await cur.fetchall()
    return [dict(r) for r in rows]

@router.post("/api/memories")
async def create_memory(body: MemoryCreate):
    """手动添加记忆（无原文追溯，不影响总结锚点）"""
    vec = await get_embedding(body.content)
    mem_id = f"mem_{int(time.time()*1000)}"
    now = time.time()
    async with get_db() as db:
        await db.execute(
            "INSERT INTO memories (id, content, type, created_at, source_conv, embedding, keywords, importance, source_start_ts, source_end_ts) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (mem_id, body.content, body.type, now, None, _pack_embedding(vec) if vec else None, '', 0.5, None, None)
        )
        await db.commit()
    mem = {"id": mem_id, "content": body.content, "type": body.type, "created_at": now,
           "keywords": "", "importance": 0.5, "source_start_ts": None, "source_end_ts": None}
    await manager.broadcast({"type": "memory_added", "data": mem})
    return mem

@router.put("/api/memories/{mem_id}")
async def update_memory(mem_id: str, body: MemoryUpdate):
    vec = await get_embedding(body.content)
    async with get_db() as db:
        fields = ["content=?", "embedding=?"]
        params = [body.content, _pack_embedding(vec) if vec else None]
        if body.type is not None:
            fields.append("type=?")
            params.append(body.type)
        if body.keywords is not None:
            fields.append("keywords=?")
            params.append(body.keywords)
        if body.importance is not None:
            fields.append("importance=?")
            params.append(body.importance)
        if body.unresolved is not None:
            fields.append("unresolved=?")
            params.append(1 if body.unresolved else 0)
        params.append(mem_id)
        await db.execute(f"UPDATE memories SET {', '.join(fields)} WHERE id=?", params)
        await db.commit()
    return {"ok": True, "id": mem_id}

@router.delete("/api/memories/{mem_id}")
async def delete_memory(mem_id: str):
    async with get_db() as db:
        await db.execute("DELETE FROM memories WHERE id=?", (mem_id,))
        await db.commit()
    return {"ok": True}

@router.patch("/api/memories/{mem_id}/unresolved")
async def toggle_unresolved(mem_id: str):
    """切换记忆的 unresolved 状态"""
    import aiosqlite
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT unresolved FROM memories WHERE id=?", (mem_id,))
        row = await cur.fetchone()
        if not row:
            return {"ok": False, "message": "记忆不存在"}
        new_val = 0 if row["unresolved"] else 1
        await db.execute("UPDATE memories SET unresolved=? WHERE id=?", (new_val, mem_id))
        await db.commit()
    return {"ok": True, "unresolved": new_val}

@router.get("/api/memories/by-conv/{conv_id}")
async def get_memories_by_conv(conv_id: str):
    """获取某对话中 AI 主动录入的记忆（有 source_msg_id 的）"""
    import aiosqlite
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT id, content, source_msg_id FROM memories WHERE source_conv=? AND source_msg_id IS NOT NULL",
            (conv_id,)
        )
        rows = await cur.fetchall()
    return [{"mem_id": r["id"], "content": r["content"], "msg_id": r["source_msg_id"]} for r in rows]

@router.post("/api/memories/digest")
async def trigger_digest():
    """手动触发记忆总结"""
    result = await manual_digest()
    return result

@router.get("/api/memories/digest/anchor")
async def get_anchor():
    """获取当前总结锚点时间戳"""
    from datetime import datetime
    ts = load_digest_anchor()
    date_str = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S") if ts > 0 else "从未总结"
    return {"ok": True, "anchor_ts": ts, "anchor_date": date_str}

class AnchorReset(BaseModel):
    date: str  # 格式: YYYY-MM-DD 或 YYYY-MM-DD HH:MM:SS

@router.post("/api/memories/digest/anchor")
async def reset_anchor(body: AnchorReset):
    """重置总结锚点到指定日期"""
    from datetime import datetime
    try:
        if len(body.date) <= 10:
            dt = datetime.strptime(body.date, "%Y-%m-%d")
        else:
            dt = datetime.strptime(body.date, "%Y-%m-%d %H:%M:%S")
        ts = dt.timestamp()
        save_digest_anchor(ts)
        return {"ok": True, "anchor_ts": ts, "anchor_date": dt.strftime("%Y-%m-%d %H:%M:%S")}
    except ValueError:
        return {"ok": False, "message": "日期格式不正确，请使用 YYYY-MM-DD 或 YYYY-MM-DD HH:MM:SS"}

@router.get("/api/memories/{mem_id}/source")
async def get_memory_source(mem_id: str):
    """追溯记忆对应的原始聊天记录"""
    import aiosqlite
    from config import load_worldbook
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT source_start_ts, source_end_ts FROM memories WHERE id=?", (mem_id,))
        mem = await cur.fetchone()
    if not mem or not mem["source_start_ts"] or not mem["source_end_ts"]:
        return {"ok": False, "message": "该记忆没有可追溯的原文"}

    wb = load_worldbook()
    user_name = wb.get("user_name", "用户")
    ai_name = wb.get("ai_name", "AI")

    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT role, content, created_at FROM messages "
            "WHERE role IN ('user','assistant') AND created_at >= ? AND created_at <= ? "
            "ORDER BY created_at ASC",
            (mem["source_start_ts"], mem["source_end_ts"])
        )
        rows = await cur.fetchall()

    messages = []
    for r in rows:
        messages.append({
            "role": r["role"],
            "name": user_name if r["role"] == "user" else ai_name,
            "content": r["content"],
            "created_at": r["created_at"],
        })
    return {"ok": True, "messages": messages}
