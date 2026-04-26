"""
Memory V2: 原子卡片 CRUD 操作
"""

import json
import time
import hashlib

import aiosqlite

from database import get_db
from sentinel import get_embedding, _pack_embedding, _unpack_embedding

VALID_TYPES = {"event", "preference", "emotion", "promise", "plan", "fact", "aggregate"}
VALID_STATUSES = {"open", "closed", "merged"}
VALID_RELATIONS = {"follow_up", "derived_from", "aggregated_into", "related"}


def _make_card_id(content: str) -> str:
    ts = int(time.time() * 1000)
    h = hashlib.md5(content.encode()).hexdigest()[:6]
    return f"card_{ts}_{h}"


async def create_card(
    content: str,
    card_type: str = "event",
    keywords: list[str] = None,
    importance: float = 0.5,
    source_conv: str = None,
    source_start_ts: float = None,
    source_end_ts: float = None,
    valence: float = 0.0,
    arousal: float = 0.0,
    intensity_score: float = None,
    unresolved: int = 0,
    embed: bool = True,
) -> dict:
    card_id = _make_card_id(content)
    now = time.time()
    keywords_json = json.dumps(keywords or [], ensure_ascii=False)

    vec = None
    if embed:
        vec = await get_embedding(content)

    async with get_db() as db:
        await db.execute(
            "INSERT INTO memory_cards "
            "(id, content, type, status, created_at, updated_at, source_conv, "
            "source_start_ts, source_end_ts, embedding, keywords, importance, "
            "unresolved, valence, arousal, intensity_score) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (card_id, content, card_type, "open", now, now, source_conv,
             source_start_ts, source_end_ts,
             _pack_embedding(vec) if vec else None,
             keywords_json, importance, unresolved, valence, arousal, intensity_score),
        )
        await db.commit()

    return {
        "id": card_id, "content": content, "type": card_type, "status": "open",
        "created_at": now, "updated_at": now, "keywords": keywords_json,
        "importance": importance, "unresolved": unresolved,
        "source_start_ts": source_start_ts, "source_end_ts": source_end_ts,
        "valence": valence, "arousal": arousal, "intensity_score": intensity_score,
    }


async def get_card(card_id: str) -> dict | None:
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT id, content, type, status, created_at, updated_at, source_conv, "
            "source_start_ts, source_end_ts, keywords, importance, unresolved, "
            "valence, arousal, intensity_score FROM memory_cards WHERE id=?",
            (card_id,),
        )
        row = await cur.fetchone()
    return dict(row) if row else None


async def update_card_status(card_id: str, status: str) -> bool:
    if status not in VALID_STATUSES:
        return False
    async with get_db() as db:
        await db.execute(
            "UPDATE memory_cards SET status=?, updated_at=? WHERE id=?",
            (status, time.time(), card_id),
        )
        await db.commit()
    return True


async def update_card(card_id: str, **fields) -> bool:
    allowed = {"content", "type", "keywords", "importance", "unresolved",
               "valence", "arousal", "intensity_score", "status"}
    updates = {k: v for k, v in fields.items() if k in allowed and v is not None}
    if not updates:
        return False
    updates["updated_at"] = time.time()
    if "content" in updates:
        vec = await get_embedding(updates["content"])
        if vec:
            updates["embedding"] = _pack_embedding(vec)
    set_clause = ", ".join(f"{k}=?" for k in updates)
    params = list(updates.values()) + [card_id]
    async with get_db() as db:
        await db.execute(f"UPDATE memory_cards SET {set_clause} WHERE id=?", params)
        await db.commit()
    return True


async def delete_card(card_id: str):
    async with get_db() as db:
        await db.execute("DELETE FROM memory_links WHERE from_id=? OR to_id=?", (card_id, card_id))
        await db.execute("DELETE FROM memory_cards WHERE id=?", (card_id,))
        await db.commit()


