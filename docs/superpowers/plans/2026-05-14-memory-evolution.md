# Memory Evolution 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 用 Ebbinghaus 遗忘曲线替代硬规则生命周期，修复情感评分，加入事实核查，提升召回性能。

**Architecture:** 在现有 SQLite + memory_cards 体系上渐进增强。新建 `decay_engine.py`（衰减引擎）和 `embedding_cache.py`（numpy 矩阵缓存），改造 `digest_v2.py`（prompt + 去重 + 核查）、`memory.py`（召回公式）、`routes/chat.py`（surfaced_ids 持久化）、`database.py`（字段迁移）、`memory_cards.py`（新字段）、`main.py`（接线）。

**Tech Stack:** Python 3.13, FastAPI, aiosqlite, numpy, sentinel (DashScope qwen-flash)

**Spec:** `docs/superpowers/specs/2026-05-14-memory-evolution-design.md`

---

## 文件结构

| 文件 | 动作 | 职责 |
|------|------|------|
| `database.py` | 改 | 新增 5 个字段的迁移 |
| `memory_cards.py` | 改 | `create_card` 适配新字段，新增 `VALID_STATUSES` 的 `archived` |
| `embedding_cache.py` | 新建 | numpy 矩阵管理：加载/追加/批量余弦/compact |
| `decay_engine.py` | 新建 | Ebbinghaus 衰减计算 + 后台归档循环 |
| `memory.py` | 改 | 召回公式改造，使用 embedding_cache，activation_count 更新 |
| `digest_v2.py` | 改 | prompt 重写、事实核查、surfaced 去重、移除 auto-close |
| `routes/chat.py` | 改 | 持久化 surfaced_ids 到 messages 表 |
| `main.py` | 改 | lifespan 中启动衰减引擎 + embedding_cache 初始化 |

---

### Task 1: 数据库迁移

**Files:**
- Modify: `aion-chat/database.py:192-214`（memory_cards 表定义之后）
- Modify: `aion-chat/database.py:20-41`（messages 表定义之后）

- [ ] **Step 1: 在 `init_db()` 中添加 memory_cards 新字段迁移**

在 `database.py` 第 215 行（`idx_memory_cards_type` 索引之后）追加：

```python
        # Memory Evolution: 新增字段
        for col, defn in [
            ("activation_count", "INTEGER DEFAULT 0"),
            ("last_activated", "REAL"),
            ("source", "TEXT DEFAULT 'both'"),
            ("verified", "INTEGER DEFAULT 1"),
        ]:
            try:
                await db.execute(f"ALTER TABLE memory_cards ADD COLUMN {col} {defn}")
            except:
                pass
```

- [ ] **Step 2: 添加 messages 新字段迁移**

在 `database.py` 第 41 行（messages 表的 `idx_messages_conv_id` 索引之后）追加：

```python
        try:
            await db.execute("ALTER TABLE messages ADD COLUMN surfaced_memory_ids TEXT")
        except:
            pass
```

- [ ] **Step 3: 初始化 last_activated 为 created_at**

在 memory_cards 字段迁移之后追加：

```python
        await db.execute(
            "UPDATE memory_cards SET last_activated = created_at "
            "WHERE last_activated IS NULL"
        )
```

- [ ] **Step 4: 验证迁移**

运行：`python -c "import asyncio; from database import init_db; asyncio.run(init_db()); print('OK')"`

然后用 sqlite3 确认字段存在：
```
python -c "import sqlite3; c=sqlite3.connect('data/chat.db'); print([r[1] for r in c.execute('PRAGMA table_info(memory_cards)').fetchall()])"
```

预期输出包含：`activation_count`, `last_activated`, `source`, `verified`

- [ ] **Step 5: Commit**

```
git add aion-chat/database.py
git commit -m "feat: memory_cards 和 messages 表新增字段迁移"
```

---

### Task 2: embedding_cache.py — numpy 矩阵缓存

**Files:**
- Create: `aion-chat/embedding_cache.py`

- [ ] **Step 1: 创建 embedding_cache.py**

