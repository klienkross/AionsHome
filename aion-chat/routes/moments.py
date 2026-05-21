"""
朋友圈 API：列表查询、发布、删除、点赞/踩、评论、AI 自动回复
"""

import time, json, asyncio, random
from typing import Optional
from datetime import datetime

import aiosqlite
from fastapi import APIRouter, Query
from pydantic import BaseModel

from config import DEFAULT_MODEL, SETTINGS, load_worldbook
from database import get_db
from ws import manager as ws_manager
from ai_providers import stream_ai, CLI_STATUS_PREFIX
from chatroom import (
    load_chatroom_config, send_to_connor, stream_connor_cli,
    _read_connor_persona, recall_chatroom_memories,
)
from context_builder import strip_tool_commands

router = APIRouter(prefix="/api/moments", tags=["moments"])


# ══════════════════════════════════════════════════
#  辅助函数
# ══════════════════════════════════════════════════

def _get_names() -> tuple[str, str, str]:
    """返回 (user_name, ai_name, connor_name)"""
    wb = load_worldbook()
    user_name = wb.get("user_name", "用户")
    ai_name = wb.get("ai_name", "AI")
    connor_name = load_chatroom_config().get("connor_name", "Connor")
    return user_name, ai_name, connor_name


def _author_display(author: str) -> str:
    """将内部 author 标识转换为显示名"""
    user_name, ai_name, connor_name = _get_names()
    return {"user": user_name, "aion": ai_name, "connor": connor_name}.get(author, author)


async def _get_moment_with_comments(moment_id: str) -> Optional[dict]:
    """获取一条朋友圈及其评论和反应"""
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM moments WHERE id=?", (moment_id,))
        row = await cur.fetchone()
        if not row:
            return None
        moment = dict(row)

        cur = await db.execute(
            "SELECT * FROM moment_comments WHERE moment_id=? ORDER BY created_at ASC",
            (moment_id,),
        )
        moment["comments"] = [dict(r) for r in await cur.fetchall()]

        cur = await db.execute(
            "SELECT * FROM moment_reactions WHERE moment_id=?",
            (moment_id,),
        )
        moment["reactions"] = [dict(r) for r in await cur.fetchall()]
    return moment


def _format_moment_for_prompt(moment: dict) -> str:
    """将朋友圈及其评论格式化为 AI 可读的文本"""
    user_name, ai_name, connor_name = _get_names()
    name_map = {"user": user_name, "aion": ai_name, "connor": connor_name}

    author_display = name_map.get(moment["author"], moment["author"])
    ts = datetime.fromtimestamp(moment["created_at"]).strftime("%Y-%m-%d %H:%M")
    lines = [f"[朋友圈] {author_display} 发布于 {ts}：", moment["content"]]

    # 反应
    reactions = moment.get("reactions", [])
    likes = [name_map.get(r["author"], r["author"]) for r in reactions if r["type"] == "like"]
    dislikes = [name_map.get(r["author"], r["author"]) for r in reactions if r["type"] == "dislike"]
    if likes:
        lines.append(f"👍 {', '.join(likes)}")
    if dislikes:
        lines.append(f"👎 {', '.join(dislikes)}")

    # 评论
    comments = moment.get("comments", [])
    if comments:
        lines.append("评论：")
        for c in comments:
            c_author = name_map.get(c["author"], c["author"])
            lines.append(f"  {c_author}：{c['content']}")

    return "\n".join(lines)


