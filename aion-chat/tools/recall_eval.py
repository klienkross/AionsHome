"""
记忆库粗筛召回率实验
测量 embedding top-K / keyword top-K / 并集 在不同 K 下的召回率
用于确认 reranker 上游候选集的合适大小

用法:
  python tools/recall_eval.py              # 默认抽 100 张
  python tools/recall_eval.py --sample 50  # 抽 50 张
  python tools/recall_eval.py --regen      # 忽略缓存重新生成查询
"""

import asyncio, json, math, struct, sys, time, random, argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.stdout.reconfigure(encoding='utf-8')

from config import DB_PATH
from sentinel import call_sentinel, get_embedding

CACHE_FILE = Path(__file__).parent / "recall_eval_data.json"
K_VALUES = [5, 10, 15, 20, 25, 30, 50]
QUERIES_PER_CARD = 2
CURRENT_K = 25  # memory.py 里 embedding top-25


# ── 数据加载 ──────────────────────────────────────────
def load_cards():
    import sqlite3
    db = sqlite3.connect(str(DB_PATH))
    db.row_factory = sqlite3.Row
    rows = db.execute(
        "SELECT id, content, type, keywords, importance, created_at, embedding "
        "FROM memory_cards "
        "WHERE type != 'aggregate' AND length(content) > 10 AND embedding IS NOT NULL "
        "ORDER BY created_at DESC"
    ).fetchall()
    db.close()
    cards = []
    for r in rows:
        c = dict(r)
        c['_kws'] = json.loads(c['keywords']) if c['keywords'] else []
        n = len(c['embedding']) // 4
        c['_vec'] = list(struct.unpack(f'{n}f', c['embedding']))
        del c['embedding']
        cards.append(c)
    return cards