```python
"""
Embedding 向量矩阵缓存：启动时从 DB 加载，numpy 批量计算余弦相似度。
"""

import numpy as np
import aiosqlite
from config import DB_PATH

_matrix: np.ndarray | None = None  # shape: (N, dims)
_card_ids: list[str] = []          # 与 matrix 行一一对应
_id_to_idx: dict[str, int] = {}    # card_id → row index
_dirty: bool = False               # compact 标记


async def load():
    """从 DB 加载所有 embedding 到内存矩阵。启动时调用一次。"""
    global _matrix, _card_ids, _id_to_idx, _dirty
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT id, embedding FROM memory_cards WHERE embedding IS NOT NULL"
        )
        rows = await cur.fetchall()

    if not rows:
        _matrix = None
        _card_ids = []
        _id_to_idx = {}
        return

    import struct
    vectors = []
    ids = []
    for card_id, blob in rows:
        n = len(blob) // 4
        vec = struct.unpack(f"{n}f", blob)
        vectors.append(vec)
        ids.append(card_id)

    _matrix = np.array(vectors, dtype=np.float32)
    # L2 归一化，后续用 dot 代替 cosine
    norms = np.linalg.norm(_matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    _matrix /= norms

    _card_ids = ids
    _id_to_idx = {cid: i for i, cid in enumerate(ids)}
    _dirty = False


def add(card_id: str, embedding: list[float]):
    """新卡片创建时追加到矩阵。"""
    global _matrix, _dirty
    vec = np.array(embedding, dtype=np.float32).reshape(1, -1)
    norm = np.linalg.norm(vec)
    if norm > 0:
        vec /= norm

    if _matrix is None:
        _matrix = vec
    else:
        _matrix = np.vstack([_matrix, vec])

    _card_ids.append(card_id)
    _id_to_idx[card_id] = len(_card_ids) - 1


def invalidate(card_id: str):
    """标记某行为无效（归档/删除时调用），零化向量使其不参与计算。"""
    global _dirty
    idx = _id_to_idx.get(card_id)
    if idx is not None and _matrix is not None:
        _matrix[idx] = 0.0
        _dirty = True


def batch_cosine(query_embedding: list[float]) -> list[tuple[str, float]]:
    """计算 query 与全部缓存向量的余弦相似度。返回 [(card_id, score), ...]。"""
    if _matrix is None or len(_card_ids) == 0:
        return []

    qvec = np.array(query_embedding, dtype=np.float32)
    norm = np.linalg.norm(qvec)
    if norm == 0:
        return [(cid, 0.0) for cid in _card_ids]
    qvec /= norm

    scores = _matrix @ qvec  # (N,)
    return [(cid, float(scores[i])) for i, cid in enumerate(_card_ids)]


def batch_cosine_filtered(query_embedding: list[float], card_ids: set[str]) -> list[tuple[str, float]]:
    """只对指定 card_ids 计算相似度。用于 surfaced 去重。"""
    if _matrix is None or not card_ids:
        return []

    indices = [_id_to_idx[cid] for cid in card_ids if cid in _id_to_idx]
    if not indices:
        return []

    qvec = np.array(query_embedding, dtype=np.float32)
    norm = np.linalg.norm(qvec)
    if norm == 0:
        return []
    qvec /= norm

    sub_matrix = _matrix[indices]
    scores = sub_matrix @ qvec
    return [(_card_ids[indices[i]], float(scores[i])) for i in range(len(indices))]


def compact():
    """清理无效行，重建索引。卡片较多时可选调用。"""
    global _matrix, _card_ids, _id_to_idx, _dirty
    if _matrix is None or not _dirty:
        return

    valid = []
    valid_ids = []
    for i, cid in enumerate(_card_ids):
        if np.any(_matrix[i] != 0):
            valid.append(_matrix[i])
            valid_ids.append(cid)

    if valid:
        _matrix = np.array(valid, dtype=np.float32)
    else:
        _matrix = None

    _card_ids = valid_ids
    _id_to_idx = {cid: i for i, cid in enumerate(valid_ids)}
    _dirty = False


def is_loaded() -> bool:
    return _matrix is not None


def count() -> int:
    return len(_card_ids)
```

- [ ] **Step 2: 验证加载**

```
cd aion-chat && python -c "import asyncio; from embedding_cache import load, count; asyncio.run(load()); print(f'Loaded {count()} vectors')"
```

预期：`Loaded 1949 vectors`（或接近数字）

- [ ] **Step 3: Commit**

```
git add aion-chat/embedding_cache.py
git commit -m "feat: 新建 embedding_cache，numpy 矩阵缓存加速向量计算"
```

