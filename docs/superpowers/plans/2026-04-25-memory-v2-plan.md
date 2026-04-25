# Memory V2: Zettelkasten-Style Atomic Memory System — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace conversation-level memory summaries with atomic "one fact, one card" architecture, with inter-card relationships, event lifecycle, multi-agent digest, and active retrieval.

**Architecture:** Incremental refactor on existing SQLite backend. New `memory_cards` + `memory_links` tables, reworked digest engine producing atomic cards, modified recall to filter by card status/aggregation, new markup commands for active retrieval. Old `memories` table renamed for reference.

**Tech Stack:** Python 3 / FastAPI / aiosqlite / DashScope (qwen-flash + text-embedding-v4) / Pydantic

**Spec:** `docs/superpowers/specs/2026-04-25-memory-v2-design.md`

---

## Phase 1: Data Structure + Atomic Digest

### Task 1: Create new database tables

**Files:**
- Modify: `aion-chat/database.py:38-62` (add new tables alongside existing)

- [ ] **Step 1: Write test for new table creation**

Create `aion-chat/tests/test_database_v2.py`:

```python
import asyncio
import aiosqlite
import tempfile, os

async def _init_test_db(path):
    """Replicate init_db logic for test DB"""
    from database import init_db
    import config
    original = config.DB_PATH
    config.DB_PATH = path
    await init_db()
    config.DB_PATH = original
    return path

async def test_memory_cards_table():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        await _init_test_db(db_path)
        async with aiosqlite.connect(db_path) as db:
            db.row_factory = aiosqlite.Row
            # Verify memory_cards columns
            cur = await db.execute("PRAGMA table_info(memory_cards)")
            cols = {row[1] for row in await cur.fetchall()}
            expected = {"id", "content", "type", "status", "created_at", "updated_at",
                       "source_conv", "source_start_ts", "source_end_ts", "embedding",
                       "keywords", "importance", "unresolved", "valence", "arousal",
                       "intensity_score"}
            assert expected.issubset(cols), f"Missing columns: {expected - cols}"

            # Verify memory_links columns
            cur = await db.execute("PRAGMA table_info(memory_links)")
            cols = {row[1] for row in await cur.fetchall()}
            expected_links = {"id", "from_id", "to_id", "relation", "created_at"}
            assert expected_links.issubset(cols), f"Missing link columns: {expected_links - cols}"

            # Verify indexes exist
            cur = await db.execute("SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_memory_%'")
            indexes = {row[0] for row in await cur.fetchall()}
            assert "idx_memory_cards_status" in indexes
            assert "idx_memory_links_from" in indexes
            assert "idx_memory_links_to" in indexes
    finally:
        os.unlink(db_path)

if __name__ == "__main__":
    asyncio.run(test_memory_cards_table())
    print("PASS: test_memory_cards_table")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd aion-chat && python tests/test_database_v2.py`
Expected: FAIL — `memory_cards` table doesn't exist yet.

- [ ] **Step 3: Add memory_cards and memory_links tables to database.py**

In `aion-chat/database.py`, after the existing `memories` table block (~line 62), add:

```python
        # ── Memory V2: 原子卡片表 ──
        await db.execute("""
            CREATE TABLE IF NOT EXISTS memory_cards (
                id TEXT PRIMARY KEY,
                content TEXT NOT NULL,
                type TEXT DEFAULT 'event',
                status TEXT DEFAULT 'open',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                source_conv TEXT,
                source_start_ts REAL,
                source_end_ts REAL,
                embedding BLOB,
                keywords TEXT DEFAULT '',
                importance REAL DEFAULT 0.5,
                unresolved INTEGER DEFAULT 0,
                valence REAL DEFAULT 0.0,
                arousal REAL DEFAULT 0.0,
                intensity_score REAL
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_memory_cards_status ON memory_cards(status)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_memory_cards_created ON memory_cards(created_at DESC)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_memory_cards_type ON memory_cards(type)")
        # ── Memory V2: 卡片关联表 ──
        await db.execute("""
            CREATE TABLE IF NOT EXISTS memory_links (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                from_id TEXT NOT NULL,
                to_id TEXT NOT NULL,
                relation TEXT NOT NULL,
                created_at REAL NOT NULL,
                FOREIGN KEY (from_id) REFERENCES memory_cards(id) ON DELETE CASCADE,
                FOREIGN KEY (to_id) REFERENCES memory_cards(id) ON DELETE CASCADE
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_memory_links_from ON memory_links(from_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_memory_links_to ON memory_links(to_id)")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd aion-chat && python tests/test_database_v2.py`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add aion-chat/database.py aion-chat/tests/test_database_v2.py
git commit -m "feat(memory-v2): add memory_cards and memory_links tables"
```

---

### Task 2: Create card CRUD module

**Files:**
- Create: `aion-chat/memory_cards.py`
- Create: `aion-chat/tests/test_memory_cards.py`

- [ ] **Step 1: Write tests for card CRUD**

Create `aion-chat/tests/test_memory_cards.py`:

```python
import asyncio
import time

async def setup_test_db():
    """Initialize a fresh test DB and return cleanup func"""
    import tempfile, os, config
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    db_path = f.name
    f.close()
    config.DB_PATH = db_path
    from database import init_db
    await init_db()
    return db_path

async def test_create_and_get_card():
    db_path = await setup_test_db()
    try:
        from memory_cards import create_card, get_card

        card = await create_card(
            content="喜欢拿铁",
            card_type="preference",
            keywords=["拿铁", "咖啡"],
            importance=0.6,
            source_conv="conv_001",
            source_start_ts=1000.0,
            source_end_ts=2000.0,
        )
        assert card["id"].startswith("card_")
        assert card["content"] == "喜欢拿铁"
        assert card["type"] == "preference"
        assert card["status"] == "open"

        fetched = await get_card(card["id"])
        assert fetched is not None
        assert fetched["content"] == "喜欢拿铁"
    finally:
        import os; os.unlink(db_path)

async def test_update_card_status():
    db_path = await setup_test_db()
    try:
        from memory_cards import create_card, update_card_status, get_card

        card = await create_card(content="计划去���啡店", card_type="plan")
        await update_card_status(card["id"], "closed")
        fetched = await get_card(card["id"])
        assert fetched["status"] == "closed"
    finally:
        import os; os.unlink(db_path)

async def test_create_link():
    db_path = await setup_test_db()
    try:
        from memory_cards import create_card, create_link, get_links_from

        c1 = await create_card(content="计划去咖啡店", card_type="plan")
        c2 = await create_card(content="去了咖啡店", card_type="event")
        await create_link(c1["id"], c2["id"], "follow_up")
        links = await get_links_from(c1["id"])
        assert len(links) == 1
        assert links[0]["to_id"] == c2["id"]
        assert links[0]["relation"] == "follow_up"
    finally:
        import os; os.unlink(db_path)