async def _get_recent_context_messages(who: str, limit: int = 30) -> list[dict]:
    """获取指定 AI 角色最近的聊天上下文（私聊+群聊合并排序）。
    who: 'aion' 或 'connor'
    """
    user_name, ai_name, connor_name = _get_names()
    name_map = {"user": user_name, "aion": ai_name, "connor": connor_name}
    messages = []

    async with get_db() as db:
        db.row_factory = aiosqlite.Row

        if who == "aion":
            # Aion 私聊：取最近的私聊消息
            cur = await db.execute(
                "SELECT role as sender, content, created_at FROM messages "
                "ORDER BY created_at DESC LIMIT ?", (limit,)
            )
            for row in await cur.fetchall():
                sender = "aion" if row["sender"] == "assistant" else "user"
                messages.append({
                    "sender": sender, "content": row["content"],
                    "created_at": row["created_at"], "source": "私聊"
                })
        else:
            # Connor 私聊：取 connor_1v1 房间消息
            cur = await db.execute(
                "SELECT rm.sender, rm.content, rm.created_at FROM chatroom_messages rm "
                "JOIN chatroom_rooms rr ON rm.room_id = rr.id "
                "WHERE rr.type = 'connor_1v1' AND rm.sender != 'system' "
                "ORDER BY rm.created_at DESC LIMIT ?", (limit,)
            )
            for row in await cur.fetchall():
                messages.append({
                    "sender": row["sender"], "content": row["content"],
                    "created_at": row["created_at"], "source": "私聊"
                })

        # 群聊消息（两个 AI 共享）
        cur = await db.execute(
            "SELECT rm.sender, rm.content, rm.created_at FROM chatroom_messages rm "
            "JOIN chatroom_rooms rr ON rm.room_id = rr.id "
            "WHERE rr.type = 'group' AND rm.sender != 'system' "
            "ORDER BY rm.created_at DESC LIMIT ?", (limit,)
        )
        for row in await cur.fetchall():
            messages.append({
                "sender": row["sender"], "content": row["content"],
                "created_at": row["created_at"], "source": "群聊"
            })

    # 按时间排序，取最近 limit 条
    messages.sort(key=lambda m: m["created_at"])
    messages = messages[-limit:]
    return messages


async def _get_recent_memories(who: str, limit: int = 5) -> list[str]:
    """获取指定 AI 角色最近的记忆"""
    memories = []
    async with get_db() as db:
        db.row_factory = aiosqlite.Row

        if who == "aion":
            # Aion 主记忆库
            cur = await db.execute(
                "SELECT content FROM memories ORDER BY created_at DESC LIMIT ?", (limit,)
            )
            memories.extend([row["content"] for row in await cur.fetchall()])
        else:
            # Connor 聊天室记忆
            cur = await db.execute(
                "SELECT content FROM chatroom_memories "
                "WHERE scope IN ('connor', 'group') "
                "ORDER BY created_at DESC LIMIT ?", (limit,)
            )
            memories.extend([row["content"] for row in await cur.fetchall()])

    return memories[:limit]


def _build_moment_reply_messages(
    who: str,
    moment: dict,
    context_msgs: list[dict],
    recent_memories: list[str],
    target_comment_id: str = None,
) -> list[dict]:
    """为 AI 回复朋友圈/评论构建 messages 列表"""
    user_name, ai_name, connor_name = _get_names()
    name_map = {"user": user_name, "aion": ai_name, "connor": connor_name}
    my_name = name_map.get(who, who)

    messages = []

    # 1. 角色人设
    wb = load_worldbook()
    if who == "aion":
        if wb.get("ai_persona"):
            messages.append({"role": "user", "content": f"[你的角色设定]\n{wb['ai_persona']}"})
            messages.append({"role": "assistant", "content": "收到，我会按照设定扮演角色。"})
    else:
        connor_persona = _read_connor_persona()
        if connor_persona:
            messages.append({"role": "user", "content": f"[你的角色设定]\n{connor_persona}"})
            messages.append({"role": "assistant", "content": "收到，我会按照设定扮演角色。"})

    if wb.get("user_persona"):
        messages.append({"role": "user", "content": f"[用户信息]\n{wb['user_persona']}"})
        messages.append({"role": "assistant", "content": "收到。"})

    # 2. 最近记忆
    if recent_memories:
        mem_text = "\n".join(f"- {m}" for m in recent_memories)
        messages.append({"role": "user", "content": f"[最近的记忆]\n{mem_text}"})
        messages.append({"role": "assistant", "content": "收到，我会参考这些记忆。"})

    # 3. 最近聊天上下文
    if context_msgs:
        ctx_lines = []
        for m in context_msgs:
            sender_name = name_map.get(m["sender"], m["sender"])
            tag = f"[{m['source']}]" if m.get("source") else ""
            ctx_lines.append(f"{tag} {sender_name}: {m['content'][:300]}")
        ctx_text = "\n".join(ctx_lines)
        messages.append({"role": "user", "content": f"[最近聊天上下文]\n{ctx_text}"})
        messages.append({"role": "assistant", "content": "收到，我了解最近的对话内容了。"})

    # 4. 朋友圈内容 + 评论
    moment_text = _format_moment_for_prompt(moment)
    messages.append({"role": "user", "content": moment_text})

    # 5. 任务指令
    if target_comment_id:
        # 回复某条评论
        target_comment = None
        for c in moment.get("comments", []):
            if c["id"] == target_comment_id:
                target_comment = c
                break
        if target_comment:
            commenter = name_map.get(target_comment["author"], target_comment["author"])
            messages.append({"role": "user", "content": (
                f"[任务] {commenter}在你的朋友圈下评论了：「{target_comment['content']}」\n"
                f"请你作为{my_name}，用简短自然的语气回复这条评论。"
                f"直接输出回复内容，不要加任何前缀标记。"
            )})
        else:
            messages.append({"role": "user", "content": (
                f"[任务] 请你作为{my_name}，对这条朋友圈写一条简短的评论。"
                f"直接输出评论内容，不要加任何前缀标记。"
            )})
    else:
        moment_author = name_map.get(moment["author"], moment["author"])
        messages.append({"role": "user", "content": (
            f"[任务] {moment_author}发了一条朋友圈。"
            f"请你作为{my_name}，对这条朋友圈写一条简短自然的评论。"
            f"可以是感想、调侃、鼓励、吐槽等，符合你的性格。"
            f"直接输出评论内容，不要加任何前缀标记。"
        )})

    return messages