---

### Task 3: decay_engine.py — 遗忘曲线引擎

**Files:**
- Create: `aion-chat/decay_engine.py`

- [ ] **Step 1: 创建 decay_engine.py**

```python
"""
Ebbinghaus 遗忘曲线引擎：计算卡片 vitality，后台定期归档低活跃卡片。
参考 Ombre-Brain decay_engine.py，适配 SQLite memory_cards 表。
"""

import math
import asyncio
import time
import logging

import aiosqlite
from config import DB_PATH, load_settings

logger = logging.getLogger("decay_engine")


def compute_vitality(
    importance: float,
    activation_count: int,
    last_activated: float,
    valence: float,
    arousal: float,
    unresolved: int,
    decay_lambda: float = 0.05,
    now: float = None,
) -> float:
    """
    vitality = importance × (activation_count ^ 0.3) × e^(-λ × days) × emotion_weight

    emotion_weight = 1.0 + |valence| × 0.3 + arousal × 0.2
    days = (now - last_activated) / 86400
    """
    if now is None:
        now = time.time()

    days = max(0.0, (now - (last_activated or now)) / 86400.0)
    act = max(1.0, float(activation_count or 1))
    imp = max(0.01, float(importance or 0.3))
    emotion_weight = 1.0 + abs(valence or 0.0) * 0.3 + max(0.0, arousal or 0.0) * 0.2

    return imp * (act ** 0.3) * math.exp(-decay_lambda * days) * emotion_weight


async def run_decay_cycle(
    decay_lambda: float = 0.05,
    archive_threshold: float = 0.1,
) -> dict:
    """扫描 open 卡片，vitality 低于阈值的归档。"""
    now = time.time()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT id, importance, activation_count, last_activated, "
            "valence, arousal, unresolved FROM memory_cards "
            "WHERE status = 'open'"
        )
        rows = await cur.fetchall()

    checked = 0
    archived = 0
    archived_ids = []

    for row in rows:
        if row["unresolved"]:
            continue
        checked += 1
        v = compute_vitality(
            importance=row["importance"],
            activation_count=row["activation_count"],
            last_activated=row["last_activated"],
            valence=row["valence"],
            arousal=row["arousal"],
            unresolved=row["unresolved"],
            decay_lambda=decay_lambda,
            now=now,
        )
        if v < archive_threshold:
            archived_ids.append(row["id"])

    if archived_ids:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.executemany(
                "UPDATE memory_cards SET status='archived', updated_at=? WHERE id=?",
                [(now, cid) for cid in archived_ids],
            )
            await db.commit()
        archived = len(archived_ids)

    logger.info(f"[decay] checked={checked}, archived={archived}")

    # invalidate cached embeddings
    if archived_ids:
        import embedding_cache
        for cid in archived_ids:
            embedding_cache.invalidate(cid)

    return {"checked": checked, "archived": archived}


async def decay_loop():
    """后台循环：每 6 小时执行一轮衰减。"""
    settings = load_settings()
    decay_cfg = settings.get("decay", {})
    decay_lambda = decay_cfg.get("lambda", 0.05)
    archive_threshold = decay_cfg.get("archive_threshold", 0.1)
    interval_hours = decay_cfg.get("interval_hours", 6)

    while True:
        await asyncio.sleep(interval_hours * 3600)
        try:
            result = await run_decay_cycle(decay_lambda, archive_threshold)
            logger.info(f"[decay] cycle done: {result}")
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"[decay] cycle error: {e}")
```

- [ ] **Step 2: 验证公式正确性**

```
cd aion-chat && python -c "
from decay_engine import compute_vitality
import time
now = time.time()
# 新卡片，刚创建
v1 = compute_vitality(0.5, 1, now, 0.0, 0.0, 0, now=now)
# 7天前，没被激活过
v2 = compute_vitality(0.5, 1, now - 7*86400, 0.0, 0.0, 0, now=now)
# 7天前，被激活过5次，高情感
v3 = compute_vitality(0.5, 5, now - 7*86400, 0.8, 0.6, 0, now=now)
# 30天前，低重要度
v4 = compute_vitality(0.2, 1, now - 30*86400, 0.0, 0.0, 0, now=now)
print(f'新卡片: {v1:.4f}')
print(f'7天未激活: {v2:.4f}')
print(f'7天5次激活高情感: {v3:.4f}')
print(f'30天低重要: {v4:.4f}')
print(f'阈值0.1, 30天低重要会被归档: {v4 < 0.1}')
"
```