async def test_list_cards_filtered():
    db_path = await setup_test_db()
    try:
        from memory_cards import create_card, list_cards

        await create_card(content="事件A", card_type="event")
        await create_card(content="偏好B", card_type="preference")
        c3 = await create_card(content="事件C", card_type="event")
        from memory_cards import update_card_status
        await update_card_status(c3["id"], "closed")

        all_cards = await list_cards()
        assert len(all_cards) == 3

        open_only = await list_cards(status="open")
        assert len(open_only) == 2

        events = await list_cards(card_type="event")
        assert len(events) == 2
    finally:
        import os; os.unlink(db_path)

if __name__ == "__main__":
    for name, fn in [
        ("test_create_and_get_card", test_create_and_get_card),
        ("test_update_card_status", test_update_card_status),
        ("test_create_link", test_create_link),
        ("test_list_cards_filtered", test_list_cards_filtered),
    ]:
        asyncio.run(fn())
        print(f"PASS: {name}")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd aion-chat && python tests/test_memory_cards.py`
Expected: FAIL — `memory_cards` module doesn't exist yet.

- [ ] **Step 3: Implement memory_cards.py**

Create `aion-chat/memory_cards.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd aion-chat && python tests/test_memory_cards.py`
Expected: All 4 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add aion-chat/memory_cards.py aion-chat/tests/test_memory_cards.py
git commit -m "feat(memory-v2): card CRUD module with link support"
```

---

### Task 3: Rewrite digest engine to produce atomic cards

**Files:**
- Create: `aion-chat/digest_v2.py`
- Create: `aion-chat/tests/test_digest_v2.py`

This is the core change: the digest pipeline now calls Agent A to split messages into atomic cards.

- [ ] **Step 1: Write test for atomic split prompt parsing**

Create `aion-chat/tests/test_digest_v2.py`:

```python
import asyncio
import json

def test_parse_atomic_cards():
    """Test parsing of Agent A output format"""
    from digest_v2 import _parse_atomic_cards

    raw = json.dumps([
        {"content": "去了咖啡店", "type": "event", "keywords": ["咖啡店"], "importance": 0.5},
        {"content": "喜欢拿铁", "type": "preference", "keywords": ["拿铁"], "importance": 0.6},
        {"content": "计划下周再去", "type": "plan", "keywords": ["咖啡店"], "importance": 0.3},
    ])
    cards = _parse_atomic_cards(raw)
    assert len(cards) == 3
    assert cards[0]["type"] == "event"
    assert cards[1]["type"] == "preference"
    assert cards[2]["type"] == "plan"

    # Malformed input returns empty
    assert _parse_atomic_cards("not json") == []
    assert _parse_atomic_cards("{}") == []
    assert _parse_atomic_cards('[{"no_content": true}]') == []

def test_compute_intensity():
    from digest_v2 import compute_intensity

    # Fast, long messages, many turns → high intensity
    msgs_fast = [
        {"created_at": 1000.0 + i * 10, "content": "x" * 200, "role": "user" if i % 2 == 0 else "assistant"}
        for i in range(20)
    ]
    score_fast = compute_intensity(msgs_fast)
    assert 0.7 < score_fast <= 1.0, f"Expected high intensity, got {score_fast}"

    # Slow, short messages, few turns → low intensity
    msgs_slow = [
        {"created_at": 1000.0 + i * 600, "content": "ok", "role": "user" if i % 2 == 0 else "assistant"}
        for i in range(3)
    ]
    score_slow = compute_intensity(msgs_slow)
    assert score_slow < 0.4, f"Expected low intensity, got {score_slow}"

if __name__ == "__main__":
    test_parse_atomic_cards()
    print("PASS: test_parse_atomic_cards")
    test_compute_intensity()
    print("PASS: test_compute_intensity")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd aion-chat && python tests/test_digest_v2.py`
Expected: FAIL — `digest_v2` module doesn't exist.

- [ ] **Step 3: Implement digest_v2.py**

Create `aion-chat/digest_v2.py`:

```python
"""
Memory V2 Digest Engine: 原子卡片拆分 + 情绪评价 + 对话强度
"""

import json
import time
import math
from datetime import datetime

import aiosqlite

from config import (
    get_key, load_worldbook, load_settings,
    load_digest_anchor, save_digest_anchor, DEFAULT_MODEL,
)
from database import get_db
from ws import manager
from sentinel import call_sentinel, get_embedding, _pack_embedding, _unpack_embedding, EMBEDDING_DIMS
from memory_cards import create_card, create_link, get_card, list_cards, update_card_status
from memory import (
    _split_into_groups_smart, _parse_json_response, _get_active_model_and_conv,
    cosine_similarity,
)


def _parse_atomic_cards(raw: str) -> list[dict]:
    """Parse Agent A output: a JSON array of atomic cards."""
    if isinstance(raw, list):
        items = raw
    else:
        raw = raw.strip()
        if "```" in raw:
            start = raw.find("[")
            end = raw.rfind("]") + 1
            if start >= 0 and end > start:
                raw = raw[start:end]
        try:
            items = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return []
    if not isinstance(items, list):
        return []
    valid = []
    for item in items:
        if isinstance(item, dict) and item.get("content", "").strip():
            valid.append({
                "content": item["content"].strip(),
                "type": item.get("type", "event"),
                "keywords": item.get("keywords", []),
                "importance": float(item.get("importance", 0.5)),
                "unresolved": 1 if item.get("unresolved", False) else 0,
            })
    return valid


def _parse_emotion_output(raw: str, card_contents: list[str]) -> list[dict]:
    """Parse Agent B output: emotion evaluations per card."""
    if isinstance(raw, list):
        items = raw
    else:
        try:
            items = json.loads(raw.strip())
        except (json.JSONDecodeError, ValueError):
            return [{"valence": 0.0, "arousal": 0.0} for _ in card_contents]
    if not isinstance(items, list):
        return [{"valence": 0.0, "arousal": 0.0} for _ in card_contents]
    result = []
    for i, content in enumerate(card_contents):
        if i < len(items) and isinstance(items[i], dict):
            result.append({
                "valence": max(-1.0, min(1.0, float(items[i].get("valence", 0.0)))),
                "arousal": max(-1.0, min(1.0, float(items[i].get("arousal", 0.0)))),
            })
        else:
            result.append({"valence": 0.0, "arousal": 0.0})
    return result


def compute_intensity(msgs: list[dict]) -> float:
    """Compute conversation intensity score from message timestamps and lengths."""
    if len(msgs) < 2:
        return 0.0
    intervals = []
    for i in range(1, len(msgs)):
        gap = msgs[i]["created_at"] - msgs[i - 1]["created_at"]
        if gap > 0:
            intervals.append(gap)
    avg_interval = sum(intervals) / len(intervals) if intervals else 300.0
    avg_chars = sum(len(m.get("content", "")) for m in msgs) / len(msgs)
    turn_count = len(msgs)

    speed_score = 1.0 - min(avg_interval / 300.0, 1.0)
    length_score = min(avg_chars / 200.0, 1.0)
    density_score = min(turn_count / 20.0, 1.0)
    return round(speed_score * 0.4 + length_score * 0.35 + density_score * 0.25, 4)


