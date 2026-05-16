"""
测试脚本：对比硅基流动 Qwen3-Embedding-8B (4096维) 与当前 DashScope text-embedding-v4 (1024维)
在卡片聚类效果上的差异。

不修改数据库。

用法:
  cd aion-chat
  python tools/siliconflow_embed_test.py
  python tools/siliconflow_embed_test.py --sample 80   # 抽取卡片数量，默认 60
  python tools/siliconflow_embed_test.py --no-compare  # 只跑新模型，跳过与旧向量对比
"""

import asyncio
import json
import math
import struct
import sqlite3
import sys
import argparse
import time
from pathlib import Path
from collections import defaultdict

import httpx

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.stdout.reconfigure(encoding='utf-8')

from config import DB_PATH, get_key

# ── 硅基流动配置 ─────────────────────────────────
SF_BASE = "https://api.siliconflow.cn/v1"
SF_MODEL = "BAAI/bge-m3"
SF_DIMS = 1024


# ── 工具函数 ─────────────────────────────────────
def _unpack(blob: bytes) -> list[float]:
    n = len(blob) // 4
    return list(struct.unpack(f'{n}f', blob))


def cosine_sim(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = sum(x * x for x in b) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def keyword_overlap(kw_a: list, kw_b: list) -> int:
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


# ── 数据库读取 ───────────────────────────────────
def load_cards(sample: int) -> list[dict]:
    db = sqlite3.connect(str(DB_PATH))
    db.row_factory = sqlite3.Row
    rows = db.execute(
        "SELECT id, content, type, status, keywords, importance, created_at, embedding "
        "FROM memory_cards "
        "WHERE type != 'aggregate' AND length(content) > 10 "
        "ORDER BY created_at DESC "
        f"LIMIT {sample}"
    ).fetchall()
    db.close()

    cards = []
    for r in rows:
        c = dict(r)
        try:
            c['_kws'] = json.loads(c['keywords']) if c['keywords'] else []
        except Exception:
            c['_kws'] = []
        c['_old_vec'] = _unpack(c['embedding']) if c['embedding'] else None
        c['_new_vec'] = None
        cards.append(c)
    return cards


# ── 硅基流动 Embedding ───────────────────────────
async def fetch_embedding(text: str, client: httpx.AsyncClient, key: str) -> list[float] | None:
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    body = {
        "model": SF_MODEL,
        "input": text,
        "encoding_format": "float",
    }
    for attempt in range(3):
        try:
            resp = await client.post(f"{SF_BASE}/embeddings", headers=headers, json=body, timeout=30)
            if resp.status_code == 429:
                await asyncio.sleep(3 * (attempt + 1))
                continue
            resp.raise_for_status()
            return resp.json()["data"][0]["embedding"]
        except Exception as e:
            if attempt < 2:
                await asyncio.sleep(2)
            else:
                print(f"  [!] embedding 失败: {e}")
    return None


async def batch_embed(cards: list[dict], key: str, concurrency: int = 5) -> int:
    sem = asyncio.Semaphore(concurrency)
    ok = 0

    async def one(card: dict, client: httpx.AsyncClient):
        nonlocal ok
        async with sem:
            vec = await fetch_embedding(card['content'][:1024], client, key)
            if vec:
                card['_new_vec'] = vec
                ok += 1
            # 软限速
            await asyncio.sleep(0.25)

    async with httpx.AsyncClient() as client:
        tasks = [one(c, client) for c in cards]
        total = len(tasks)
        done = 0
        for coro in asyncio.as_completed(tasks):
            await coro
            done += 1
            if done % 10 == 0 or done == total:
                print(f"  进度 {done}/{total}  (成功 {ok})", end='\r', flush=True)

    print()
    return ok


# ── 聚类 ─────────────────────────────────────────
def cluster(cards: list[dict], vec_key: str, threshold: float,
            mode: str = 'vec_only',
            kw_min: int = 2, time_window: int = 7 * 86400,
            max_cluster: int = 12, min_cluster: int = 3,
            overlap_ratio: float = 0.7) -> list[list[dict]]:
    by_type: dict[str, list[dict]] = defaultdict(list)
    for c in cards:
        by_type[c['type']].append(c)

    def match(ca, cb) -> bool:
        if abs(cb['created_at'] - ca['created_at']) > time_window:
            return False
        va, vb = ca.get(vec_key), cb.get(vec_key)
        kw_ok = (ca['_kws'] and cb['_kws'] and
                 keyword_overlap(ca['_kws'], cb['_kws']) >= kw_min)
        vec_ok = bool(va and vb and cosine_sim(va, vb) >= threshold)

        if mode == 'vec_only':
            return vec_ok
        elif mode == 'kw_only':
            return kw_ok
        elif mode == 'kw+vec':
            return kw_ok or vec_ok
        elif mode == 'kw&vec':
            return kw_ok and vec_ok
        return False

    seeded: set[str] = set()
    clusters: list[list[dict]] = []
    for typ, tcards in by_type.items():
        tcards.sort(key=lambda c: c['created_at'])
        for seed in tcards:
            if seed['id'] in seeded:
                continue
            seeded.add(seed['id'])
            group = [seed]
            seen = {seed['id']}
            for cand in tcards:
                if cand['id'] in seen or len(group) >= max_cluster:
                    continue
                if any(match(g, cand) for g in group):
                    group.append(cand)
                    seen.add(cand['id'])
            if len(group) < min_cluster:
                continue
            if not any(len(seen & {c['id'] for c in ex}) / len(group) >= overlap_ratio
                       for ex in clusters):
                clusters.append(group)
    return clusters


# ── 相似度分布统计 ────────────────────────────────
def sim_distribution(cards: list[dict], vec_key: str, sample: int = 300):
    import random
    vecs = [(c, c[vec_key]) for c in cards if c.get(vec_key)]
    if len(vecs) < 2:
        print("  (向量数量不足，跳过分布统计)")
        return

    random.seed(42)
    same_type, diff_type, kw_match, kw_nomatch = [], [], [], []

    pairs = set()
    attempts = min(sample * 20, len(vecs) * (len(vecs) - 1) // 2)
    while len(pairs) < min(sample, len(vecs) * (len(vecs) - 1) // 2) and attempts > 0:
        attempts -= 1
        i, j = random.sample(range(len(vecs)), 2)
        if i > j:
            i, j = j, i
        if (i, j) in pairs:
            continue
        pairs.add((i, j))
        ca, va = vecs[i]
        cb, vb = vecs[j]
        sim = cosine_sim(va, vb)
        (same_type if ca['type'] == cb['type'] else diff_type).append(sim)
        if ca['_kws'] and cb['_kws']:
            (kw_match if keyword_overlap(ca['_kws'], cb['_kws']) >= 2 else kw_nomatch).append(sim)

    def stats(arr: list[float], name: str):
        if not arr:
            print(f"  {name}: 无数据")
            return
        arr.sort()
        n = len(arr)
        avg = sum(arr) / n
        print(f"  {name:22s} (n={n:4d})  avg={avg:.3f}  "
              f"p25={arr[n//4]:.3f}  p50={arr[n//2]:.3f}  "
              f"p75={arr[3*n//4]:.3f}  p90={arr[int(n*0.9)]:.3f}")

    stats(same_type,   "同类型")
    stats(diff_type,   "不同类型")
    stats(kw_match,    "关键词匹配≥2")
    stats(kw_nomatch,  "关键词不匹配")


# ── 打印聚类 ──────────────────────────────────────
def print_clusters(clusters: list[list[dict]], label: str, max_show: int = 4):
    total = sum(len(g) for g in clusters)
    print(f"\n{'='*72}")
    print(f"  {label}")
    print(f"  聚合数: {len(clusters)}  涉及卡片: {total}")
    print(f"{'='*72}")
    for i, group in enumerate(clusters[:max_show]):
        group.sort(key=lambda c: c['created_at'])
        print(f"\n  --- #{i+1} ({len(group)}张, type={group[0]['type']}) ---")
        for c in group:
            kws = ', '.join(c['_kws'][:4]) if c['_kws'] else '无'
            snippet = c['content'][:55].replace('\n', ' ')
            print(f"    [{c['status']:6s}] {snippet}  kw=[{kws}]")
    if len(clusters) > max_show:
        print(f"\n  ... 还有 {len(clusters) - max_show} 个聚合未显示")


# ── 主流程 ────────────────────────────────────────
async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--sample', type=int, default=60, help='抽取卡片数量 (default: 60)')
    parser.add_argument('--no-compare', action='store_true', help='跳过与旧向量对比')
    args = parser.parse_args()

    key = get_key("siliconflow")
    if not key:
        print("[错误] settings.json 中没有 siliconflow_key，请先配置。")
        sys.exit(1)

    print(f"加载卡片（最近 {args.sample} 张）...")
    cards = load_cards(args.sample)
    print(f"已加载 {len(cards)} 张，其中有旧向量 {sum(1 for c in cards if c['_old_vec'])} 张")

    print(f"\n调用 {SF_MODEL} 获取 {SF_DIMS} 维向量...")
    t0 = time.time()
    ok = await batch_embed(cards, key)
    elapsed = time.time() - t0
    print(f"完成：{ok}/{len(cards)} 张  耗时 {elapsed:.1f}s")

    new_cards = [c for c in cards if c['_new_vec']]
    old_cards = [c for c in cards if c['_old_vec']]

    # ── 分布对比 ─────────────────────────────────
    if not args.no_compare and old_cards:
        print(f"\n{'#'*72}")
        print(f"  旧模型 DashScope text-embedding-v4 (1024维)  n={len(old_cards)}")
        print(f"{'#'*72}")
        sim_distribution(old_cards, '_old_vec')

    print(f"\n{'#'*72}")
    print(f"  新模型 {SF_MODEL} ({SF_DIMS}维)  n={len(new_cards)}")
    print(f"{'#'*72}")
    sim_distribution(new_cards, '_new_vec')

    # ── 聚类对比 ─────────────────────────────────
    thresholds_new = [0.65, 0.70, 0.75, 0.80]
    thresholds_old = [0.65, 0.70, 0.75, 0.80]

    if not args.no_compare and old_cards:
        print(f"\n\n{'*'*72}")
        print("  旧模型聚类")
        print(f"{'*'*72}")
        kw_cs = cluster(old_cards, '_old_vec', 0, mode='kw_only')
        print_clusters(kw_cs, "纯关键词（当前线上）")
        for t in thresholds_old:
            cs = cluster(old_cards, '_old_vec', t, mode='vec_only')
            print_clusters(cs, f"旧模型 纯向量 threshold={t:.2f}")

    print(f"\n\n{'*'*72}")
    print(f"  新模型聚类 ({SF_MODEL})")
    print(f"{'*'*72}")
    kw_cs = cluster(new_cards, '_new_vec', 0, mode='kw_only')
    print_clusters(kw_cs, "纯关键词（参照）")
    for t in thresholds_new:
        cs = cluster(new_cards, '_new_vec', t, mode='vec_only')
        print_clusters(cs, f"新模型 纯向量 threshold={t:.2f}")

    # ── kw+vec 兜底 ───────────────────────────────
    print(f"\n\n{'*'*72}")
    print(f"  新模型 关键词优先 + 向量兜底")
    print(f"{'*'*72}")
    for t in [0.70, 0.75, 0.80]:
        cs = cluster(new_cards, '_new_vec', t, mode='kw+vec')
        print_clusters(cs, f"kw+vec threshold={t:.2f}")

    print("\n测试完成。以上结果不影响数据库。")


if __name__ == '__main__':
    asyncio.run(main())