预期：v1 > v2, v3 > v2（情感+激活加成），v4 < 0.1（会被归档）

- [ ] **Step 3: Commit**

```
git add aion-chat/decay_engine.py
git commit -m "feat: 新建 decay_engine，Ebbinghaus 遗忘曲线衰减引擎"
```

---

### Task 4: memory_cards.py — 适配新字段

**Files:**
- Modify: `aion-chat/memory_cards.py:14-67`

- [ ] **Step 1: 更新 VALID_STATUSES 和 create_card**

`memory_cards.py` 第 15 行，给 VALID_STATUSES 加 `"archived"`：

```python
VALID_STATUSES = {"open", "closed", "merged", "archived"}
```

`create_card` 函数签名新增 `source` 参数，INSERT 语句新增 `source`, `verified`, `activation_count`, `last_activated` 字段：

```python
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
    source: str = "both",
) -> dict:
    card_id = _make_card_id(content)
    now = time.time()
    keywords_json = json.dumps(keywords or [], ensure_ascii=False)
    verified = 1 if source != "ai" else 0

    vec = None
    if embed:
        vec = await get_embedding(content)

    async with get_db() as db:
        await db.execute(
            "INSERT INTO memory_cards "
            "(id, content, type, status, created_at, updated_at, source_conv, "
            "source_start_ts, source_end_ts, embedding, keywords, importance, "
            "unresolved, valence, arousal, intensity_score, "
            "source, verified, activation_count, last_activated) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (card_id, content, card_type, "open", now, now, source_conv,
             source_start_ts, source_end_ts,
             _pack_embedding(vec) if vec else None,
             keywords_json, importance, unresolved, valence, arousal, intensity_score,
             source, verified, 0, now),
        )
        await db.commit()

    # 追加到 embedding cache
    if vec:
        import embedding_cache
        embedding_cache.add(card_id, vec)

    return {
        "id": card_id, "content": content, "type": card_type, "status": "open",
        "created_at": now, "updated_at": now, "keywords": keywords_json,
        "importance": importance, "unresolved": unresolved,
        "source_start_ts": source_start_ts, "source_end_ts": source_end_ts,
        "valence": valence, "arousal": arousal, "intensity_score": intensity_score,
        "source": source, "verified": verified,
    }
```

- [ ] **Step 2: update_card 的 allowed 集合加入新字段**

`memory_cards.py` 第 96 行：

```python
    allowed = {"content", "type", "keywords", "importance", "unresolved",
               "valence", "arousal", "intensity_score", "status",
               "activation_count", "last_activated", "source", "verified"}
```

- [ ] **Step 3: Commit**

```
git add aion-chat/memory_cards.py
git commit -m "feat: memory_cards 适配新字段 source/verified/activation_count"
```

---

### Task 5: memory.py — 召回公式改造

**Files:**
- Modify: `aion-chat/memory.py:30-104`（cosine_similarity + recall_memories + build_surfacing_memories）

- [ ] **Step 1: 用 embedding_cache 替代逐条计算**

替换 `recall_memories` 函数（约第 56~104 行）：