async def _ai_reply_to_moment(who: str, moment_id: str, target_comment_id: str = None):
    """让指定的 AI 角色回复朋友圈或评论"""
    moment = await _get_moment_with_comments(moment_id)
    if not moment:
        return

    # 获取上下文和记忆
    context_msgs = await _get_recent_context_messages(who, limit=30)
    recent_memories = await _get_recent_memories(who, limit=5)

    # 构建 messages
    messages = _build_moment_reply_messages(
        who, moment, context_msgs, recent_memories, target_comment_id
    )

    # 调用 AI
    full_text = ""
    try:
        if who == "aion":
            # 取用户最近私聊会话选用的模型
            async with get_db() as _mdb:
                _mdb.row_factory = aiosqlite.Row
                _cur = await _mdb.execute(
                    "SELECT model FROM conversations ORDER BY updated_at DESC LIMIT 1"
                )
                _row = await _cur.fetchone()
            _model = _row["model"] if _row else DEFAULT_MODEL
            _temp = SETTINGS.get("temperature")
            async for chunk in stream_ai(messages, _model, temperature=_temp):
                if chunk.startswith(CLI_STATUS_PREFIX):
                    continue
                full_text += chunk
        else:
            # Connor: 尝试 HTTP 服务，失败则用 CLI
            result = await send_to_connor(messages[-1]["content"])
            if result and result != "__CONNOR_STILL_PROCESSING__":
                full_text = result
            else:
                async for chunk in stream_connor_cli(messages=messages):
                    if chunk.startswith(CLI_STATUS_PREFIX):
                        continue
                    full_text += chunk
    except Exception as e:
        print(f"[moments] AI 回复失败 ({who}): {e}")
        return

    full_text = strip_tool_commands(full_text).strip()
    if not full_text:
        return

    # 保存评论
    now = time.time()
    comment_id = f"mc_{int(now * 1000)}_{who[:1]}"
    async with get_db() as db:
        await db.execute(
            "INSERT INTO moment_comments (id, moment_id, author, content, reply_to_id, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (comment_id, moment_id, who, full_text, target_comment_id, now),
        )
        await db.commit()

    # 广播新评论
    comment_data = {
        "id": comment_id, "moment_id": moment_id, "author": who,
        "content": full_text, "reply_to_id": target_comment_id, "created_at": now,
    }
    await ws_manager.broadcast({"type": "moment_comment", "data": comment_data})

    # 随机决定是否点赞（70% 概率点赞）
    if random.random() < 0.7:
        react_id = f"mr_{int(now * 1000)}_{who[:1]}"
        try:
            async with get_db() as db:
                await db.execute(
                    "INSERT OR REPLACE INTO moment_reactions (id, moment_id, author, type, created_at) "
                    "VALUES (?,?,?,?,?)",
                    (react_id, moment_id, who, "like", now),
                )
                await db.commit()
            await ws_manager.broadcast({"type": "moment_reaction", "data": {
                "id": react_id, "moment_id": moment_id, "author": who, "type": "like", "created_at": now,
            }})
        except Exception:
            pass

    print(f"[moments] {who} 回复了朋友圈 {moment_id}: {full_text[:50]}")
    return comment_data