def _build_agent_a_prompt(messages_text: str, user_name: str, ai_name: str, persona_block: str) -> str:
    """Build the atomic split prompt for Agent A."""
    return (
        f"{persona_block}"
        f"你是一个记忆拆分专家。请将下面的对话拆分成独立的原子记忆卡片，每张卡片只记录一件事。\n\n"
        f"规则：\n"
        f"- 每张卡片的 content 应是一个完整的陈述句，包含日期和必要上下文\n"
        f"- 使用 \"{user_name}\" 和 \"{ai_name}\" 指代双方\n"
        f"- type 必须是以下之一：event, preference, emotion, promise, plan, fact\n"
        f"- keywords: 2-6 个核心关键词，【严禁】人名（Aion, Ithil 等）和泛指词\n"
        f"- importance: 0.0-1.0，评分严厉（默认 0.3，只有重大事实才给 0.8+）\n"
        f"- unresolved: 未完成的计划/承诺为 true，已发生事实为 false\n\n"
        f"输出一个 JSON 数组，每个元素格式：\n"
        f'{{"content": "...", "type": "...", "keywords": [...], "importance": 0.X, "unresolved": false}}\n\n'
        f"严格只输出 JSON 数组，不要其他内容。\n\n"
        f"【对话记录】：\n{messages_text}"
    )


def _build_agent_b_prompt(card_contents: list[str], messages_text: str) -> str:
    """Build the emotion evaluation prompt for Agent B."""
    cards_list = "\n".join(f"{i+1}. {c}" for i, c in enumerate(card_contents))
    return (
        f"请对以下每条记忆卡片评估情绪维度。\n\n"
        f"卡片列表：\n{cards_list}\n\n"
        f"原始对话供参考：\n{messages_text[:2000]}\n\n"
        f"对每张卡片输出 valence(-1.0~1.0, 正=正面情绪, 负=负面) 和 arousal(-1.0~1.0, 正=高能量, 负=低能量)。\n"
        f"输出 JSON 数组，每个元素：{{\"valence\": X, \"arousal\": Y}}\n"
        f"顺序与卡片列表一一对应。严格只输出 JSON 数组。"
    )


async def _find_matching_open_cards(new_card_content: str, new_card_embedding: list[float],
                                     auto_threshold: float, ask_threshold: float) -> list[dict]:
    """Find existing open cards that match a new card by embedding similarity."""
    if not new_card_embedding:
        return []
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT id, content, type, status, embedding FROM memory_cards "
            "WHERE status='open' AND embedding IS NOT NULL"
        )
        rows = await cur.fetchall()
    matches = []
    for row in rows:
        mem_vec = _unpack_embedding(row["embedding"])
        sim = cosine_similarity(new_card_embedding, mem_vec)
        if sim >= ask_threshold:
            matches.append({
                "id": row["id"], "content": row["content"],
                "type": row["type"], "similarity": round(sim, 4),
                "auto": sim >= auto_threshold,
            })
    matches.sort(key=lambda x: x["similarity"], reverse=True)
    return matches


async def _dedup_against_realtime(card_content: str, card_embedding: list[float],
                                   source_conv: str, threshold: float = 0.85) -> str | None:
    """Check if a [MEMORY:...] card already covers this content. Returns existing card ID or None."""
    if not card_embedding or not source_conv:
        return None
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT id, embedding FROM memory_cards WHERE source_conv=? AND embedding IS NOT NULL",
            (source_conv,),
        )
        rows = await cur.fetchall()
    for row in rows:
        mem_vec = _unpack_embedding(row["embedding"])
        sim = cosine_similarity(card_embedding, mem_vec)
        if sim >= threshold:
            return row["id"]
    return None