```python
async def recall_memories(query_text: str, query_keywords: list[str] = None,
                          top_k: int = 5, threshold: float = 0.35) -> tuple[list[dict], list[dict]]:
    """
    新公式：base_score = kw×0.5 + vec×0.3 + importance×0.2, final = base × vitality
    命中的卡片 activation_count += 1
    """
    from decay_engine import compute_vitality
    import embedding_cache

    query_vec = await get_embedding(query_text)
    if not query_vec:
        return [], []
    if query_keywords is None:
        query_keywords = []

    # numpy 批量余弦
    vec_scores = {}
    if embedding_cache.is_loaded():
        for cid, score in embedding_cache.batch_cosine(query_vec):
            vec_scores[cid] = score

    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT id, content, type, created_at, keywords, "
            "importance, source_start_ts, source_end_ts, unresolved, status, "
            "intensity_score, activation_count, last_activated, valence, arousal "
            "FROM memory_cards "
            "WHERE status IN ('open', 'closed') AND embedding IS NOT NULL"
        )
        rows = await cur.fetchall()

    now_ts = time.time()
    all_scored = []
    for row in rows:
        vec_sim = vec_scores.get(row["id"], 0.0)
        kw_score = _keyword_match_score(query_keywords, row["keywords"]) if query_keywords else 0.0
        importance = float(row["importance"] or 0.5)
        base_score = kw_score * 0.5 + vec_sim * 0.3 + importance * 0.2

        vitality = compute_vitality(
            importance=importance,
            activation_count=row["activation_count"] or 0,
            last_activated=row["last_activated"] or row["created_at"],
            valence=row["valence"] or 0.0,
            arousal=row["arousal"] or 0.0,
            unresolved=row["unresolved"] or 0,
            now=now_ts,
        )
        final_score = base_score * vitality

        item = {
            "id": row["id"], "content": row["content"], "type": row["type"],
            "created_at": row["created_at"],
            "score": round(final_score, 4),
            "vec_sim": round(vec_sim, 4),
            "kw_score": round(kw_score, 4),
            "importance": round(importance, 2),
            "vitality": round(vitality, 4),
            "keywords": row["keywords"] or "",
            "source_start_ts": row["source_start_ts"],
            "source_end_ts": row["source_end_ts"],
        }
        all_scored.append(item)

    all_scored.sort(key=lambda x: x["score"], reverse=True)
    debug_top6 = all_scored[:6]
    matched = [r for r in all_scored if r["score"] >= threshold][:top_k]

    # activation_count += 1 for matched
    if matched:
        async with get_db() as db:
            for m in matched:
                await db.execute(
                    "UPDATE memory_cards SET activation_count = activation_count + 1, "
                    "last_activated = ? WHERE id = ?",
                    (now_ts, m["id"]),
                )
            await db.commit()

    return matched, debug_top6
```

- [ ] **Step 2: build_surfacing_memories 也改用 embedding_cache**

在 `build_surfacing_memories` 函数中，找到向量相似度计算的部分，改用 `embedding_cache.batch_cosine`。具体：把原来逐条 `_unpack_embedding` + `cosine_similarity` 的循环替换为：

```python
    # 话题相关（向量匹配）
    import embedding_cache
    if topic and embedding_cache.is_loaded():
        topic_vec = await get_embedding(topic)
        if topic_vec:
            all_sims = embedding_cache.batch_cosine(topic_vec)
            for cid, sim in all_sims:
                if sim >= 0.50 and cid not in added_ids:
                    # 查找对应卡片（从 open 的候选中）
                    ...
```

保留原有的 unresolved 优先 + 最近条目补充逻辑。

- [ ] **Step 3: 添加 import**

在 `memory.py` 顶部添加：

```python
import aiosqlite  # 已有
# 新增
import embedding_cache
from decay_engine import compute_vitality
```

- [ ] **Step 4: 验证召回性能提升**

复用之前的性能测试脚本思路，对比改造前后耗时。启动 Python 测试：

```
cd aion-chat && python -c "
import asyncio, time
async def test():
    import embedding_cache
    await embedding_cache.load()
    print(f'Loaded {embedding_cache.count()} vectors')

    from sentinel import get_embedding
    qvec = await get_embedding('咖啡')
    t0 = time.perf_counter()
    results = embedding_cache.batch_cosine(qvec)
    t1 = time.perf_counter()
    top5 = sorted(results, key=lambda x: -x[1])[:5]
    print(f'batch_cosine: {(t1-t0)*1000:.1f}ms')
    for cid, score in top5:
        print(f'  {cid}: {score:.4f}')
asyncio.run(test())
"
```

预期：batch_cosine < 10ms（对比之前 ~500ms）

- [ ] **Step 5: Commit**

```
git add aion-chat/memory.py
git commit -m "feat: 召回公式改用 vitality 衰减 + numpy 向量化加速"
```

---

### Task 6: digest_v2.py — Prompt 重写 + 去重 + 事实核查

**Files:**
- Modify: `aion-chat/digest_v2.py`

这是最大的改动，分多步。

- [ ] **Step 1: 重写 unified prompt**

替换 `_build_unified_prompt`（第 157~179 行）：