async def _trigger_ai_replies(moment_id: str, exclude_author: str = None):
    """触发 AI 角色回复朋友圈。exclude_author 为发布者自身，不需要自己回复自己。"""
    ai_roles = ["aion", "connor"]
    reply_roles = [r for r in ai_roles if r != exclude_author]
    random.shuffle(reply_roles)

    for role in reply_roles:
        try:
            await _ai_reply_to_moment(role, moment_id)
            # 间隔一下，让回复有时间差
            await asyncio.sleep(random.uniform(1, 3))
        except Exception as e:
            print(f"[moments] {role} 回复朋友圈失败: {e}")


# ══════════════════════════════════════════════════
#  API 路由
# ══════════════════════════════════════════════════

@router.get("")
async def list_moments(page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    """分页获取朋友圈列表（按时间倒序），包含评论和反应"""
    offset = (page - 1) * page_size
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT COUNT(*) as cnt FROM moments")
        total = (await cur.fetchone())["cnt"]
        cur = await db.execute(
            "SELECT * FROM moments ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (page_size, offset),
        )
        moments = [dict(r) for r in await cur.fetchall()]

        for m in moments:
            cur = await db.execute(
                "SELECT * FROM moment_comments WHERE moment_id=? ORDER BY created_at ASC",
                (m["id"],),
            )
            m["comments"] = [dict(r) for r in await cur.fetchall()]

            cur = await db.execute(
                "SELECT * FROM moment_reactions WHERE moment_id=?",
                (m["id"],),
            )
            m["reactions"] = [dict(r) for r in await cur.fetchall()]

    return {"items": moments, "total": total, "page": page, "page_size": page_size}


class MomentCreate(BaseModel):
    content: str


@router.post("")
async def create_moment(body: MomentCreate):
    """用户发布朋友圈"""
    content = body.content.strip()
    if not content:
        return {"error": "内容不能为空"}

    now = time.time()
    moment_id = f"mt_{int(now * 1000)}"

    async with get_db() as db:
        await db.execute(
            "INSERT INTO moments (id, author, content, source_conv, source_msg_id, expect_reply, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (moment_id, "user", content, None, None, 1, now),
        )
        await db.commit()

    moment_data = {
        "id": moment_id, "author": "user", "content": content,
        "source_conv": None, "source_msg_id": None,
        "expect_reply": 1, "created_at": now,
        "comments": [], "reactions": [],
    }

    # 广播新朋友圈
    await ws_manager.broadcast({"type": "moment_new", "data": moment_data})

    # 异步触发两个 AI 回复
    asyncio.create_task(_trigger_ai_replies(moment_id, exclude_author="user"))

    return moment_data


@router.delete("/{moment_id}")
async def delete_moment(moment_id: str):
    """删除朋友圈及其评论和反应"""
    async with get_db() as db:
        await db.execute("DELETE FROM moment_comments WHERE moment_id=?", (moment_id,))
        await db.execute("DELETE FROM moment_reactions WHERE moment_id=?", (moment_id,))
        await db.execute("DELETE FROM moments WHERE id=?", (moment_id,))
        await db.commit()
    return {"ok": True}


class ReactionBody(BaseModel):
    author: str  # user/aion/connor
    type: str  # like/dislike


@router.post("/{moment_id}/react")
async def toggle_reaction(moment_id: str, body: ReactionBody):
    """点赞/点踩（切换式：再次点击取消，换类型则替换）"""
    now = time.time()
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM moment_reactions WHERE moment_id=? AND author=?",
            (moment_id, body.author),
        )
        existing = await cur.fetchone()

        if existing:
            if existing["type"] == body.type:
                # 取消
                await db.execute("DELETE FROM moment_reactions WHERE id=?", (existing["id"],))
                await db.commit()
                await ws_manager.broadcast({"type": "moment_reaction_removed", "data": {
                    "moment_id": moment_id, "author": body.author,
                }})
                return {"ok": True, "action": "removed"}
            else:
                # 替换
                await db.execute(
                    "UPDATE moment_reactions SET type=?, created_at=? WHERE id=?",
                    (body.type, now, existing["id"]),
                )
                await db.commit()
                await ws_manager.broadcast({"type": "moment_reaction", "data": {
                    "id": existing["id"], "moment_id": moment_id,
                    "author": body.author, "type": body.type, "created_at": now,
                }})
                return {"ok": True, "action": "replaced"}
        else:
            react_id = f"mr_{int(now * 1000)}"
            await db.execute(
                "INSERT INTO moment_reactions (id, moment_id, author, type, created_at) "
                "VALUES (?,?,?,?,?)",
                (react_id, moment_id, body.author, body.type, now),
            )
            await db.commit()
            await ws_manager.broadcast({"type": "moment_reaction", "data": {
                "id": react_id, "moment_id": moment_id,
                "author": body.author, "type": body.type, "created_at": now,
            }})
            return {"ok": True, "action": "added", "id": react_id}


