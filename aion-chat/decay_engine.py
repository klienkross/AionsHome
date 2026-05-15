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