```python
def _build_unified_prompt(messages_text: str, user_name: str, ai_name: str, persona_block: str) -> str:
    return (
        f"{persona_block}"
        f"你是一个记忆整理专家。请将下面的对话整理成独立的记忆卡片。\n\n"
        f"【整理规则】\n"
        f"1. 每张卡片记录一件独立的事实/事件/情感/计划\n"
        f"2. content 是完整陈述句，包含日期和上下文，不少于30字\n"
        f"3. 同一主题的零散信息合并为一个卡片，不要过度碎片化\n"
        f"4. 每组对话生成 2~8 张卡片\n"
        f"5. 使用 \"{user_name}\" 和 \"{ai_name}\" 指代双方\n"
        f"6. 去除口水话、打招呼、重复信息、无实质内容的寒暄\n"
        f"7. 如果 {ai_name} 提到过去的事但 {user_name} 没有确认或补充，不要提取为卡片（可能是 AI 编造的）\n"
        f"8. 如果 {ai_name} 回忆/复述已知的旧事件且没有新信息，不要提取\n\n"
        f"【字段说明】\n"
        f"- type: event/preference/emotion/promise/plan/fact\n"
        f"- keywords: 3~6个，领域词(1-2个)在前 + 实体词(2-4个)在后\n"
        f"  禁止: 人名({user_name}, {ai_name})、泛指词(提醒、建议、完成、计划、测试)\n"
        f"- importance: 0.0~1.0，默认0.3，重大事实才给0.7+\n"
        f"- unresolved: 未完成的计划/承诺为true\n"
        f"- valence: -1.0~1.0（正=正面情绪，负=负面）\n"
        f"- arousal: -1.0~1.0（正=高能量，负=低能量）\n"
        f"- source: 信息主要来自谁\n"
        f"  \"user\" = {user_name}亲口说的/做的\n"
        f"  \"ai\" = {ai_name}单方面声称或推测的\n"
        f"  \"both\" = 双方共同参与确认的\n\n"
        f"【输出格式】JSON 数组，每个元素：\n"
        f'{{"content":"...","type":"...","keywords":[...],"importance":0.X,"unresolved":false,'
        f'"valence":0.X,"arousal":0.X,"source":"user|ai|both"}}\n\n'
        f"严格只输出 JSON 数组。\n\n"
        f"【对话记录】\n{messages_text}"
    )
```

- [ ] **Step 2: 废弃 Agent A/B 相关函数，强制 unified 模式**

在 `_do_digest_v2` 中（约第 281 行），不再读取 `split_mode`，直接使用 unified prompt：

```python
    # 废弃 separate 模式，统一用 unified prompt
    split_mode = "unified"
```

同时在 unified 分支中设置 temperature=0.0。找到 `simple_ai_call` 调用，改为传入 temperature 参数（如果 `simple_ai_call` 支持的话），或者在 prompt 调用前设置。

- [ ] **Step 3: 修复 _parse_atomic_cards 适配 source 字段**

在 `_parse_atomic_cards`（第 11~40 行）的 valid.append 中新增 source：

```python
            valid.append({
                "content": item["content"].strip(),
                "type": item.get("type", "event"),
                "keywords": item.get("keywords", []),
                "importance": float(item.get("importance", 0.5)),
                "unresolved": 1 if item.get("unresolved", False) else 0,
                "valence": float(item.get("valence", 0.0)),
                "arousal": float(item.get("arousal", 0.0)),
                "source": item.get("source", "both"),
            })
```

- [ ] **Step 4: 去重范围扩大到全局近 7 天**

替换 `_dedup_against_realtime` 函数（第 245~266 行）：

```python
async def _dedup_against_realtime(card_content: str, card_embedding: list[float],
                                   source_conv: str, threshold: float = 0.85) -> str | None:
    """全局近 7 天去重（替代原来的同 conversation 去重）"""
    import embedding_cache
    import time as _time

    if not card_embedding or not embedding_cache.is_loaded():
        return None

    cutoff = _time.time() - 7 * 86400
    all_sims = embedding_cache.batch_cosine(card_embedding)

    # 需要知道哪些卡片是近 7 天的
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT id FROM memory_cards WHERE created_at > ? AND status IN ('open','closed')",
            (cutoff,),
        )
        recent_ids = {row["id"] for row in await cur.fetchall()}

    for cid, sim in all_sims:
        if cid in recent_ids and sim >= threshold:
            return cid

    return None
```

- [ ] **Step 5: 新增 surfaced 去重逻辑**

