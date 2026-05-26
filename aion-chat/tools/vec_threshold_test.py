"""
诊断脚本：测试不同向量相似度阈值下的聚类效果，支持 reranker 二次过滤。
不修改数据库，只读取 + 打印样例。

用法:
    python tools/vec_threshold_test.py                        # 纯向量 + 关键词对比
    python tools/vec_threshold_test.py --rerank               # 加 reranker 过滤对比
    python tools/vec_threshold_test.py --rerank --rr-th 0.5   # 自定义 reranker 阈值
    python tools/vec_threshold_test.py --thresholds 0.6,0.7,0.8
"""

import argparse
import asyncio
import json
import math
import struct
import sqlite3
import sys
from pathlib import Path
from collections import defaultdict

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.stdout.reconfigure(encoding='utf-8')

from config import DB_PATH


def _unpack(blob: bytes) -> list[float]:
    n = len(blob) // 4
    return list(struct.unpack(f'{n}f', blob))


def cosine_sim(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def keyword_overlap(kw_a, kw_b):
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


def load_cards(limit=0):
    db = sqlite3.connect(str(DB_PATH))
    db.row_factory = sqlite3.Row
    sql = ("SELECT id, content, type, status, keywords, importance, created_at, embedding "
           "FROM memory_cards WHERE type != 'aggregate' ORDER BY created_at DESC")
    if limit > 0:
        sql += f" LIMIT {limit}"
    rows = db.execute(sql).fetchall()
    db.close()

    cards = []
    for r in rows:
        c = dict(r)
        try:
            c['_kws'] = json.loads(c['keywords']) if c['keywords'] else []
        except Exception:
            c['_kws'] = []
        c['_vec'] = _unpack(c['embedding']) if c['embedding'] else None
        cards.append(c)
    return cards


def cluster_at_threshold(cards, vec_threshold, kw_min=2, time_window=7*86400,
                         max_cluster=12, min_cluster=3, overlap_ratio=0.7,
                         mode='vec_only'):
    """
    mode:
      'vec_only'  — 纯向量
      'kw_only'   — 纯关键词（当前线上）
      'kw+vec'    — 关键词优先，向量兜底
      'kw&vec'    — 关键词 AND 向量同时满足
    """
    by_type = defaultdict(list)
    for c in cards:
        by_type[c['type']].append(c)

    def match(ca, cb):
        gap = abs(cb['created_at'] - ca['created_at'])
        if gap > time_window:
            return False

        kw_ok = (ca['_kws'] and cb['_kws'] and
                 keyword_overlap(ca['_kws'], cb['_kws']) >= kw_min)
        vec_ok = False
        if ca['_vec'] and cb['_vec']:
            vec_ok = cosine_sim(ca['_vec'], cb['_vec']) >= vec_threshold

        if mode == 'vec_only':
            return vec_ok
        elif mode == 'kw_only':
            return kw_ok
        elif mode == 'kw+vec':
            return kw_ok or vec_ok
        elif mode == 'kw&vec':
            return kw_ok and vec_ok
        return False

    seeded = set()
    clusters = []
    for typ, tcards in by_type.items():
        tcards.sort(key=lambda c: c['created_at'])
        for seed in tcards:
            if seed['id'] in seeded:
                continue
            seeded.add(seed['id'])
            group = [seed]
            seen = {seed['id']}
            for cand in tcards:
                if cand['id'] in seen:
                    continue
                if len(group) >= max_cluster:
                    break
                if any(match(g, cand) for g in group):
                    group.append(cand)
                    seen.add(cand['id'])
            if len(group) < min_cluster:
                continue
            dominated = False
            for existing in clusters:
                ex_ids = {c['id'] for c in existing}
                if len(seen & ex_ids) / len(group) >= overlap_ratio:
                    dominated = True
                    break
            if not dominated:
                clusters.append(group)
    return clusters


def kw_post_filter(clusters, kw_min=2, min_cluster=3):
    """后置关键词过滤：seed 和成员的关键词重叠 >= kw_min 才保留。"""
    total_before = sum(len(g) for g in clusters)
    filtered = []
    dropped_clusters = 0

    for group in clusters:
        seed = group[0]
        if len(group) <= 1:
            continue
        kept = [seed]
        for m in group[1:]:
            overlap = keyword_overlap(seed['_kws'], m['_kws'])
            m['_kw_overlap'] = overlap
            if overlap >= kw_min:
                kept.append(m)
        if len(kept) >= min_cluster:
            filtered.append(kept)
        else:
            dropped_clusters += 1

    total_after = sum(len(g) for g in filtered)
    removed = total_before - total_after
    print(f"  [kw_post] 过滤前: {len(clusters)}簇/{total_before}卡 → "
          f"过滤后: {len(filtered)}簇/{total_after}卡 | "
          f"踢掉 {removed} 张, 丢弃 {dropped_clusters} 个不足簇")
    return filtered


async def rerank_filter(clusters, rr_threshold=0.3, min_cluster=3, concurrency=10):
    """用 reranker 对每个簇做二次过滤：seed content 作 query，成员作 documents。"""
    from sentinel import fetch_rerank

    total_before = sum(len(g) for g in clusters)
    sem = asyncio.Semaphore(concurrency)

    async def _process_one(group):
        seed = group[0]
        if len(group) <= 1:
            return None
        members = group[1:]
        docs = [m['content'][:512] for m in members]
        async with sem:
            rr_results = await fetch_rerank(seed['content'][:512], docs, top_n=len(docs))
        if not rr_results:
            return group
        score_map = {rr['index']: rr['relevance_score'] for rr in rr_results}
        kept = [seed]
        for idx, m in enumerate(members):
            score = score_map.get(idx, 0.0)
            m['_rr_score'] = score
            if score >= rr_threshold:
                kept.append(m)
        return kept if len(kept) >= min_cluster else None

    # 分批并发，每批 concurrency 个
    results = []
    for i in range(0, len(clusters), concurrency):
        batch = clusters[i:i+concurrency]
        batch_results = await asyncio.gather(*[_process_one(g) for g in batch])
        results.extend(batch_results)
        done = min(i + concurrency, len(clusters))
        print(f"  [reranker] 进度: {done}/{len(clusters)}", flush=True)
    filtered = [r for r in results if r is not None]
    total_after = sum(len(g) for g in filtered)
    dropped_clusters = sum(1 for r in results if r is None)
    removed = total_before - total_after

    print(f"  [reranker] 过滤前: {len(clusters)}簇/{total_before}卡 → "
          f"过滤后: {len(filtered)}簇/{total_after}卡 | "
          f"踢掉 {removed} 张假阳性, 丢弃 {dropped_clusters} 个不足簇")
    return filtered


def print_clusters(clusters, label, max_show=5, show_rr=False):
    total_cards = sum(len(g) for g in clusters)
    print(f"\n{'='*70}")
    print(f"  {label}")
    print(f"  聚合数: {len(clusters)}  |  涉及卡片: {total_cards}")
    print(f"{'='*70}")

    for i, group in enumerate(clusters[:max_show]):
        group.sort(key=lambda c: c['created_at'])
        print(f"\n  --- 聚合 #{i+1} ({len(group)}张, type={group[0]['type']}) ---")
        for c in group:
            kws = ', '.join(c['_kws'][:4]) if c['_kws'] else '无'
            content = c['content'][:60].replace('\n', ' ')
            rr_tag = f"  rr={c['_rr_score']:.2f}" if show_rr and '_rr_score' in c else ""
            print(f"    [{c['status']:6s}] {content}  kw=[{kws}]{rr_tag}")

    if len(clusters) > max_show:
        print(f"\n  ... 还有 {len(clusters) - max_show} 个聚合未显示")


def compute_pairwise_stats(cards, sample=200):
    """随机采样卡片对，统计向量相似度分布"""
    import random
    vecs = [(c, c['_vec']) for c in cards if c['_vec']]
    if len(vecs) < 2:
        return

    random.seed(42)
    sims_same_type = []
    sims_diff_type = []
    sims_kw_match = []
    sims_kw_nomatch = []

    pairs = []
    for _ in range(min(sample * 10, len(vecs) * (len(vecs)-1) // 2)):
        i, j = random.sample(range(len(vecs)), 2)
        pairs.append((i, j))

    seen = set()
    for i, j in pairs:
        if (i, j) in seen:
            continue
        seen.add((i, j))
        ca, va = vecs[i]
        cb, vb = vecs[j]
        sim = cosine_sim(va, vb)

        if ca['type'] == cb['type']:
            sims_same_type.append(sim)
        else:
            sims_diff_type.append(sim)

        if ca['_kws'] and cb['_kws']:
            if keyword_overlap(ca['_kws'], cb['_kws']) >= 2:
                sims_kw_match.append(sim)
            else:
                sims_kw_nomatch.append(sim)

        if len(seen) >= sample:
            break

    def stats(arr, name):
        if not arr:
            print(f"  {name}: 无数据")
            return
        arr.sort()
        avg = sum(arr) / len(arr)
        p25 = arr[len(arr)//4]
        p50 = arr[len(arr)//2]
        p75 = arr[3*len(arr)//4]
        p90 = arr[int(len(arr)*0.9)]
        print(f"  {name} (n={len(arr)}): avg={avg:.3f}  p25={p25:.3f}  p50={p50:.3f}  p75={p75:.3f}  p90={p90:.3f}")

    print(f"\n{'='*70}")
    print("  向量相似度分布统计（随机采样）")
    print(f"{'='*70}")
    stats(sims_same_type, "同类型卡片")
    stats(sims_diff_type, "不同类型")
    stats(sims_kw_match, "关键词匹配≥2")
    stats(sims_kw_nomatch, "关键词不匹配")


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--thresholds', type=str, default='0.65,0.70,0.75,0.80,0.85,0.90')
    parser.add_argument('--rerank', action='store_true', help='启用 reranker 二次过滤对比')
    parser.add_argument('--rr-th', type=float, default=0.3, help='reranker 过滤阈值 (default: 0.3)')
    parser.add_argument('--max-show', type=int, default=5, help='每种模式最多展示几个簇')
    parser.add_argument('--limit', type=int, default=0, help='只取最近 N 张卡片 (0=全量)')
    args = parser.parse_args()

    thresholds = [float(x) for x in args.thresholds.split(',')]

    print("加载卡片...")
    cards = load_cards(limit=args.limit)
    print(f"共 {len(cards)} 张卡片")

    compute_pairwise_stats(cards)

    # 当前线上：纯关键词
    kw_clusters = cluster_at_threshold(cards, vec_threshold=0, mode='kw_only')
    print_clusters(kw_clusters, "当前线上: 纯关键词 (kw≥2)", max_show=args.max_show)

    # 纯向量 + 可选 reranker 过滤
    for t in thresholds:
        cs = cluster_at_threshold(cards, vec_threshold=t, mode='vec_only')
        print_clusters(cs, f"纯向量 threshold={t:.2f}", max_show=args.max_show)

        # 后置关键词过滤（始终跑，作为对照）
        import copy
        cs_kw = kw_post_filter(copy.deepcopy(cs))
        print_clusters(cs_kw, f"向量{t:.2f} + kw≥2 后置过滤", max_show=args.max_show)

        if args.rerank and cs:
            cs_rr = await rerank_filter(cs, rr_threshold=args.rr_th)
            print_clusters(cs_rr, f"向量{t:.2f} + reranker≥{args.rr_th}",
                          max_show=args.max_show, show_rr=True)

    # 关键词 + 向量兜底
    for t in thresholds:
        cs = cluster_at_threshold(cards, vec_threshold=t, mode='kw+vec')
        print_clusters(cs, f"关键词优先 + 向量兜底 threshold={t:.2f}", max_show=args.max_show)

        if args.rerank and cs:
            cs_rr = await rerank_filter(cs, rr_threshold=args.rr_th)
            print_clusters(cs_rr, f"kw+vec{t:.2f} + reranker≥{args.rr_th}",
                          max_show=args.max_show, show_rr=True)

    # 关键词 AND 向量
    for t in [0.5, 0.55, 0.6, 0.65]:
        cs = cluster_at_threshold(cards, vec_threshold=t, mode='kw&vec')
        print_clusters(cs, f"关键词 AND 向量 threshold={t:.2f}", max_show=args.max_show)


if __name__ == '__main__':
    asyncio.run(main())