async def _do_digest_v2(min_messages: int = 0) -> dict:
    """V2 digest: atomic card split + emotion + intensity + relationship matching."""
    from ai_providers import simple_ai_call

    settings = load_settings()
    split_mode = settings.get("digest_agents", {}).get("split_mode", "separate")
    auto_threshold = settings.get("digest_matching", {}).get("auto_threshold", 0.85)
    ask_threshold = settings.get("digest_matching", {}).get("ask_threshold", 0.65)

    anchor_ts = load_digest_anchor()

    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT id, conv_id, role, content, created_at FROM messages "
            "WHERE role IN ('user','assistant') AND created_at > ? "
            "ORDER BY created_at ASC",
            (anchor_ts,),
        )
        new_msgs = [dict(r) for r in await cur.fetchall()]

    if not new_msgs:
        return {"ok": True, "message": "没有新消息需要总结", "new_cards_count": 0, "processed_messages": 0}

    if min_messages > 0 and len(new_msgs) < min_messages:
        return {"ok": True, "message": f"消息不足 {min_messages} 条，跳过", "new_cards_count": 0, "processed_messages": 0}

    wb = load_worldbook()
    user_name = wb.get("user_name", "用户")
    ai_name = wb.get("ai_name", "AI")
    ai_persona = wb.get("ai_persona", "")
    user_persona = wb.get("user_persona", "")

    model_key, conv_id = await _get_active_model_and_conv()

    persona_block = ""
    if ai_persona:
        persona_block += f"[{ai_name}的人设]\n{ai_persona}\n\n"
    if user_persona:
        persona_block += f"[{user_name}的人设]\n{user_persona}\n\n"

    groups = await _split_into_groups_smart(new_msgs, user_name, ai_name)
    total_new = 0
    all_summaries = []

    for group in groups:
        group_start = datetime.fromtimestamp(group[0]["created_at"]).strftime("%Y年%m月%d日 %H:%M")
        group_end = datetime.fromtimestamp(group[-1]["created_at"]).strftime("%Y年%m月%d日 %H:%M")
        date_header = f"[对话时间范围: {group_start} ~ {group_end}]\n"
        messages_text = date_header + "\n".join([
            f"[{datetime.fromtimestamp(m['created_at']).strftime('%m-%d %H:%M')}] "
            f"{user_name if m['role']=='user' else ai_name}: {m['content'][:300]}"
            for m in group
        ])

        source_start_ts = group[0]["created_at"]
        source_end_ts = group[-1]["created_at"]
        source_conv_id = group[0].get("conv_id")

        # Step 2: Agent A — Atomic split
        agent_a_prompt = _build_agent_a_prompt(messages_text, user_name, ai_name, persona_block)
        try:
            raw_a = await simple_ai_call([{"role": "user", "content": agent_a_prompt}], model_key)
        except Exception as e:
            print(f"[digest_v2] Agent A failed: {e}")
            save_digest_anchor(source_end_ts)
            continue

        atomic_cards = _parse_atomic_cards(raw_a)
        if not atomic_cards:
            print(f"[digest_v2] Agent A returned no valid cards for group {group_start}")
            save_digest_anchor(source_end_ts)
            continue

        # Step 3: Agent B — Emotion (if separate mode)
        card_contents = [c["content"] for c in atomic_cards]
        if split_mode == "separate":
            agent_b_prompt = _build_agent_b_prompt(card_contents, messages_text)
            try:
                raw_b = await call_sentinel(agent_b_prompt)
                emotions = _parse_emotion_output(raw_b if isinstance(raw_b, str) else json.dumps(raw_b), card_contents)
            except Exception as e:
                print(f"[digest_v2] Agent B failed: {e}")
                emotions = [{"valence": 0.0, "arousal": 0.0} for _ in card_contents]
        else:
            emotions = [{"valence": 0.0, "arousal": 0.0} for _ in card_contents]

        # Step 4: Intensity (pure math)
        intensity = compute_intensity(group)

        # Step 5 & 6: Create cards, match relationships, dedup
        for i, ac in enumerate(atomic_cards):
            vec = await get_embedding(ac["content"])

            # Dedup against [MEMORY:...] cards from same conversation
            if source_conv_id:
                existing_id = await _dedup_against_realtime(
                    ac["content"], vec, source_conv_id, auto_threshold
                )
                if existing_id:
                    print(f"[digest_v2] Skipping duplicate of {existing_id}: {ac['content'][:40]}")
                    continue

            card = await create_card(
                content=ac["content"],
                card_type=ac["type"],
                keywords=ac["keywords"],
                importance=ac["importance"],
                source_conv=source_conv_id,
                source_start_ts=source_start_ts,
                source_end_ts=source_end_ts,
                valence=emotions[i]["valence"] if i < len(emotions) else 0.0,
                arousal=emotions[i]["arousal"] if i < len(emotions) else 0.0,
                intensity_score=intensity,
                unresolved=ac["unresolved"],
                embed=False,
            )
            # Write embedding directly (we already have it)
            if vec:
                async with get_db() as db:
                    await db.execute(
                        "UPDATE memory_cards SET embedding=? WHERE id=?",
                        (_pack_embedding(vec), card["id"]),
                    )
                    await db.commit()

            # Relationship matching
            if vec:
                matches = await _find_matching_open_cards(
                    ac["content"], vec, auto_threshold, ask_threshold
                )
                for match in matches[:3]:
                    if match["id"] == card["id"]:
                        continue
                    if match["auto"]:
                        await create_link(match["id"], card["id"], "follow_up")
                        print(f"[digest_v2] Auto-linked {match['id'][:20]} → {card['id'][:20]}")

            await manager.broadcast({"type": "memory_added", "data": {
                "id": card["id"], "content": card["content"], "type": card["type"],
                "status": "open", "created_at": card["created_at"],
                "keywords": card["keywords"], "importance": card["importance"],
                "unresolved": card["unresolved"],
                "valence": card.get("valence", 0.0), "arousal": card.get("arousal", 0.0),
            }})
            total_new += 1
            all_summaries.append(ac["content"])

        save_digest_anchor(source_end_ts)

    # Step 7: AI reflection + gift (reuse existing logic from memory.py)
    if conv_id and total_new > 0 and all_summaries:
        from memory import _do_digest as _  # noqa: ensure module loaded
        from ai_providers import simple_ai_call as ai_call
        try:
            async with get_db() as db:
                db.row_factory = aiosqlite.Row
                cur = await db.execute(
                    "SELECT role, content FROM messages "
                    "WHERE conv_id=? AND role IN ('user','assistant') "
                    "ORDER BY created_at DESC LIMIT 30",
                    (conv_id,),
                )
                recent_rows = list(reversed(await cur.fetchall()))
            context_msgs = [{"role": r["role"], "content": r["content"][:300]} for r in recent_rows]

            summaries_text = "\n".join(f"- {s}" for s in all_summaries)
            comment_prompt = (
                f"{persona_block}"
                f"你是{ai_name}。你刚刚整理了和{user_name}今天的聊天记忆，以下是你整理出的摘要：\n"
                f"{summaries_text}\n\n"
                f"现在写下整理完这些记忆后想对{user_name}说的话。"
                f"可以是感慨、吐槽、温情的碎碎念，或者根据之前聊的上下文，未来的计划，想说的心里话等等，语气要完全符合你的人设性格。"
            )
            comment_messages = context_msgs + [{"role": "user", "content": comment_prompt}]
            comment_text = await ai_call(comment_messages, model_key)
            comment_text = comment_text.strip().strip('"').strip()

            if comment_text:
                capsule_now = time.time()
                capsule_id = f"msg_{int(capsule_now*1000)}_digest"
                capsule_text = f"🧠 {ai_name}整理了记忆库"
                async with get_db() as db:
                    await db.execute(
                        "INSERT INTO messages (id, conv_id, role, content, created_at, attachments) VALUES (?,?,?,?,?,?)",
                        (capsule_id, conv_id, "system", capsule_text, capsule_now, "[]"),
                    )
                    await db.commit()
                await manager.broadcast({"type": "msg_created", "data": {
                    "id": capsule_id, "conv_id": conv_id, "role": "system",
                    "content": capsule_text, "created_at": capsule_now, "attachments": [],
                }})

                comment_now = time.time()
                comment_id = f"msg_{int(comment_now*1000)}_digest_comment"
                async with get_db() as db:
                    await db.execute(
                        "INSERT INTO messages (id, conv_id, role, content, created_at, attachments) VALUES (?,?,?,?,?,?)",
                        (comment_id, conv_id, "assistant", comment_text, comment_now, "[]"),
                    )
                    await db.commit()
                await manager.broadcast({"type": "msg_created", "data": {
                    "id": comment_id, "conv_id": conv_id, "role": "assistant",
                    "content": comment_text, "created_at": comment_now, "attachments": [],
                }})
        except Exception as e:
            print(f"[digest_v2] Reflection failed: {e}")

        # Gift judgment
        try:
            import asyncio
            from gift import judge_and_send_gift
            asyncio.create_task(judge_and_send_gift(
                all_summaries, context_msgs, persona_block,
                ai_name, user_name, model_key, conv_id,
            ))
        except Exception as e:
            print(f"[digest_v2] Gift judgment failed: {e}")

    return {
        "ok": True,
        "message": f"V2总结完成：处理 {len(new_msgs)} 条消息（{len(groups)} 组），生成 {total_new} 张卡片",
        "new_cards_count": total_new,
        "processed_messages": len(new_msgs),
    }


async def manual_digest_v2() -> dict:
    return await _do_digest_v2(min_messages=0)