在 `_do_digest_v2` 的 Phase 2（dedup）之后、Phase 3（建卡）之前，新增 surfaced 去重。在每个 group 循环开头读取 surfaced_ids：

```python
        # 读取本组时间范围内的 surfaced memory ids
        surfaced_set = set()
        async with get_db() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT surfaced_memory_ids FROM messages "
                "WHERE created_at >= ? AND created_at <= ? "
                "AND surfaced_memory_ids IS NOT NULL AND surfaced_memory_ids != ''",
                (source_start_ts, source_end_ts),
            )
            for row in await cur.fetchall():
                try:
                    ids = json.loads(row["surfaced_memory_ids"])
                    surfaced_set.update(ids)
                except (json.JSONDecodeError, TypeError):
                    pass
```

然后在 keep_indices 过滤后、建卡循环中，对每张新卡片做 surfaced 对比：

```python
            # surfaced 去重
            if surfaced_set and vectors[i]:
                import embedding_cache
                surfaced_sims = embedding_cache.batch_cosine_filtered(vectors[i], surfaced_set)
                best_surfaced = max((s for _, s in surfaced_sims), default=0.0)
                if best_surfaced >= 0.85:
                    print(f"[digest_v2] Skip surfaced duplicate: {ac['content'][:40]}")
                    continue
                if best_surfaced >= 0.65:
                    # 用户补充了新信息 → 建卡但创建 follow_up link
                    best_cid = max(surfaced_sims, key=lambda x: x[1])[0]
                    # 建卡后会创建 link（下面的 lifecycle 匹配会处理）
```

- [ ] **Step 6: 新增事实核查函数**

在 `digest_v2.py` 中添加 `_verify_ai_claims`：

```python
async def _verify_ai_claims(card: dict, source_start_ts: float) -> bool:
    """对 source='ai' 的卡片做事实核查。返回 True=保留, False=丢弃。"""
    if card.get("source") != "ai":
        return True

    keywords = card.get("keywords", [])
    if not keywords:
        return True

    # Step 1: 在 messages 中搜索关键词（往前 30 天）
    cutoff = source_start_ts - 30 * 86400
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT content FROM messages WHERE role='user' AND created_at > ? AND created_at < ?",
            (cutoff, source_start_ts),
        )
        user_msgs = [row["content"] for row in await cur.fetchall()]

    # 关键词匹配：至少 2 个实体词出现在历史用户消息中
    kw_hits = 0
    for kw in keywords:
        if any(kw in msg for msg in user_msgs):
            kw_hits += 1
    if kw_hits >= 2:
        return True

    # Step 2: sentinel 二次判断
    from sentinel import call_sentinel
    context_sample = "\n".join(user_msgs[-20:])[:2000] if user_msgs else "(无历史消息)"
    prompt = (
        f"判断这条记忆描述的事件在历史对话中是否有依据。\n\n"
        f"记忆内容：{card['content']}\n\n"
        f"历史用户消息（最近20条）：\n{context_sample}\n\n"
        f"输出 JSON：{{\"has_evidence\": true/false, \"reason\": \"简短理由\"}}"
    )
    try:
        result = await call_sentinel(prompt)
        if isinstance(result, dict):
            return result.get("has_evidence", True)
    except Exception as e:
        print(f"[digest_v2] verify failed: {e}")

    return True  # 核查失败时保守保留
```

- [ ] **Step 7: 在建卡流程中调用事实核查**

在 Phase 3 建卡循环中（约第 414 行 `for i in keep_indices:` 内），建卡前调用：

```python
            # 事实核查（仅 source=ai）
            if ac.get("source") == "ai":
                keep = await _verify_ai_claims(
                    {"content": ac["content"], "keywords": ac.get("keywords", []), "source": "ai"},
                    source_start_ts,
                )
                if not keep:
                    print(f"[digest_v2] 丢弃未核实卡片: {ac['content'][:40]}")
                    continue
```

- [ ] **Step 8: 建卡时传入 source 参数**

在 `create_card` 调用中新增 `source=ac.get("source", "both")`：

```python
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
                source=ac.get("source", "both"),
            )
```

- [ ] **Step 9: 移除 auto-close 硬规则**

删除第 518~539 行的 auto-close 代码块：

```python
    # Auto-close stale open cards by type
    # ... 整块删除 ...
```

这部分功能由 `decay_engine.py` 的后台循环替代。

- [ ] **Step 10: Commit**