class CommentCreate(BaseModel):
    content: str
    reply_to_id: Optional[str] = None


@router.post("/{moment_id}/comments")
async def add_comment(moment_id: str, body: CommentCreate):
    """用户发表评论，触发对应 AI 回复"""
    content = body.content.strip()
    if not content:
        return {"error": "评论内容不能为空"}

    now = time.time()
    comment_id = f"mc_{int(now * 1000)}_u"

    async with get_db() as db:
        await db.execute(
            "INSERT INTO moment_comments (id, moment_id, author, content, reply_to_id, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (comment_id, moment_id, "user", content, body.reply_to_id, now),
        )
        await db.commit()

    comment_data = {
        "id": comment_id, "moment_id": moment_id, "author": "user",
        "content": content, "reply_to_id": body.reply_to_id, "created_at": now,
    }
    await ws_manager.broadcast({"type": "moment_comment", "data": comment_data})

    # 查询朋友圈作者，触发对应 AI 回复
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT author FROM moments WHERE id=?", (moment_id,))
        row = await cur.fetchone()
        if row and row["author"] in ("aion", "connor"):
            # 用户评论了 AI 的朋友圈，让这个 AI 回复
            asyncio.create_task(_ai_reply_to_moment(row["author"], moment_id, comment_id))
        elif row and row["author"] == "user":
            # 用户评论了自己的朋友圈——不触发额外 AI 回复
            pass

    return comment_data


@router.get("/unread")
async def check_unread():
    """检查是否有未读朋友圈（红点逻辑）"""
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        # 获取上次已读时间
        cur = await db.execute("SELECT last_read_at FROM moment_read_anchor WHERE id=1")
        row = await cur.fetchone()
        last_read = row["last_read_at"] if row else 0

        # 检查是否有新的朋友圈或评论
        cur = await db.execute(
            "SELECT COUNT(*) as cnt FROM moments WHERE created_at > ?", (last_read,)
        )
        new_moments = (await cur.fetchone())["cnt"]

        cur = await db.execute(
            "SELECT COUNT(*) as cnt FROM moment_comments WHERE created_at > ?", (last_read,)
        )
        new_comments = (await cur.fetchone())["cnt"]

    return {"has_unread": (new_moments + new_comments) > 0, "new_moments": new_moments, "new_comments": new_comments}


@router.post("/mark-read")
async def mark_read():
    """标记朋友圈为已读"""
    now = time.time()
    async with get_db() as db:
        await db.execute(
            "INSERT OR REPLACE INTO moment_read_anchor (id, last_read_at) VALUES (1, ?)",
            (now,),
        )
        await db.commit()
    return {"ok": True}
