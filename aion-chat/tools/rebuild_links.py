"""
重建卡片链接和聚合（不重跑 digest，直接基于现有卡片）。
逻辑：
  1. 清除旧 follow_up / aggregated_into 链接，恢复 merged 卡片为 open
  2. 日常事件自动关闭：importance ≤ 0.4 的 event，超过 24h → closed
  3. 关键词子串匹配 + 向量兜底 → 建立 follow_up 链
  4. 同类型+关键词聚类 → 生成聚合卡片
"""

import asyncio
import json
import sys
import time
import struct
import math
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def _unpack_embedding(blob: bytes) -> list[float]:
    n = len(blob) // 4
    return list(struct.unpack(f'{n}f', blob))


def cosine_similarity(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def keyword_overlap(kw_a: list[str], kw_b: list[str]) -> int:
    """子串匹配：a 中的词是否包含在 b 的某个词中，或反过来"""
    count = 0
    for ka in kw_a:
        if not ka:
            continue
        for kb in kw_b:
            if not kb:
                continue
            if ka in kb or kb in ka:
                count += 1
                break
    return count


async def main():
    import aiosqlite
    from config import DB_PATH

    sys.stdout.reconfigure(encoding='utf-8')
    db_path = str(DB_PATH)

    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row

        # ── Step 1: 清除旧链接，恢复 merged 状态 ──
        del_fu = await db.execute("DELETE FROM memory_links WHERE relation IN ('follow_up', 'aggregated_into')")
        print(f"[1] 清除 follow_up/aggregated_into 链接: {del_fu.rowcount}")

        restored = await db.execute("UPDATE memory_cards SET status='open' WHERE status='merged'")
        print(f"[1] 恢复 merged → open: {restored.rowcount}")

        # 删除旧聚合卡片
        del_agg = await db.execute("DELETE FROM memory_cards WHERE type='aggregate'")
        print(f"[1] 删除旧 aggregate 卡片: {del_agg.rowcount}")
        await db.commit()

        # ── Step 2: 日常事件自动关闭 ──
        cutoff = time.time() - 24 * 3600
        closed = await db.execute(
            "UPDATE memory_cards SET status='closed', updated_at=? "
            "WHERE type='event' AND status='open' AND importance <= 0.4 AND created_at < ?",
            (time.time(), cutoff)
        )
        print(f"[2] 自动关闭低重要度 event: {closed.rowcount}")
        await db.commit()

        # ── Step 3: 加载所有卡片 ──
        cur = await db.execute(
            "SELECT id, content, type, status, keywords, importance, created_at, embedding "
            "FROM memory_cards ORDER BY created_at ASC"
        )
        all_cards = [dict(r) for r in await cur.fetchall()]
        print(f"[3] 加载卡片: {len(all_cards)}")

        # 解析关键词
        for c in all_cards:
            try:
                c['_kws'] = json.loads(c['keywords']) if c['keywords'] else []
            except:
                c['_kws'] = []
            c['_vec'] = _unpack_embedding(c['embedding']) if c['embedding'] else None

        # ── Step 4 + 5: 贪心聚类 → follow_up 链接 + 聚合 ──
        # 策略：按 type 分组，同 type 内以每张卡为种子，找 7 天内关键词重叠 ≥2
        # 或向量 ≥0.65 的邻居，限制每组 ≤ MAX_CLUSTER。已分配的卡不再参与。
        from collections import defaultdict
        by_type = defaultdict(list)
        for c in all_cards:
            by_type[c['type']].append(c)

        VEC_THRESHOLD = 0.65
        KW_MIN_OVERLAP = 2
        MAX_CLUSTER = 12
        MIN_CLUSTER = 3
        TIME_WINDOW = 7 * 86400

        def cards_match(ca, cb):
            time_gap = abs(cb['created_at'] - ca['created_at'])
            if time_gap > TIME_WINDOW:
                return False
            if ca['_kws'] and cb['_kws']:
                if keyword_overlap(ca['_kws'], cb['_kws']) >= KW_MIN_OVERLAP:
                    return True
            return False

        seeded = set()
        clusters = []
        OVERLAP_RATIO = 0.7  # 新组与已有聚合成员重叠超过此比例则丢弃

        for typ, cards in by_type.items():
            if typ == 'aggregate':
                continue
            cards.sort(key=lambda c: c['created_at'])
            for seed in cards:
                if seed['id'] in seeded:
                    continue
                seeded.add(seed['id'])
                group = [seed]
                seen_in_group = {seed['id']}
                for cand in cards:
                    if cand['id'] in seen_in_group:
                        continue
                    if len(group) >= MAX_CLUSTER:
                        break
                    if any(cards_match(g, cand) for g in group):
                        group.append(cand)
                        seen_in_group.add(cand['id'])
                if len(group) < MIN_CLUSTER:
                    continue
                # 检查与已有聚合的重叠度，超过阈值则丢弃（但种子已消耗）
                dominated = False
                for existing in clusters:
                    existing_ids = {c['id'] for c in existing}
                    overlap = len(seen_in_group & existing_ids)
                    if overlap / len(group) >= OVERLAP_RATIO:
                        dominated = True
                        break
                if not dominated:
                    clusters.append(group)

        # 建立 follow_up 链接（组内按时间顺序串联）
        link_count = 0
        for group in clusters:
            group.sort(key=lambda c: c['created_at'])
            for i in range(len(group) - 1):
                await db.execute(
                    "INSERT INTO memory_links (from_id, to_id, relation, created_at) VALUES (?,?,?,?)",
                    (group[i]['id'], group[i+1]['id'], 'follow_up', time.time())
                )
                link_count += 1

        await db.commit()
        print(f"[4] 建立 follow_up 链接: {link_count}")

        # 生成聚合卡片（调模型写摘要）
        from sentinel import call_sentinel_text
        agg_count = 0
        for members in clusters:
            members.sort(key=lambda c: c['created_at'])
            chain_contents = [m['content'] for m in members]
            agg_prompt = (
                f"将以下{len(members)}条记忆卡片总结为一句话，包含时间跨度和核心主题：\n"
                + "\n".join(f"- {c}" for c in chain_contents)
                + "\n\n只输出总结文本，不要 JSON 或其他格式。"
            )
            try:
                summary = await call_sentinel_text(agg_prompt)
                summary = summary.strip().strip('"')
                print(f"  [AI] {summary[:80]}")
            except Exception as e:
                print(f"  [AI失败] {e}")
                contents = [m['content'][:40] for m in members]
                summary = " → ".join(contents[:5])
                if len(contents) > 5:
                    summary += f" …（共{len(contents)}条）"

            first_ts = members[0]['created_at']
            last_ts = members[-1]['created_at']
            # 如果所有成员都 closed，聚合也 closed
            all_closed = all(m['status'] == 'closed' for m in members)
            agg_status = 'closed' if all_closed else 'open'

            import hashlib
            ts_ms = int(time.time() * 1000)
            h = hashlib.md5(summary.encode()).hexdigest()[:6]
            agg_id = f"card_{ts_ms}_{h}"

            await db.execute(
                "INSERT INTO memory_cards "
                "(id, content, type, status, created_at, updated_at, source_start_ts, source_end_ts, "
                "keywords, importance, unresolved, valence, arousal) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (agg_id, summary, 'aggregate', agg_status, time.time(), time.time(),
                 first_ts, last_ts, '[]', 0.5, 0, 0.0, 0.0)
            )

            for m in members:
                await db.execute(
                    "INSERT INTO memory_links (from_id, to_id, relation, created_at) VALUES (?,?,?,?)",
                    (m['id'], agg_id, 'aggregated_into', time.time())
                )
                await db.execute(
                    "UPDATE memory_cards SET status='merged' WHERE id=?",
                    (m['id'],)
                )

            agg_count += 1
            print(f"  聚合[{len(members)}张|{agg_status}]: {summary[:60]}")

        await db.commit()
        print(f"[5] 生成聚合卡片: {agg_count}")

        # ── 最终统计 ──
        cur = await db.execute("SELECT type, status, COUNT(*) FROM memory_cards GROUP BY type, status ORDER BY type, status")
        print("\n=== 最终分布 ===")
        for row in await cur.fetchall():
            print(f"  {row[0]:12s} {row[1]:8s} {row[2]}")
        cur = await db.execute("SELECT relation, COUNT(*) FROM memory_links GROUP BY relation")
        print("\n=== 链接统计 ===")
        for row in await cur.fetchall():
            print(f"  {row[0]:20s} {row[1]}")


if __name__ == "__main__":
    asyncio.run(main())