async def auto_digest_v2() -> dict:
    return await _do_digest_v2(min_messages=30)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd aion-chat && python tests/test_digest_v2.py`
Expected: Both PASS (these test only the pure parsing/computation functions, not the full async pipeline).

- [ ] **Step 5: Commit**

```bash
git add aion-chat/digest_v2.py aion-chat/tests/test_digest_v2.py
git commit -m "feat(memory-v2): atomic digest engine with Agent A/B split and intensity"
```

---

### Task 4: Wire V2 digest into the app and update recall

**Files:**
- Modify: `aion-chat/main.py` (switch auto_digest to v2)
- Modify: `aion-chat/routes/memories.py` (add v2 endpoints, keep v1 endpoints working)
- Modify: `aion-chat/memory.py` (update recall to read from memory_cards)

- [ ] **Step 1: Update main.py to use V2 auto_digest**

Find the auto_digest scheduling in `aion-chat/main.py` and add a conditional switch. Search for `auto_digest` import and replace with:

```python
from digest_v2 import auto_digest_v2 as auto_digest
```

Keep the old import commented out so it's easy to switch back.

- [ ] **Step 2: Update recall_memories in memory.py to read from memory_cards**

In `aion-chat/memory.py`, modify `recall_memories` (around line 56-102). The key changes:
- Read from `memory_cards` instead of `memories`
- Add filter: exclude cards with `status='merged'`
- Add filter: exclude cards that have an `aggregated_into` link (check `memory_links`)
- Add `status_weight` to the scoring formula

Replace the SQL query inside `recall_memories`:

```python
        cur = await db.execute(
            "SELECT c.id, c.content, c.type, c.created_at, c.embedding, c.keywords, "
            "c.importance, c.source_start_ts, c.source_end_ts, c.unresolved, c.status, c.intensity_score "
            "FROM memory_cards c "
            "WHERE c.embedding IS NOT NULL AND c.status != 'merged' "
            "AND c.id NOT IN (SELECT from_id FROM memory_links WHERE relation='aggregated_into')"
        )
```

And add `status_weight` after the time decay:

```python
        status_weight = 0.3 if row["status"] == "closed" else 1.0
        final_score = base_score * decay * status_weight
```

- [ ] **Step 3: Update build_surfacing_memories to read from memory_cards**

Same pattern: change table name from `memories` to `memory_cards`, add status filters. In priority 2 (topic-related), prefer aggregate cards by adding `ORDER BY (CASE WHEN type='aggregate' THEN 0 ELSE 1 END)`.

- [ ] **Step 4: Add V2 card endpoints to routes/memories.py**

Add new endpoints alongside existing ones:

```python
from memory_cards import (
    create_card, get_card, update_card, delete_card, list_cards,
    update_card_status, create_link, get_all_links,
)

@router.get("/api/v2/cards")
async def list_memory_cards(status: str = None, card_type: str = None):
    return await list_cards(status=status, card_type=card_type)

@router.get("/api/v2/cards/{card_id}")
async def get_memory_card(card_id: str):
    card = await get_card(card_id)
    if not card:
        return {"ok": False, "message": "卡片不存在"}
    card["links"] = await get_all_links(card_id)
    return card

@router.post("/api/v2/cards")
async def create_memory_card(body: MemoryCreate):
    card = await create_card(content=body.content, card_type=body.type)
    return card

@router.put("/api/v2/cards/{card_id}")
async def update_memory_card(card_id: str, body: MemoryUpdate):
    fields = {}
    if body.content is not None:
        fields["content"] = body.content
    if body.type is not None:
        fields["type"] = body.type
    if body.keywords is not None:
        fields["keywords"] = body.keywords
    if body.importance is not None:
        fields["importance"] = body.importance
    if body.unresolved is not None:
        fields["unresolved"] = 1 if body.unresolved else 0
    await update_card(card_id, **fields)
    return {"ok": True, "id": card_id}

@router.delete("/api/v2/cards/{card_id}")
async def delete_memory_card(card_id: str):
    await delete_card(card_id)
    return {"ok": True}

@router.patch("/api/v2/cards/{card_id}/status")
async def change_card_status(card_id: str, status: str):
    ok = await update_card_status(card_id, status)
    return {"ok": ok}

@router.get("/api/v2/cards/{card_id}/links")
async def get_card_links(card_id: str):
    return await get_all_links(card_id)

@router.post("/api/v2/cards/{card_id}/links")
async def add_card_link(card_id: str, to_id: str, relation: str):
    link = await create_link(card_id, to_id, relation)
    return link

@router.post("/api/v2/digest")
async def trigger_digest_v2():
    from digest_v2 import manual_digest_v2
    return await manual_digest_v2()
```

- [ ] **Step 5: Update [MEMORY:...] handler in chat.py**

In `aion-chat/routes/chat.py` around line 1076-1100 (and the duplicate ~2004), change the memory recording to use `create_card` instead of direct SQL insert to `memories`:

```python
            # 检测 [MEMORY:xxx] 记忆录入指令
            memory_matches = MEMORY_CMD_PATTERN.findall(full_text)
            if memory_matches:
                full_text = MEMORY_CMD_PATTERN.sub("", full_text).strip()
                for mem_content in memory_matches:
                    mem_content = mem_content.strip()
                    if mem_content:
                        from memory_cards import create_card
                        card = await create_card(
                            content=mem_content,
                            card_type="event",
                            source_conv=conv_id,
                            importance=0.5,
                        )
                        mem_data = {"id": card["id"], "content": mem_content, "type": card["type"],
                                    "created_at": card["created_at"], "keywords": card["keywords"],
                                    "importance": card["importance"],
                                    "source_start_ts": None, "source_end_ts": None}
                        await manager.broadcast({"type": "memory_added", "data": mem_data})
                        mr_data = {'type': 'memory_record', 'msg_id': ai_msg_id, 'content': mem_content, 'mem_id': card["id"]}
                        await _q.put(mr_data)
                        print(f"[MEMORY] AI 录入卡片: {mem_content[:50]}")
```

Apply the same change to the duplicate block around line 2004.

- [ ] **Step 6: Rename old memories table**

Add to `database.py` init_db, after the new table creation:

```python
        # Rename old memories table for reference (one-time migration)
        try:
            await db.execute("ALTER TABLE memories RENAME TO memories_v1")
        except:
            pass  # Already renamed or doesn't exist
```

**Important:** This must go AFTER the new `memory_cards` table creation and AFTER the old `memories` table block (so the old table gets its columns added first).

- [ ] **Step 7: Test manually**

Run the app, trigger a manual digest via the API:
```bash
curl -X POST http://localhost:8000/api/v2/digest
```

Verify cards appear in:
```bash
curl http://localhost:8000/api/v2/cards
```

- [ ] **Step 8: Commit**

```bash
git add aion-chat/main.py aion-chat/routes/memories.py aion-chat/memory.py aion-chat/routes/chat.py aion-chat/database.py
git commit -m "feat(memory-v2): wire V2 digest, update recall to use memory_cards, add V2 API"
```

---

## Phase 2: Event Lifecycle

### Task 5: Lifecycle detection in digest pipeline

**Files:**
- Modify: `aion-chat/digest_v2.py` (add lifecycle detection step after relationship matching)
- Create: `aion-chat/tests/test_lifecycle.py`

- [ ] **Step 1: Write test for lifecycle detection**

Create `aion-chat/tests/test_lifecycle.py`:

```python
import asyncio
import json