```
git add aion-chat/digest_v2.py
git commit -m "feat: digest prompt 重写 + 事实核查 + surfaced 去重 + 移除 auto-close"
```

---

### Task 7: routes/chat.py — 持久化 surfaced_ids

**Files:**
- Modify: `aion-chat/routes/chat.py:1016-1022`（assistant 消息保存处）
- Modify: `aion-chat/routes/chat.py:1366-1368`（surfaced_ids 计算处）

- [ ] **Step 1: 把 surfaced_ids 提升为闭包外可访问的变量**

在 `send_message` 函数开头（RAG 流程之前），初始化：

```python
    _surfaced_ids_json = ""
```

在 surfaced_ids 计算完成后（约第 1368 行），保存为 JSON：

```python
        (surfaced, surfaced_ids), (_, debug_top6) = await asyncio.gather(
            _do_surfacing(), _do_recall()
        )
        _surfaced_ids_json = json.dumps(list(surfaced_ids)) if surfaced_ids else ""
```

- [ ] **Step 2: assistant 消息 INSERT 时写入 surfaced_ids**

修改第 1018 行的 INSERT 语句：

```python
                await db2.execute(
                    "INSERT INTO messages (id, conv_id, role, content, created_at, attachments, surfaced_memory_ids) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (ai_msg_id, conv_id, "assistant", full_text, now2, att_json, _surfaced_ids_json)
                )
```

注意 `_surfaced_ids_json` 需要在 `_bg_generate` 闭包内可访问。由于 `_bg_generate` 是在 surfaced_ids 赋值之后定义的内部函数，可以直接使用外层变量。如果作用域有问题，改为使用 `nonlocal` 或存在 mutable 容器中。

- [ ] **Step 3: 对其他路径的 assistant 消息保存也做同样处理**

检查 chat.py 中其他保存 assistant 消息的路径（如 send_message_quick 等），如果也有 surfaced_ids 则同样保存。没有 surfaced_ids 的路径保持空字符串即可。

- [ ] **Step 4: Commit**

```
git add aion-chat/routes/chat.py
git commit -m "feat: 持久化 surfaced_ids 到 messages 表"
```

---

### Task 8: main.py — 接线 + 集成

**Files:**
- Modify: `aion-chat/main.py:106-153`（lifespan 函数）

- [ ] **Step 1: 在 lifespan 中初始化 embedding_cache 和衰减引擎**

在 `lifespan` 函数中，`await init_db()` 之后添加：

```python
    # 初始化 embedding 矩阵缓存
    import embedding_cache
    await embedding_cache.load()
    print(f"[embedding_cache] Loaded {embedding_cache.count()} vectors")
```

在 `digest_task = asyncio.create_task(_auto_digest_loop())` 之后添加：

```python
    # 遗忘曲线后台衰减任务
    from decay_engine import decay_loop
    decay_task = asyncio.create_task(decay_loop())
```

- [ ] **Step 2: 在 yield 之后取消衰减任务**

在 `yield` 之后的清理代码中，`digest_task.cancel()` 旁边添加：

```python
    decay_task.cancel()
```

- [ ] **Step 3: 验证启动**

```
cd aion-chat && python main.py
```

预期日志输出包含：
- `[embedding_cache] Loaded 1949 vectors`
- 无报错

- [ ] **Step 4: 端到端测试**

1. 发送一条消息，确认 RAG 召回正常工作（检查响应速度是否明显提升）
2. 手动触发 digest（POST /api/v2/digest），确认：
   - 卡片生成数量在 2~8 之间
   - 卡片有非零 valence/arousal
   - source 字段有值
3. 检查数据库中新卡片的字段是否完整

- [ ] **Step 5: Commit**

```
git add aion-chat/main.py
git commit -m "feat: 接入 embedding_cache 和衰减引擎到 lifespan"
```

---

### Task 9: 清理

- [ ] **Step 1: 删除测试脚本**

```
rm aion-chat/test_fuzzy.py aion-chat/_check_db.py aion-chat/_card_stats.py aion-chat/_emotion_stats.py aion-chat/_perf_test.py
```

- [ ] **Step 2: 可选删除 ombre-brain 参考副本**

```
rm -rf _ombre-brain-ref/
```

- [ ] **Step 3: Final commit**

```
git add -A
git commit -m "chore: 清理测试脚本和参考副本"
```
