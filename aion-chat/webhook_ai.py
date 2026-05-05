"""
Webhook 触发的 AI 消息生成（通用管道 + 夜间手机检测 handler）

新增场景只需：
  1. 写一个 handler 函数，构建 trigger_prompt + system_note
  2. 调用 trigger_ai_reply()
  3. 在 routes/webhooks.py 的 _HANDLERS 里注册 channel → handler 映射
"""
import json, time, logging, aiosqlite
from datetime import datetime

from database import get_db
from config import DEFAULT_MODEL, SETTINGS, load_worldbook
from ws import manager
from ai_providers import stream_ai
from memory import recall_memories

log = logging.getLogger("night_phone")

DEFAULT_NIGHT_START = 23
DEFAULT_NIGHT_END = 7


def _is_night() -> bool:
    start = SETTINGS.get("night_start_hour", DEFAULT_NIGHT_START)
    end = SETTINGS.get("night_end_hour", DEFAULT_NIGHT_END)
    now_hour = datetime.now().hour
    if start < end:
        return start <= now_hour < end
    return now_hour >= start or now_hour < end


# ══════════════════════════════════════════════════════
# 通用 AI 消息生成管道
# ══════════════════════════════════════════════════════

async def trigger_ai_reply(
    trigger_prompt: str,
    system_note: str = "",
    *,
    max_chars: int = 80,
) -> dict:
    """
    查找最近活跃对话，构建上下文，调用 AI 生成回复，写入 DB 并广播。

    参数:
        trigger_prompt: 发给 AI 的触发提示词（会作为最后一条 user 消息）
        system_note:    插入对话的系统消息文本（可为空，则不插入系统消息）
        max_chars:      提示词中建议 AI 回复的字数上限（默认 80）
    返回:
        {"triggered": True/False, "conv_id": ..., "msg_id": ...}
    """
    wb = load_worldbook()
    user_name = wb.get("user_name", "你")
    ai_name = wb.get("ai_name", "AI")

    # 1. 查找最近活跃对话
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM conversations ORDER BY updated_at DESC LIMIT 1"
        )
        conv = await cur.fetchone()
        if not conv:
            log.info("无活跃对话，跳过")
            return {"triggered": False, "reason": "无活跃对话"}

        conv_id = conv["id"]
        model_key = conv["model"] or DEFAULT_MODEL

        # 加载最近对话历史（背景）
        cur = await db.execute(
            "SELECT role, content, attachments FROM messages WHERE conv_id=? "
            "AND role IN ('user','assistant') ORDER BY created_at DESC LIMIT 20",
            (conv_id,),
        )
        rows = await cur.fetchall()
        history = []
        for r in reversed(rows):
            d = dict(r)
            try:
                d["attachments"] = (
                    json.loads(d.get("attachments") or "[]")
                    if d.get("attachments")
                    else []
                )
            except Exception:
                d["attachments"] = []
            history.append(d)

    # 2. 世界书前缀
    prefix = []
    if wb.get("ai_persona"):
        prefix.append({"role": "user", "content": f"[系统设定 - AI人设]\n{wb['ai_persona']}"})
        prefix.append({"role": "assistant", "content": "收到，我会按照设定扮演角色。"})
    if wb.get("user_persona"):
        prefix.append({"role": "user", "content": f"[系统设定 - 用户信息]\n{wb['user_persona']}"})
        prefix.append({"role": "assistant", "content": "收到，我会记住你的信息。"})

    # 注入当前时间
    now_str = datetime.now().strftime("%Y年%m月%d日 %H:%M:%S")
    if prefix:
        prefix[-1]["content"] += f"\n系统当前的准确时间是 {now_str}"

    # 3. 记忆召回
    recalled, _ = await recall_memories(trigger_prompt[:300])
    mem_inject = []
    if recalled:
        mem_lines = "\n".join([f"- {m['content']}" for m in recalled])
        mem_inject = [
            {"role": "user", "content": f"[相关记忆]\n你脑海中与当前话题相关的记忆：\n{mem_lines}"},
            {"role": "assistant", "content": "收到，我会自然地参考这些记忆。"},
        ]

    # 4. 组装消息并调 AI
    messages = (
        prefix
        + mem_inject
        + history[-6:]
        + [{"role": "user", "content": f"{trigger_prompt}\n请控制在{max_chars}字以内。"}]
    )

    ai_msg_id = f"msg_{int(time.time() * 1000)}_wh"
    full_text = ""
    try:
        _temp = SETTINGS.get("temperature")
        async for chunk in stream_ai(messages, model_key, temperature=_temp):
            full_text += chunk
    except Exception as e:
        log.warning("AI 生成失败: %s", e)
        return {"triggered": False, "reason": str(e)}

    if not full_text.strip():
        return {"triggered": False, "reason": "AI 回复为空"}

    # 5. 写入 system_note（可选）
    now = time.time()
    if system_note:
        sys_msg_id = f"msg_{int(now * 1000)}_ws"
        async with get_db() as db:
            await db.execute(
                "INSERT INTO messages (id, conv_id, role, content, created_at, attachments) VALUES (?,?,?,?,?,?)",
                (sys_msg_id, conv_id, "system", system_note, now, "[]"),
            )
            await db.commit()
        sys_msg = {
            "id": sys_msg_id, "conv_id": conv_id, "role": "system",
            "content": system_note, "created_at": now, "attachments": [],
        }
        await manager.broadcast({"type": "msg_created", "data": sys_msg})

    # 6. 写入 AI 回复 + 广播
    now2 = time.time()
    async with get_db() as db:
        await db.execute(
            "INSERT INTO messages (id, conv_id, role, content, created_at, attachments) VALUES (?,?,?,?,?,?)",
            (ai_msg_id, conv_id, "assistant", full_text, now2, "[]"),
        )
        await db.execute(
            "UPDATE conversations SET updated_at=? WHERE id=?", (now2, conv_id)
        )
        await db.commit()
    ai_msg = {
        "id": ai_msg_id, "conv_id": conv_id, "role": "assistant",
        "content": full_text, "created_at": now2, "attachments": [],
    }
    await manager.broadcast({"type": "msg_created", "data": ai_msg})

    log.info("AI 消息已发送: %s", full_text[:50])
    return {"triggered": True, "conv_id": conv_id, "msg_id": ai_msg_id}


# ══════════════════════════════════════════════════════
# 内置 handler：夜间手机使用提醒
# ══════════════════════════════════════════════════════

async def handle_night_activity(payload: dict) -> dict:
    """夜间手机使用 → AI 提醒早睡"""
    if not _is_night():
        return {"triggered": False, "reason": "非夜间时段"}

    wb = load_worldbook()
    user_name = wb.get("user_name", "你")
    ai_name = wb.get("ai_name", "AI")

    app_name = payload.get("app", "") or payload.get("app_name", "")
    app_info = f"，正在使用 {app_name}" if app_name else ""

    trigger_prompt = (
        f"[系统事件] 检测到{user_name}在深夜打开了手机{app_info}。\n"
        f"请你作为{ai_name}，自然地提醒{user_name}注意休息、早点睡觉。"
    )

    system_note = f"🌙 夜间手机使用检测{('：' + app_name) if app_name else ''}"

    return await trigger_ai_reply(trigger_prompt, system_note, max_chars=50)
