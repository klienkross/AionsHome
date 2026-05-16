"""
四路召回对比：纯向量 / 纯关键词 / 线上复合公式 / reranker
"""
import asyncio, json, math, struct, sys, time
from pathlib import Path
import httpx

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.stdout.reconfigure(encoding='utf-8')
from config import DB_PATH, get_key

SF_BASE = "https://api.siliconflow.cn/v1"
EMBED_MODEL = "BAAI/bge-m3"
RERANK_MODEL = "BAAI/bge-reranker-v2-m3"

# ── 加载卡片 ──────────────────────────
def load_cards(limit: int = 200):
    import sqlite3
    db = sqlite3.connect(str(DB_PATH))
    db.row_factory = sqlite3.Row
    rows = db.execute(
        "SELECT id, content, type, keywords, importance, created_at, embedding "
        "FROM memory_cards WHERE type != 'aggregate' AND length(content) > 10 "
        f"AND embedding IS NOT NULL ORDER BY created_at DESC LIMIT {limit}"
    ).fetchall()
    db.close()
    cards = []
    for r in rows:
        c = dict(r)
        c['_kws'] = json.loads(c['keywords']) if c['keywords'] else []
        c['_vec'] = list(struct.unpack(f'{len(c["embedding"])//4}f', c['embedding']))
        c['_importance'] = float(c['importance'] or 0.5)
        cards.append(c)
    return cards

def cosine(a, b):
    dot = sum(x*y for x,y in zip(a,b))
    na = math.sqrt(sum(x*x for x in a))
    nb = math.sqrt(sum(x*x for x in b))
    return dot/(na*nb) if na and nb else 0.0

def kw_score(query_keywords: list[str], card_kws: list[str]) -> float:
    """和 memory.py _keyword_match_score 一致: 命中率"""
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

# ── Reranker API ──────────────────────
async def rerank(query: str, docs: list[str], key: str, top_n: int = 10) -> list[dict]:
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    body = {"model": RERANK_MODEL, "query": query, "documents": docs, "top_n": top_n}
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(f"{SF_BASE}/rerank", headers=headers, json=body)
        resp.raise_for_status()
        return resp.json()["results"]

# ── 打印 top-N ────────────────────────
def show_top5(label: str, items: list[tuple], time_ms: float, fmt: str):
    print(f"\n  [{label}] (耗时 {time_ms:.0f}ms)")
    for i, item in enumerate(items[:5]):
        c, score = item[0], item[1]
        kws = ', '.join(c['_kws'][:3]) if c['_kws'] else '无'
        txt = c['content'][:55].replace('\n', ' ')
        print(f"    {i+1}. [{fmt}={score:.4f}] [{c['type']}] {txt}")
        print(f"       kw=[{kws}]")

# ── 主流程 ────────────────────────────
async def main():
    key = get_key("siliconflow")
    if not key:
        print("[错误] 无 siliconflow_key")
        sys.exit(1)

    queries = [
        ("狗 三轮车 日常", ["狗", "三轮车", "日常"]),
        ("钱包 bug 前端", ["钱包", "bug", "前端"]),
        ("吃药 健康 习惯", ["吃药", "健康", "习惯"]),
        ("压力 情绪 愧疚", ["压力", "情绪", "愧疚"]),
    ]

    print("加载卡片...")
    cards = load_cards(200)
    print(f"共 {len(cards)} 张卡片\n")

    for q_text, q_kws in queries:
        print(f"{'='*64}")
        print(f"  Query: {q_text}")
        print(f"  Keywords: {q_kws}")
        print(f"{'='*64}")

        # 1. 获取 query embedding
        t0 = time.time()
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{SF_BASE}/embeddings",
                headers=headers,
                json={"model": EMBED_MODEL, "input": q_text, "encoding_format": "float"},
            )
            q_vec = resp.json()["data"][0]["embedding"]
        embed_api_time = (time.time() - t0) * 1000

        # 2. 全量打分
        all_scored = []
        for c in cards:
            vec_sim = cosine(q_vec, c['_vec'])
            kw_s = kw_score(q_kws, c['_kws'])
            imp = c['_importance']
            composite = kw_s * 0.5 + vec_sim * 0.3 + imp * 0.2
            all_scored.append((c, vec_sim, kw_s, imp, composite))

        # 纯向量 top-5
        by_vec = sorted(all_scored, key=lambda x: x[1], reverse=True)
        show_top5("纯向量 (embedding)", [(c, s1) for c, s1, _, _, _ in by_vec[:5]], embed_api_time, "vec")

        # 纯关键词 top-5
        by_kw = sorted(all_scored, key=lambda x: x[2], reverse=True)
        show_top5("纯关键词 (keyword)", [(c, s2) for c, _, s2, _, _ in by_kw[:5]], 0, "kw")

        # 线上复合公式 top-5
        by_comp = sorted(all_scored, key=lambda x: x[4], reverse=True)
        show_top5("线上公式 (kw*0.5+vec*0.3+imp*0.2)", [(c, s4) for c, _, _, _, s4 in by_comp[:5]], 0, "comp")

        # 4. reranker 精排
        # 先用 embedding 粗筛 top-20 喂给 reranker
        top20 = [(c, s1) for c, s1, _, _, _ in by_vec[:20]]
        t0 = time.time()
        docs = [c['content'][:512] for c, _ in top20]
        rerank_results = await rerank(q_text, docs, key, top_n=10)
        rerank_time = (time.time() - t0) * 1000

        rerank_items = []
        for r in rerank_results[:5]:
            idx = r['index']
            c, emb_sim = top20[idx]
            rerank_items.append((c, r['relevance_score']))
        show_top5("reranker (BGE-reranker-v2-m3)", rerank_items, rerank_time, "rerank")

        # ── 汇总对比 ──────────────────────
        print(f"\n  {'─'*58}")
        print(f"  top-5 id 集合对比:")
        vec_ids = {by_vec[i][0]['id'] for i in range(5)}
        kw_ids = {by_kw[i][0]['id'] for i in range(5)}
        comp_ids = {by_comp[i][0]['id'] for i in range(5)}
        rerank_ids = {item[0]['id'] for item in rerank_items}

        # 两两重叠
        def overlap(a, b, name_a, name_b):
            o = len(a & b)
            print(f"    {name_a:8s} ∩ {name_b:20s} = {o}/5")
        overlap(vec_ids, kw_ids, "vec", "kw")
        overlap(comp_ids, kw_ids, "comp", "kw")
        overlap(comp_ids, vec_ids, "comp", "vec")
        overlap(rerank_ids, vec_ids, "reranker", "vec")
        overlap(rerank_ids, kw_ids, "reranker", "kw")
        overlap(rerank_ids, comp_ids, "reranker", "comp")
        print()

    print("测试完成。")


if __name__ == '__main__':
    asyncio.run(main())