def test_parse_lifecycle_judgment():
    from digest_v2 import _parse_lifecycle_judgment

    raw = json.dumps({
        "should_close": True,
        "confidence": 0.9,
        "relation": "follow_up",
    })
    result = _parse_lifecycle_judgment(raw)
    assert result["should_close"] is True
    assert result["confidence"] == 0.9
    assert result["relation"] == "follow_up"

    # Invalid input
    result = _parse_lifecycle_judgment("not json")
    assert result["should_close"] is False

if __name__ == "__main__":
    test_parse_lifecycle_judgment()
    print("PASS: test_parse_lifecycle_judgment")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd aion-chat && python tests/test_lifecycle.py`
Expected: FAIL — `_parse_lifecycle_judgment` not defined yet.

- [ ] **Step 3: Add lifecycle detection functions to digest_v2.py**

Add to `aion-chat/digest_v2.py`:

```python
def _parse_lifecycle_judgment(raw) -> dict:
    """Parse the lifecycle AI judgment output."""
    default = {"should_close": False, "confidence": 0.0, "relation": "related"}
    if isinstance(raw, dict):
        obj = raw
    elif isinstance(raw, str):
        try:
            obj = json.loads(raw.strip())
        except (json.JSONDecodeError, ValueError):
            return default
    else:
        return default
    if not isinstance(obj, dict):
        return default
    return {
        "should_close": bool(obj.get("should_close", False)),
        "confidence": float(obj.get("confidence", 0.0)),
        "relation": obj.get("relation", "related"),
    }


def _build_lifecycle_prompt(new_content: str, candidate_content: str) -> str:
    """Build prompt for lifecycle judgment: should the old card be closed?"""
    return (
        f"判断新事件是否意味着旧事件已完结。\n\n"
        f"旧卡片：{candidate_content}\n"
        f"新卡片：{new_content}\n\n"
        f"输出 JSON：\n"
        f'- "should_close": true/false（新事件是否表示旧事件已完结/实现/取消）\n'
        f'- "confidence": 0.0-1.0（你的确信度）\n'
        f'- "relation": "follow_up"（后续进展）或 "related"（仅相关）\n\n'
        f"严格只输出 JSON 对象。"
    )
```

Then modify the relationship matching section inside `_do_digest_v2` — after finding matches with `_find_matching_open_cards`, add lifecycle detection:

```python
            # Relationship matching + lifecycle detection
            if vec:
                matches = await _find_matching_open_cards(
                    ac["content"], vec, auto_threshold, ask_threshold
                )
                for match in matches[:3]:
                    if match["id"] == card["id"]:
                        continue
                    if match["auto"]:
                        # High similarity — ask AI if this closes the old card
                        lifecycle_prompt = _build_lifecycle_prompt(ac["content"], match["content"])
                        try:
                            lifecycle_raw = await call_sentinel(lifecycle_prompt)
                            judgment = _parse_lifecycle_judgment(lifecycle_raw)
                        except Exception:
                            judgment = {"should_close": False, "confidence": 0.0, "relation": "follow_up"}

                        relation = judgment.get("relation", "follow_up")
                        await create_link(match["id"], card["id"], relation)

                        if judgment["should_close"] and judgment["confidence"] >= auto_threshold:
                            await update_card_status(match["id"], "closed")
                            print(f"[digest_v2] Auto-closed {match['id'][:20]} (conf={judgment['confidence']})")
                        elif judgment["should_close"] and judgment["confidence"] >= ask_threshold:
                            # Store for reflection message to ask user
                            pending_closes.append({
                                "old_id": match["id"], "old_content": match["content"],
                                "new_content": ac["content"], "confidence": judgment["confidence"],
                            })
                    else:
                        await create_link(match["id"], card["id"], "related")
```

Add `pending_closes = []` at the start of the group loop, and include pending close questions in the reflection message.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd aion-chat && python tests/test_lifecycle.py`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add aion-chat/digest_v2.py aion-chat/tests/test_lifecycle.py
git commit -m "feat(memory-v2): event lifecycle detection in digest pipeline"
```

---

### Task 6: Aggregate generation

**Files:**
- Modify: `aion-chat/memory_cards.py` (add aggregate helper functions)
- Modify: `aion-chat/digest_v2.py` (trigger aggregate after lifecycle)
- Create: `aion-chat/tests/test_aggregate.py`

- [ ] **Step 1: Write test for aggregate chain detection**

Create `aion-chat/tests/test_aggregate.py`:

```python
import asyncio

async def test_get_follow_up_chain():
    import tempfile, os, config
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    db_path = f.name
    f.close()
    config.DB_PATH = db_path
    from database import init_db
    await init_db()

    try:
        from memory_cards import create_card, create_link, get_follow_up_chain

        c1 = await create_card(content="感冒了", card_type="event", embed=False)
        c2 = await create_card(content="还在发烧", card_type="event", embed=False)
        c3 = await create_card(content="感冒好了", card_type="event", embed=False)
        await create_link(c1["id"], c2["id"], "follow_up")
        await create_link(c2["id"], c3["id"], "follow_up")

        chain = await get_follow_up_chain(c1["id"])
        assert len(chain) == 3
        assert chain[0]["id"] == c1["id"]
        assert chain[2]["id"] == c3["id"]
    finally:
        os.unlink(db_path)

async def test_should_aggregate():
    import tempfile, os, config
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    db_path = f.name
    f.close()
    config.DB_PATH = db_path
    from database import init_db
    await init_db()

    try:
        from memory_cards import create_card, create_link, should_generate_aggregate

        c1 = await create_card(content="事件A", card_type="event", embed=False)
        c2 = await create_card(content="事件B", card_type="event", embed=False)
        await create_link(c1["id"], c2["id"], "follow_up")
        # Only 2 in chain — not enough
        assert not await should_generate_aggregate(c1["id"])

        c3 = await create_card(content="事件C", card_type="event", embed=False)
        await create_link(c2["id"], c3["id"], "follow_up")
        # Now 3 — should aggregate
        assert await should_generate_aggregate(c1["id"])
    finally:
        os.unlink(db_path)

