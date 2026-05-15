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
