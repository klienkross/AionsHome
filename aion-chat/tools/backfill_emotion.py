"""补标脚本：为已有记忆补充 valence / arousal 情绪维度（Russell 环形模型）。
回溯原始对话文本标注，无原文时降级用摘要。
兼容 sentinel 模块（DashScope）和无 sentinel 环境（Gemini / 其他）。
"""

import asyncio
import json
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from config import load_worldbook
from database import init_db, get_db
import aiosqlite

# ── 模型调用：优先 sentinel，fallback 到 simple_ai_call ──
try:
    from sentinel import call_sentinel
    _USE_SENTINEL = True
    print("[backfill] 使用 sentinel（DashScope）标注")
except ImportError:
    _USE_SENTINEL = False
    print("[backfill] sentinel 不可用，使用 simple_ai_call 标注")


def _parse_json_response(raw: str) -> dict | None:
    raw = raw.strip()
    if "```" in raw:
        start = raw.find("{")
        end = raw.rfind("}") + 1
        if start >= 0 and end > start:
            raw = raw[start:end]
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None


async def _call_model(prompt: str) -> dict | None:
    if _USE_SENTINEL:
        return await call_sentinel(prompt)
    else:
        from ai_providers import simple_ai_call
        from config import DEFAULT_MODEL
        messages = [{"role": "user", "content": prompt}]
        try:
            raw = await simple_ai_call(messages, DEFAULT_MODEL, temperature=0.1)
            return _parse_json_response(raw)
        except Exception as e:
            print(f"    模型调用失败: {e}")
            return None


async def _fetch_source_messages(mem) -> str | None:
    """通过记忆的 source_start_ts / source_end_ts 回溯原始对话"""
    start_ts = mem["source_start_ts"]
    end_ts = mem["source_end_ts"]
    if not start_ts or not end_ts:
        return None

    wb = load_worldbook()
    user_name = wb.get("user_name", "用户")
    ai_name = wb.get("ai_name", "AI")

    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT role, content, created_at FROM messages "
            "WHERE role IN ('user','assistant') AND created_at >= ? AND created_at <= ? "
            "ORDER BY created_at ASC",
            (start_ts, end_ts),
        )
        rows = await cur.fetchall()

    if not rows:
        return None

    lines = []
    for r in rows:
        name = user_name if r["role"] == "user" else ai_name
        lines.append(f"{name}: {r['content'][:300]}")
    return "\n".join(lines)


async def backfill():
    await init_db()

    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT id, content, source_start_ts, source_end_ts FROM memories "
            "ORDER BY created_at"
        )
        rows = await cur.fetchall()

    print(f"[backfill] 全部记忆: {len(rows)} 条")
    if not rows:
        return

    success = 0
    fail = 0
    skip = 0
    for i, row in enumerate(rows):
        mem_id = row["id"]
        content = row["content"]

        source_text = await _fetch_source_messages(row)
        if source_text:
            input_label = "原始对话"
            input_text = source_text[:2000]
        else:
            input_label = "记忆摘要（无原文）"
            input_text = content
            skip += 1

        prompt = (
            "根据 Russell 环形情绪模型，为以下对话标注整体情绪基调：\n"
            "- valence: (-1.0 ~ 1.0) 情绪效价。正=正面（开心、感动），负=负面（难过、愤怒），0=中性\n"
            "- arousal: (-1.0 ~ 1.0) 唤醒度。正=高能量（兴奋、激动），负=低能量（平静、低落），0=平淡\n\n"
            "示例：\n"
            "- 惊喜收到礼物 → {\"valence\":0.8,\"arousal\":0.7}\n"
            "- 安静回忆往事 → {\"valence\":0.3,\"arousal\":-0.5}\n"
            "- 吵架生气 → {\"valence\":-0.7,\"arousal\":0.8}\n"
            "- 无聊闲聊 → {\"valence\":0.1,\"arousal\":-0.3}\n"
            "- 纯事务（买东西、定计划）→ {\"valence\":0.0,\"arousal\":0.0}\n\n"
            "严格只输出 JSON 对象 {\"valence\": float, \"arousal\": float}。\n\n"
            f"【{input_label}】\n{input_text}"
        )

        result = await _call_model(prompt)
        if not result or "valence" not in result:
            print(f"  [{i+1}/{len(rows)}] FAIL {mem_id[:16]}... -> {result}")
            fail += 1
            continue

        valence = max(-1.0, min(1.0, float(result.get("valence", 0.0))))
        arousal = max(-1.0, min(1.0, float(result.get("arousal", 0.0))))

        async with get_db() as db:
            await db.execute(
                "UPDATE memories SET valence=?, arousal=? WHERE id=?",
                (valence, arousal, mem_id),
            )
            await db.commit()

        quadrant = (
            "高能量/正面" if valence > 0 and arousal > 0 else
            "低能量/正面" if valence > 0 else
            "高能量/负面" if arousal > 0 else
            "低能量/负面"
        )
        src = "原文" if source_text else "摘要"
        print(f"  [{i+1}/{len(rows)}] v={valence:+.1f} a={arousal:+.1f} [{quadrant}] ({src}) {content[:50]}")
        success += 1

    print(f"\n[done] 成功: {success}, 失败: {fail}, 无原文降级: {skip}")


if __name__ == "__main__":
    asyncio.run(backfill())