if __name__ == "__main__":
    asyncio.run(test_get_follow_up_chain())
    print("PASS: test_get_follow_up_chain")
    asyncio.run(test_should_aggregate())
    print("PASS: test_should_aggregate")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd aion-chat && python tests/test_aggregate.py`
Expected: FAIL — `get_follow_up_chain` not defined.

- [ ] **Step 3: Add aggregate helpers to memory_cards.py**

Add to `aion-chat/memory_cards.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd aion-chat && python tests/test_aggregate.py`
Expected: Both PASS.

- [ ] **Step 5: Wire aggregate generation into digest_v2.py**

At the end of `_do_digest_v2`, after all groups are processed but before the reflection, add:

```python
    # Check for chains that need aggregate generation
    from memory_cards import get_follow_up_chain, should_generate_aggregate, create_aggregate_for_chain
    processed_chains = set()
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT DISTINCT from_id FROM memory_links WHERE relation='follow_up'"
        )
        chain_roots = [row["from_id"] for row in await cur.fetchall()]
    for root_id in chain_roots:
        if root_id in processed_chains:
            continue
        if await should_generate_aggregate(root_id):
            chain = await get_follow_up_chain(root_id)
            chain_contents = [c["content"] for c in chain]
            # Generate aggregate summary via AI
            agg_prompt = (
                f"将以下事件链总结为一句话，包含时间跨度和最终状态：\n"
                + "\n".join(f"- {c}" for c in chain_contents)
                + "\n\n只输出总结文本，不要 JSON 或其他格式。"
            )
            try:
                from sentinel import call_sentinel_text
                agg_summary = await call_sentinel_text(agg_prompt)
                agg_summary = agg_summary.strip().strip('"')
            except Exception:
                agg_summary = " → ".join(chain_contents)
            agg_card = await create_aggregate_for_chain(chain, agg_summary)
            processed_chains.update(c["id"] for c in chain)
            print(f"[digest_v2] Created aggregate: {agg_summary[:60]}")
```

- [ ] **Step 6: Commit**

```bash
git add aion-chat/memory_cards.py aion-chat/digest_v2.py aion-chat/tests/test_aggregate.py
git commit -m "feat(memory-v2): aggregate generation for follow_up chains ≥3"
```

---

## Phase 3: Agent Separation

### Task 7: Configurable agent split mode

**Files:**
- Modify: `aion-chat/digest_v2.py` (already has split_mode support, needs unified mode implementation)

- [ ] **Step 1: Implement unified mode prompt**

The `separate` mode already works (Agent A for split, Agent B for emotion). Add unified mode — a single prompt that outputs both cards and emotions together.

In `digest_v2.py`, add:

```python
def _build_unified_prompt(messages_text: str, user_name: str, ai_name: str, persona_block: str) -> str:
    """Build a single prompt that does atomic split + emotion in one call."""
    return (
        f"{persona_block}"
        f"你是一个记忆拆分专家。请将下面的对话拆分成独立的原子记忆卡片，每张卡片只记录一件事。\n\n"
        f"规则：\n"
        f"- 每张卡片的 content 应是一个完整的陈述句，包含日期和必要上下文\n"
        f"- 使用 \"{user_name}\" 和 \"{ai_name}\" 指代双方\n"
        f"- type: event/preference/emotion/promise/plan/fact\n"
        f"- keywords: 2-6 个核心关键词，禁止人名和泛指词\n"
        f"- importance: 0.0-1.0，默认 0.3\n"
        f"- unresolved: 未完成=true，已发生=false\n"
        f"- valence: -1.0~1.0（正=正面情绪，负=负面）\n"
        f"- arousal: -1.0~1.0（正=高能量，负=低能量）\n\n"
        f"输出 JSON 数组，每个元素：\n"
        f'{{"content":"...","type":"...","keywords":[...],"importance":0.X,"unresolved":false,"valence":0.X,"arousal":0.X}}\n\n'
        f"严格只输出 JSON 数组。\n\n"
        f"【对话记录】：\n{messages_text}"
    )
```

Then modify the group processing logic to use this when `split_mode == "unified"`:

```python
        if split_mode == "unified":
            unified_prompt = _build_unified_prompt(messages_text, user_name, ai_name, persona_block)
            try:
                raw_u = await simple_ai_call([{"role": "user", "content": unified_prompt}], model_key)
            except Exception as e:
                print(f"[digest_v2] Unified agent failed: {e}")
                save_digest_anchor(source_end_ts)
                continue
            atomic_cards = _parse_atomic_cards(raw_u)
            # Extract emotion from the unified output
            emotions = []
            for ac in atomic_cards:
                emotions.append({
                    "valence": max(-1.0, min(1.0, float(ac.get("valence", 0.0)))),
                    "arousal": max(-1.0, min(1.0, float(ac.get("arousal", 0.0)))),
                })
        else:
            # Existing separate mode logic...
```

- [ ] **Step 2: Add valence/arousal to _parse_atomic_cards output**

Update `_parse_atomic_cards` to also extract valence/arousal when present (for unified mode):

```python
    valid.append({
        "content": item["content"].strip(),
        "type": item.get("type", "event"),
        "keywords": item.get("keywords", []),
        "importance": float(item.get("importance", 0.5)),
        "unresolved": 1 if item.get("unresolved", False) else 0,
        "valence": float(item.get("valence", 0.0)),
        "arousal": float(item.get("arousal", 0.0)),
    })
```

- [ ] **Step 3: Test both modes manually**

Set `split_mode: "unified"` in settings.json, trigger digest, verify cards have valence/arousal.
Set `split_mode: "separate"`, trigger digest, verify same.

- [ ] **Step 4: Commit**

```bash
git add aion-chat/digest_v2.py
git commit -m "feat(memory-v2): configurable unified/separate agent mode for digest"
```

---

## Phase 4: Active Retrieval + Intensity

### Task 8: Active retrieval markup parsing

**Files:**
- Modify: `aion-chat/routes/chat.py` (add `[RECALL:]`, `[EXPAND:]`, `[TIMELINE:]`, `[ORGANIZE:]` patterns)
- Create: `aion-chat/active_recall.py` (retrieval functions)
- Create: `aion-chat/tests/test_active_recall.py`

- [ ] **Step 1: Write test for retrieval functions**

Create `aion-chat/tests/test_active_recall.py`:

```python
import asyncio

async def setup_test_db():
    import tempfile, os, config
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    db_path = f.name
    f.close()
    config.DB_PATH = db_path
    from database import init_db
    await init_db()
    return db_path

async def test_expand_memory():
    db_path = await setup_test_db()
    try:
        from memory_cards import create_card, create_link, create_aggregate_for_chain
        from active_recall import expand_memory

        c1 = await create_card(content="感冒了", card_type="event", embed=False)
        c2 = await create_card(content="发烧了", card_type="event", embed=False)
        c3 = await create_card(content="好了", card_type="event", embed=False)
        await create_link(c1["id"], c2["id"], "follow_up")
        await create_link(c2["id"], c3["id"], "follow_up")
        agg = await create_aggregate_for_chain([c1, c2, c3], "感冒事件")

        result = await expand_memory(agg["id"])
        assert len(result) == 3
        assert result[0]["content"] == "感冒了"
    finally:
        import os; os.unlink(db_path)

async def test_get_timeline():
    db_path = await setup_test_db()
    try:
        from memory_cards import create_card, create_link
        from active_recall import get_timeline

        c1 = await create_card(content="计划A", card_type="plan", embed=False)
        c2 = await create_card(content="执行A", card_type="event", embed=False)
        await create_link(c1["id"], c2["id"], "follow_up")

        timeline = await get_timeline(c1["id"])
        assert len(timeline) == 2
        assert timeline[0]["content"] == "计划A"
        assert timeline[1]["content"] == "执行A"
    finally:
        import os; os.unlink(db_path)