async def list_cards(status: str = None, card_type: str = None) -> list[dict]:
    conditions = []
    params = []
    if status:
        conditions.append("status=?")
        params.append(status)
    if card_type:
        conditions.append("type=?")
        params.append(card_type)
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            f"SELECT id, content, type, status, created_at, updated_at, keywords, "
            f"importance, unresolved, source_start_ts, source_end_ts, valence, arousal, "
            f"intensity_score FROM memory_cards {where} ORDER BY created_at DESC",
            params,
        )
        return [dict(r) for r in await cur.fetchall()]


async def create_link(from_id: str, to_id: str, relation: str) -> dict:
    now = time.time()
    async with get_db() as db:
        cur = await db.execute(
            "INSERT INTO memory_links (from_id, to_id, relation, created_at) VALUES (?,?,?,?)",
            (from_id, to_id, relation, now),
        )
        await db.commit()
        link_id = cur.lastrowid
    return {"id": link_id, "from_id": from_id, "to_id": to_id, "relation": relation, "created_at": now}


async def get_links_from(card_id: str) -> list[dict]:
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT id, from_id, to_id, relation, created_at FROM memory_links WHERE from_id=?",
            (card_id,),
        )
        return [dict(r) for r in await cur.fetchall()]


async def get_links_to(card_id: str) -> list[dict]:
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT id, from_id, to_id, relation, created_at FROM memory_links WHERE to_id=?",
            (card_id,),
        )
        return [dict(r) for r in await cur.fetchall()]


async def get_all_links(card_id: str) -> list[dict]:
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT id, from_id, to_id, relation, created_at FROM memory_links "
            "WHERE from_id=? OR to_id=?",
            (card_id, card_id),
        )
        return [dict(r) for r in await cur.fetchall()]


async def get_follow_up_chain(card_id: str) -> list[dict]:
    """Walk the follow_up chain starting from card_id, return ordered list of cards."""
    visited = set()
    chain = []

    # Walk backward to find the chain root
    current = card_id
    while current and current not in visited:
        visited.add(current)
        async with get_db() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT from_id FROM memory_links WHERE to_id=? AND relation='follow_up'",
                (current,),
            )
            row = await cur.fetchone()
        if row:
            current = row["from_id"]
        else:
            break

    # Now walk forward from root
    root = current
    visited.clear()
    current = root
    while current and current not in visited:
        visited.add(current)
        card = await get_card(current)
        if card:
            chain.append(card)
        async with get_db() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT to_id FROM memory_links WHERE from_id=? AND relation='follow_up'",
                (current,),
            )
            row = await cur.fetchone()
        if row:
            current = row["to_id"]
        else:
            break

    return chain


async def should_generate_aggregate(card_id: str, min_chain_length: int = 3) -> bool:
    """Check if a card's follow_up chain is long enough to warrant an aggregate."""
    chain = await get_follow_up_chain(card_id)
    if len(chain) < min_chain_length:
        return False
    # Check no aggregate already exists for this chain
    for card in chain:
        async with get_db() as db:
            cur = await db.execute(
                "SELECT id FROM memory_links WHERE from_id=? AND relation='aggregated_into'",
                (card["id"],),
            )
            row = await cur.fetchone()
        if row:
            return False
    return True


async def create_aggregate_for_chain(chain: list[dict], summary: str) -> dict:
    """Create an aggregate card and link all chain cards to it."""
    first_ts = min(c["created_at"] for c in chain)
    last_ts = max(c["created_at"] for c in chain)
    last_status = chain[-1].get("status", "open")

    agg = await create_card(
        content=summary,
        card_type="aggregate",
        source_start_ts=first_ts,
        source_end_ts=last_ts,
    )
    if last_status == "closed":
        await update_card_status(agg["id"], "closed")

    for card in chain:
        await create_link(card["id"], agg["id"], "aggregated_into")
        await update_card_status(card["id"], "merged")

    return agg