# ── 相似度函数 ────────────────────────────────────────
def cosine(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return dot / (na * nb) if na and nb else 0.0


def kw_score(query_keywords, card_kws):
    if not query_keywords or not card_kws:
        return 0.0
    hit = 0
    for qk in query_keywords:
        qk_lower = qk.lower()
        for ck in card_kws:
            if qk_lower in ck.lower() or ck.lower() in qk_lower:
                hit += 1
                break
    return hit / len(query_keywords)


# ── 查询生成 ──────────────────────────────────────────
async def _gen_one(card, sem):
    async with sem:
        prompt = (
            f"你是记忆检索测试数据生成器。\n"
            f"以下是一条记忆卡片：\n「{card['content'][:300]}」\n\n"
            f"请生成 {QUERIES_PER_CARD} 条自然口语查询，模拟用户在聊天中提起这段记忆时会说的话。"
            f"同时为每条查询提取 2-4 个检索关键词（稀缺名词，禁止包含人名）。\n\n"
            f"严格输出 JSON，格式：\n"
            f'{{\"items\": [{{\"query\": \"...\", \"keywords\": [\"kw1\", \"kw2\"]}}]}}'
        )
        result = await call_sentinel(prompt, timeout=20, max_retries=1)
        if not result:
            return []
        items = result.get("items", [])
        out = []
        for item in items[:QUERIES_PER_CARD]:
            q = str(item.get("query", "")).strip()
            kws = item.get("keywords", [])
            if q:
                out.append({"query": q, "keywords": kws})
        return out


async def generate_missing(sample_cards, cache):
    missing = [c for c in sample_cards if c['id'] not in cache]
    if not missing:
        return
    print(f"生成查询中（{len(missing)} 张卡片，每张 {QUERIES_PER_CARD} 条）...")
    sem = asyncio.Semaphore(3)
    tasks = [(c['id'], asyncio.create_task(_gen_one(c, sem))) for c in missing]
    for i, (card_id, task) in enumerate(tasks):
        items = await task
        cache[card_id] = items
        status = f"{len(items)} 条" if items else "失败"
        print(f"  [{i+1}/{len(missing)}] {card_id[:14]}... {status}")


# ── 查询向量化 ────────────────────────────────────────
async def embed_queries(cache):
    need = [(card_id, item)
            for card_id, items in cache.items()
            for item in items
            if "_vec" not in item and item.get("query")]
    if not need:
        return
    print(f"向量化查询（{len(need)} 条）...")
    for i, (_, item) in enumerate(need):
        vec = await get_embedding(item["query"])
        if vec:
            item["_vec"] = vec
        if (i + 1) % 10 == 0:
            print(f"  {i+1}/{len(need)}")


# ── 评估 ──────────────────────────────────────────────
def evaluate(all_cards, sample_cards, cache):
    hits = {k: {"emb": [], "kw": [], "union": []} for k in K_VALUES}
    total = 0
    skipped = 0

    for card in sample_cards:
        card_id = card['id']
        items = cache.get(card_id, [])
        for item in items:
            q_vec = item.get("_vec")
            q_kws = item.get("keywords", [])
            if not q_vec:
                skipped += 1
                continue
            total += 1

            emb_ranked = sorted(all_cards, key=lambda c: cosine(q_vec, c['_vec']), reverse=True)
            kw_ranked = sorted(all_cards, key=lambda c: kw_score(q_kws, c['_kws']), reverse=True)
            emb_ids = [c['id'] for c in emb_ranked]
            kw_ids = [c['id'] for c in kw_ranked]

            for k in K_VALUES:
                emb_set = set(emb_ids[:k])
                kw_set = set(kw_ids[:k])
                hits[k]["emb"].append(1 if card_id in emb_set else 0)
                hits[k]["kw"].append(1 if card_id in kw_set else 0)
                hits[k]["union"].append(1 if card_id in (emb_set | kw_set) else 0)

    return hits, total, skipped


def print_report(hits, total, skipped, n_sample, n_all):
    print(f"\n{'='*66}")
    print(f"  样本: {n_sample} 张卡片 × {QUERIES_PER_CARD} 条查询 = {total} 条有效查询")
    print(f"  全库: {n_all} 张  |  跳过: {skipped} 条（生成/向量化失败）")
    print(f"{'='*66}")
    print(f"  {'k':<5}  {'embedding':>11}  {'keyword':>11}  {'并集':>11}")
    print(f"  {'-'*50}")
    for k in K_VALUES:
        n = len(hits[k]["emb"])
        if n == 0:
            continue
        emb_r = sum(hits[k]["emb"]) / n
        kw_r = sum(hits[k]["kw"]) / n
        union_r = sum(hits[k]["union"]) / n
        marker = "  ← 当前值" if k == CURRENT_K else ""
        print(f"  {k:<5}  {emb_r:>11.3f}  {kw_r:>11.3f}  {union_r:>11.3f}{marker}")
    print(f"{'='*66}\n")


# ── 主流程 ────────────────────────────────────────────
async def main():
    parser = argparse.ArgumentParser(description="记忆库粗筛召回率实验")
    parser.add_argument("--sample", type=int, default=100, help="抽样卡片数（默认 100）")
    parser.add_argument("--seed", type=int, default=42, help="随机种子")
    parser.add_argument("--regen", action="store_true", help="忽略缓存重新生成查询")
    args = parser.parse_args()

    print("加载卡片...")
    all_cards = load_cards()
    print(f"全库: {len(all_cards)} 张有 embedding 的卡片")

    n = min(args.sample, len(all_cards))
    random.seed(args.seed)
    sample_cards = random.sample(all_cards, n)
    print(f"样本: {n} 张（seed={args.seed}）")

    # 加载缓存
    cache = {}
    if CACHE_FILE.exists() and not args.regen:
        cache = json.loads(CACHE_FILE.read_text(encoding='utf-8'))
        already = sum(1 for c in sample_cards if c['id'] in cache)
        print(f"缓存命中: {already}/{n} 张")

    # 生成缺失查询
    await generate_missing(sample_cards, cache)

    # 向量化查询（结果存回 cache，下次可跳过）
    await embed_queries(cache)

    # 保存缓存（含向量）
    CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding='utf-8')

    # 评估
    print("评估中...")
    t0 = time.time()
    hits, total, skipped = evaluate(all_cards, sample_cards, cache)
    print(f"评估完成（{time.time()-t0:.1f}s）")

    print_report(hits, total, skipped, n, len(all_cards))


if __name__ == '__main__':
    asyncio.run(main())