if __name__ == "__main__":
    asyncio.run(test_expand_memory())
    print("PASS: test_expand_memory")
    asyncio.run(test_get_timeline())
    print("PASS: test_get_timeline")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd aion-chat && python tests/test_active_recall.py`
Expected: FAIL — module doesn't exist.

- [ ] **Step 3: Implement active_recall.py**

Create `aion-chat/active_recall.py`:

```python
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


def format_recall_results(cards: list[dict]) -> str:
    """Format card results for injection into prompt."""
    if not cards:
        return ""
    lines = []
    for c in cards:
        status_tag = f"[{c.get('status', 'open')}]" if c.get("status") == "closed" else ""
        lines.append(f"- {c['content']} {status_tag}")
    return "\n".join(lines)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd aion-chat && python tests/test_active_recall.py`
Expected: Both PASS.

- [ ] **Step 5: Add markup patterns to chat.py**

In `aion-chat/routes/chat.py`, add new patterns near the existing command patterns (around line 25):

```python
RECALL_CMD_PATTERN = re.compile(r'\[RECALL:([^\]]+)\]')
EXPAND_CMD_PATTERN = re.compile(r'\[EXPAND:([^\]]+)\]')
TIMELINE_CMD_PATTERN = re.compile(r'\[TIMELINE:([^\]]+)\]')
ORGANIZE_CMD_PATTERN = re.compile(r'\[ORGANIZE:([^\]]+)\]')
```

Then in the response processing section (after `[MEMORY:...]` handling), add:

```python
            # 检测主动检索指令
            from active_recall import search_memory, expand_memory, get_timeline, organize_memories, format_recall_results
            recall_matches = RECALL_CMD_PATTERN.findall(full_text)
            if recall_matches:
                full_text = RECALL_CMD_PATTERN.sub("", full_text).strip()
                for keywords_str in recall_matches:
                    kws = [k.strip() for k in keywords_str.split(",") if k.strip()]
                    results = await search_memory(kws)
                    if results:
                        recall_context = format_recall_results(results)
                        # Store for injection in next turn
                        # (implementation depends on how context is managed)
                        print(f"[RECALL] Found {len(results)} cards for: {kws}")

            expand_matches = EXPAND_CMD_PATTERN.findall(full_text)
            if expand_matches:
                full_text = EXPAND_CMD_PATTERN.sub("", full_text).strip()
                for card_id in expand_matches:
                    fragments = await expand_memory(card_id.strip())
                    if fragments:
                        print(f"[EXPAND] Expanded {card_id}: {len(fragments)} fragments")

            timeline_matches = TIMELINE_CMD_PATTERN.findall(full_text)
            if timeline_matches:
                full_text = TIMELINE_CMD_PATTERN.sub("", full_text).strip()
                for card_id in timeline_matches:
                    chain = await get_timeline(card_id.strip())
                    if chain:
                        print(f"[TIMELINE] Chain for {card_id}: {len(chain)} cards")

            organize_matches = ORGANIZE_CMD_PATTERN.findall(full_text)
            if organize_matches:
                full_text = ORGANIZE_CMD_PATTERN.sub("", full_text).strip()
                for keywords_str in organize_matches:
                    kws = [k.strip() for k in keywords_str.split(",") if k.strip()]
                    result = await organize_memories(kws)
                    print(f"[ORGANIZE] {result}")
```

- [ ] **Step 6: Add new markup commands to system prompt**

In the abilities block of chat.py (around line 360 and duplicates), add after the `[MEMORY:...]` ability:

```python
    abilities.append(f"[RECALL:关键词1,关键词2] — 当你需要查找记忆库中的信息时使用。系统会返回匹配的记忆卡片。")
    abilities.append(f"[ORGANIZE:关键词] — 当你发现记忆库中某个话题的记忆需要整理时使用。系统会自动合并和归类相关记忆。")
```

- [ ] **Step 7: Test manually**

Start the app and test in a conversation. Verify:
1. AI can output `[RECALL:咖啡]` and results are logged
2. `[ORGANIZE:咖啡]` triggers organization logic

- [ ] **Step 8: Commit**

```bash
git add aion-chat/active_recall.py aion-chat/tests/test_active_recall.py aion-chat/routes/chat.py
git commit -m "feat(memory-v2): active retrieval and organization via markup commands"
```

---

### Task 9: Frontend updates for V2

**Files:**
- Modify: `aion-chat/static/memory.html`

- [ ] **Step 1: Update API calls to use V2 endpoints**

Change fetch URLs from `/api/memories` to `/api/v2/cards`. Update the rendering to show:
- Card status badge (open/closed/merged) with color coding
- Status filter buttons at the top
- Aggregate cards with an expand button

- [ ] **Step 2: Add status filter UI**

Add filter buttons above the search bar:

```html
<div class="mem-filters" style="display:flex;gap:6px;margin-bottom:8px;">
  <button class="filter-btn active" data-filter="all">全部</button>
  <button class="filter-btn" data-filter="open">进行中</button>
  <button class="filter-btn" data-filter="closed">已完结</button>
</div>
```

- [ ] **Step 3: Add aggregate expand functionality**

For cards with `type=aggregate`, show an expand button that calls `/api/v2/cards/{id}/links` and renders child cards inline.

- [ ] **Step 4: Add link visualization**

When viewing a card's detail, show linked cards with relation type labels.

- [ ] **Step 5: Test in browser**

Open the memory page, verify:
- Status filters work
- Aggregate cards expand to show children
- Links are visible
- CRUD operations work with new API

- [ ] **Step 6: Commit**

```bash
git add aion-chat/static/memory.html
git commit -m "feat(memory-v2): update frontend for card-based memory with status filters and aggregates"
```

---

### Task 10: Final integration test and cleanup

**Files:**
- Modify: `aion-chat/tools/rebuild_memories.py` (adapt for V2)

- [ ] **Step 1: Update rebuild_memories.py for V2**

Change the rebuild script to:
- Clear `memory_cards` and `memory_links` tables instead of `memories`
- Call `_do_digest_v2` instead of `_do_digest`
- Reset the digest anchor

- [ ] **Step 2: End-to-end test**

1. Start the app on the feature branch
2. Send several messages in a conversation
3. Wait 30 minutes for auto-digest (or trigger manually via `/api/v2/digest`)
4. Verify:
   - Atomic cards created (not conversation summaries)
   - Links created between related cards
   - AI reflection message appears
   - `[MEMORY:...]` creates a card in `memory_cards`
   - Recall returns cards (not old memories)
   - Frontend shows cards with status and links

- [ ] **Step 3: Clean up imports and dead code**

Remove any unused v1-only imports from modified files. Ensure `memories_v1` table is preserved but no new code writes to it.

- [ ] **Step 4: Final commit**

```bash
git add -A
git commit -m "feat(memory-v2): final integration, rebuild script update, cleanup"
```
