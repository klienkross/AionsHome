"""
Memory V2: 主动检索与整理
"""

import aiosqlite

from database import get_db
from memory import recall_memories
from memory_cards import (
    get_card, get_follow_up_chain, get_all_links, list_cards,
)


async def search_memory(keywords: list[str], top_k: int = 5) -> list[dict]:
    """Search memory cards by keywords + vector similarity."""
    query = " ".join(keywords)
    matched, _ = await recall_memories(query, query_keywords=keywords, top_k=top_k)
    return matched


async def expand_memory(card_id: str) -> list[dict]:
    """Expand an aggregate card to show its underlying fragments."""
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT from_id FROM memory_links WHERE to_id=? AND relation='aggregated_into'",
            (card_id,),
        )
        rows = await cur.fetchall()
    fragments = []
    for row in rows:
        card = await get_card(row["from_id"])
        if card:
            fragments.append(card)
    fragments.sort(key=lambda c: c["created_at"])
    return fragments


async def get_timeline(card_id: str) -> list[dict]:
    """Get the full follow_up chain for a card."""
    return await get_follow_up_chain(card_id)


async def organize_memories(keywords: list[str]) -> dict:
    """Active organization: find related cards and suggest merges/closes."""
    from digest_v2 import _find_matching_open_cards
    from sentinel import get_embedding
    from memory_cards import should_generate_aggregate, create_aggregate_for_chain

    query = " ".join(keywords)
    vec = await get_embedding(query)
    if not vec:
        return {"ok": False, "message": "无法生成向量"}

    from config import load_settings
    settings = load_settings()
    auto_threshold = settings.get("digest_matching", {}).get("auto_threshold", 0.85)
    ask_threshold = settings.get("digest_matching", {}).get("ask_threshold", 0.65)

    matches = await _find_matching_open_cards(query, vec, auto_threshold, ask_threshold)

    actions_taken = []
    for match in matches:
        if await should_generate_aggregate(match["id"]):
            chain = await get_follow_up_chain(match["id"])
            chain_summary = " → ".join(c["content"][:30] for c in chain)
            try:
                from sentinel import call_sentinel_text
                agg_summary = await call_sentinel_text(
                    f"将以下事件链总结为一句话：\n{chain_summary}\n只输出总结。"
                )
                agg_summary = agg_summary.strip().strip('"')
            except Exception:
                agg_summary = chain_summary
            agg = await create_aggregate_for_chain(chain, agg_summary)
            actions_taken.append(f"Created aggregate: {agg_summary[:50]}")

    return {"ok": True, "matches": len(matches), "actions": actions_taken}


async def execute_mem_edit(raw: str) -> str:
    """
    解析并执行 bot 的记忆编辑指令。
    格式:
      card_id|summary|新内容
      card_id|keywords|kw1,kw2,kw3
      card_id|close
      card_id|delete
      card_id1,card_id2,...|merge|合并后的总结
    返回操作结果的简短文本。
    """
    import json as _json
    from memory_cards import update_card, update_card_status, delete_card, get_card

    parts = raw.strip().split("|", 2)
    if len(parts) < 2:
        return f"格式错误: {raw}"

    ids_str = parts[0].strip()
    action = parts[1].strip().lower()
    payload = parts[2].strip() if len(parts) > 2 else ""

    if action == "summary":
        if not payload:
            return "summary 缺少内容"
        ok = await update_card(ids_str, content=payload)
        return f"已更新卡片总结: {ids_str}" if ok else f"卡片不存在: {ids_str}"

    elif action == "keywords":
        if not payload:
            return "keywords 缺少内容"
        kws = [k.strip() for k in payload.split(",") if k.strip()]
        ok = await update_card(ids_str, keywords=_json.dumps(kws, ensure_ascii=False))
        return f"已更新关键词({len(kws)}个): {ids_str}" if ok else f"卡片不存在: {ids_str}"

    elif action == "close":
        ok = await update_card_status(ids_str, "closed")
        return f"已关闭: {ids_str}" if ok else f"卡片不存在: {ids_str}"

    elif action == "delete":
        card = await get_card(ids_str)
        if not card:
            return f"卡片不存在: {ids_str}"
        await delete_card(ids_str)
        return f"已删除: {ids_str}"

    elif action == "merge":
        if not payload:
            return "merge 缺少总结内容"
        card_ids = [cid.strip() for cid in ids_str.split(",") if cid.strip()]
        if len(card_ids) < 2:
            return "merge 至少需要2张卡片"
        from memory_cards import create_aggregate_for_chain
        cards = []
        for cid in card_ids:
            c = await get_card(cid)
            if c:
                cards.append(c)
        if len(cards) < 2:
            return f"有效卡片不足2张（找到{len(cards)}张）"
        cards.sort(key=lambda c: c["created_at"])
        agg = await create_aggregate_for_chain(cards, payload)
        return f"已合并{len(cards)}张卡片 → {agg['id']}: {payload[:40]}"

    else:
        return f"未知操作: {action}"


def format_recall_results(cards: list[dict]) -> str:
    """Format card results for injection into prompt."""
    if not cards:
        return ""
    lines = []
    for c in cards:
        status_tag = f"[{c.get('status', 'open')}]" if c.get("status") == "closed" else ""
        lines.append(f"- {c['content']} {status_tag}")
    return "\n".join(lines)
