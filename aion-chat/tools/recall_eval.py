"""
记忆召回质量评估脚本（正式评估集版）

计算 recall@K 的 Precision 和 Recall，按场景（type/period/importance）输出分组报告。
评估集格式见 gen_eval_dataset.py 或 eval_dataset.json。

用法：
  python tools/recall_eval.py                          # 用默认 eval_dataset.json
  python tools/recall_eval.py --dataset path/to/x.json
  python tools/recall_eval.py --k 5                    # 只看 recall@5
  python tools/recall_eval.py --k 5,10                 # 看 @5 和 @10
  python tools/recall_eval.py --threshold 0.0          # 不过 threshold 截断（纯排序）
  python tools/recall_eval.py --no-vitality            # 关闭 vitality 加权

依赖：
  - eval_dataset.json（由 gen_eval_dataset.py 生成，或人工标注）
  - aion-chat/data/chat.db（memory_cards 表，需有 embedding 字段）
  - 运行目录或 sys.path 中有 config.py（提供 DB_PATH）

注意：此脚本不调用任何外部 API，完全离线运行。
     vitality 计算依赖 decay_engine.py（本地纯计算，无网络）。
"""

import argparse
import json
import math
import sqlite3
import struct
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.stdout.reconfigure(encoding='utf-8')

from config import DB_PATH

DEFAULT_DATASET = Path(__file__).parent / "eval_dataset.json"

# ── 向量工具 ───────────────────────────────────────────
def unpack_vec(blob: bytes) -> list[float]:
    n = len(blob) // 4
    return list(struct.unpack(f'{n}f', blob))


def cosine_sim(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return dot / (na * nb) if na and nb else 0.0


# ── 关键词匹配（同 memory.py 逻辑）────────────────────
def keyword_match_score(query_keywords: list[str], mem_kws: list[str]) -> float:
    if not query_keywords or not mem_kws:
        return 0.0
    mem_lower = [k.lower() for k in mem_kws]
    hits = sum(
        1 for qk in query_keywords
        if any(qk.lower() in mk or mk in qk.lower() for mk in mem_lower)
    )
    return hits / len(query_keywords)


# ── vitality 计算（同 decay_engine.py）────────────────
def compute_vitality(importance, activation_count, last_activated,
                     valence, arousal, decay_lambda=0.05, now=None):
    if now is None:
        now = time.time()
    days = max(0.0, (now - (last_activated or now)) / 86400.0)
    act = max(1.0, float(activation_count or 1))
    imp = max(0.01, float(importance or 0.3))
    ew = 1.0 + abs(valence or 0.0) * 0.3 + max(0.0, arousal or 0.0) * 0.2
    return imp * (act ** 0.3) * math.exp(-decay_lambda * days) * ew


# ── 数据加载 ───────────────────────────────────────────
def load_cards() -> list[dict]:
    """加载所有 open/closed 且有 embedding 的卡片"""
    db = sqlite3.connect(str(DB_PATH))
    db.row_factory = sqlite3.Row
    rows = db.execute(
        "SELECT id, content, type, created_at, keywords, importance, "
        "activation_count, last_activated, valence, arousal, unresolved, "
        "embedding "
        "FROM memory_cards "
        "WHERE status IN ('open', 'closed') AND embedding IS NOT NULL"
    ).fetchall()
    db.close()

    cards = []
    for r in rows:
        c = dict(r)
        try:
            c['_kws'] = json.loads(c['keywords']) if c['keywords'] else []
        except Exception:
            c['_kws'] = []
        c['_vec'] = unpack_vec(c['embedding']) if c['embedding'] else None
        del c['embedding']
        cards.append(c)
    return cards


def load_dataset(path: Path) -> list[dict]:
    if not path.exists():
        print(f"ERROR: 评估集不存在: {path}")
        print("请先运行 python tools/gen_eval_dataset.py 生成评估集。")
        sys.exit(1)
    data = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(data, list) or not data:
        print(f"ERROR: 评估集格式错误或为空: {path}")
        sys.exit(1)
    return data


# ── 召回逻辑（离线版，镜像 memory.py recall_memories）──
def score_cards(query_keywords: list[str], query_vec: list[float] | None,
                cards: list[dict], use_vitality: bool, now: float,
                no_vec: bool = False) -> list[dict]:
    """
    base_score = kw×0.5 + vec×0.3 + importance×0.2  (默认)
    no_vec 时  = kw×0.7 + importance×0.3
    final = base × vitality   (use_vitality=True 时)
    """
    scored = []
    for c in cards:
        if not c['_vec'] and query_vec is None:
            continue
        vec_sim = 0.0 if no_vec else (cosine_sim(query_vec, c['_vec']) if (query_vec and c['_vec']) else 0.0)
        kw_s = keyword_match_score(query_keywords, c['_kws'])
        importance = float(c['importance'] or 0.5)
        if no_vec:
            base = kw_s * 0.7 + importance * 0.3
        else:
            base = kw_s * 0.5 + vec_sim * 0.3 + importance * 0.2

        if use_vitality:
            vitality = compute_vitality(
                importance=importance,
                activation_count=c.get('activation_count', 0),
                last_activated=c.get('last_activated') or c['created_at'],
                valence=c.get('valence', 0.0),
                arousal=c.get('arousal', 0.0),
                now=now,
            )
            final = base * vitality
        else:
            final = base

        scored.append({
            'id': c['id'],
            'score': final,
            'vec_sim': vec_sim,
            'kw_score': kw_s,
            'importance': importance,
        })

    scored.sort(key=lambda x: x['score'], reverse=True)
    return scored


# ── 评估一条样本 ────────────────────────────────────────
def eval_one(sample: dict, cards: list[dict],
             k_values: list[int], use_vitality: bool, threshold: float, now: float,
             use_reranker: bool = False, no_vec: bool = False,
             rerank_candidates: int = 20) -> dict:
    """
    返回 {k: {'precision': float, 'recall': float, 'hit': bool}} 及调试信息
    """
    query = sample['query']
    query_keywords = sample.get('query_keywords', [])
    expected_ids = set(sample['expected_card_ids'])

    query_vec = sample.get('_query_vec')

    scored = score_cards(query_keywords, query_vec, cards, use_vitality, now, no_vec=no_vec)

    if use_reranker:
        max_k = max(k_values)
        scored = _rerank_topk(query, scored, cards,
                              top_n_candidates=rerank_candidates, top_k=max_k)

    result = {}
    for k in k_values:
        top_k = scored[:k]
        if threshold > 0.0:
            top_k = [r for r in top_k if r['score'] >= threshold]

        retrieved_ids = {r['id'] for r in top_k}
        tp = len(retrieved_ids & expected_ids)

        precision = tp / len(top_k) if top_k else 0.0
        recall = tp / len(expected_ids) if expected_ids else 0.0
        hit = tp > 0  # 至少命中一个

        result[k] = {
            'precision': precision,
            'recall': recall,
            'hit': hit,
            'retrieved': len(top_k),
            'expected': len(expected_ids),
            'tp': tp,
        }

    # 附上调试：主目标卡片的 rank
    primary_id = sample.get('source_card_id') or (sample['expected_card_ids'][0] if sample['expected_card_ids'] else None)
    rank_of_primary = None
    if primary_id:
        for i, r in enumerate(scored):
            if r['id'] == primary_id:
                rank_of_primary = i + 1
                break

    return {
        'k_metrics': result,
        'rank_of_primary': rank_of_primary,
        'top3_scores': [(r['id'][:16], round(r['score'], 4)) for r in scored[:3]],
    }


# ── 聚合报告 ────────────────────────────────────────────
def aggregate_metrics(all_results: list[dict], k_values: list[int]) -> dict:
    """计算整体均值"""
    agg = {k: {'precision': [], 'recall': [], 'hit': []} for k in k_values}
    ranks = []

    for res in all_results:
        for k in k_values:
            m = res['k_metrics'].get(k, {})
            if m:
                agg[k]['precision'].append(m['precision'])
                agg[k]['recall'].append(m['recall'])
                agg[k]['hit'].append(1 if m['hit'] else 0)
        if res.get('rank_of_primary') is not None:
            ranks.append(res['rank_of_primary'])

    out = {}
    for k in k_values:
        n = len(agg[k]['precision'])
        if n == 0:
            continue
        out[k] = {
            'precision': sum(agg[k]['precision']) / n,
            'recall': sum(agg[k]['recall']) / n,
            'hit_rate': sum(agg[k]['hit']) / n,
            'n': n,
        }

    mrr = sum(1.0 / r for r in ranks) / len(ranks) if ranks else 0.0
    out['mrr'] = mrr
    out['median_rank'] = sorted(ranks)[len(ranks) // 2] if ranks else None

    return out


def tag_group_metrics(samples: list[dict], all_results: list[dict],
                      k_values: list[int]) -> dict:
    """按 tag 分组计算指标"""
    groups = defaultdict(list)
    for sample, res in zip(samples, all_results):
        for tag in sample.get('tags', []):
            groups[tag].append(res)

    group_out = {}
    for tag, results in sorted(groups.items()):
        group_out[tag] = aggregate_metrics(results, k_values)

    return group_out


# ── 打印报告 ────────────────────────────────────────────
def print_report(overall: dict, by_tag: dict, k_values: list[int],
                 n_samples: int, n_cards: int, use_vitality: bool, threshold: float):
    line = "=" * 68
    print(f"\n{line}")
    print(f"  记忆召回评估报告")
    print(f"  评估集: {n_samples} 条  |  卡片库: {n_cards} 张")
    print(f"  vitality: {'开' if use_vitality else '关'}  |  threshold: {threshold}")
    print(line)

    # 整体指标表
    header = f"  {'@K':<6}  {'Precision':>10}  {'Recall':>10}  {'Hit Rate':>10}  {'n':>5}"
    print(f"\n  [整体指标]")
    print(header)
    print(f"  {'-'*54}")
    for k in k_values:
        m = overall.get(k)
        if not m:
            continue
        print(f"  {'@'+str(k):<6}  {m['precision']:>10.3f}  {m['recall']:>10.3f}  {m['hit_rate']:>10.3f}  {m['n']:>5}")
    if overall.get('mrr') is not None:
        print(f"\n  MRR (primary card): {overall['mrr']:.3f}")
    if overall.get('median_rank') is not None:
        print(f"  Median rank of primary card: {overall['median_rank']}")

    # 按 type 分组
    type_tags = {tag: m for tag, m in by_tag.items() if tag.startswith('type:')}
    if type_tags:
        k_main = k_values[0]
        print(f"\n  [按 type 分组，@{k_main}]")
        print(f"  {'type':<16}  {'Precision':>10}  {'Recall':>10}  {'Hit Rate':>10}  {'n':>5}")
        print(f"  {'-'*62}")
        for tag in sorted(type_tags):
            m = type_tags[tag].get(k_main)
            if not m:
                continue
            label = tag.replace('type:', '')
            print(f"  {label:<16}  {m['precision']:>10.3f}  {m['recall']:>10.3f}  {m['hit_rate']:>10.3f}  {m['n']:>5}")

    # 按 importance 分组
    imp_tags = {tag: m for tag, m in by_tag.items() if tag.startswith('importance:')}
    if imp_tags:
        k_main = k_values[0]
        print(f"\n  [按 importance 分组，@{k_main}]")
        print(f"  {'importance':<16}  {'Precision':>10}  {'Recall':>10}  {'Hit Rate':>10}  {'n':>5}")
        print(f"  {'-'*62}")
        for tag in sorted(imp_tags):
            m = imp_tags[tag].get(k_main)
            if not m:
                continue
            label = tag
            print(f"  {label:<16}  {m['precision']:>10.3f}  {m['recall']:>10.3f}  {m['hit_rate']:>10.3f}  {m['n']:>5}")

    print(f"\n{line}\n")


def save_report(overall: dict, by_tag: dict, samples: list[dict],
                all_results: list[dict], k_values: list[int],
                out_path: Path, cards: list[dict] = None):
    card_kw_map = {}
    if cards:
        card_kw_map = {c['id']: c['_kws'] for c in cards}

    per_sample = []
    for s, r in zip(samples, all_results):
        k_main = k_values[0] if k_values else 5
        is_miss = not r['k_metrics'].get(k_main, {}).get('hit', False)

        entry = {
            'id': s['id'],
            'query': s['query'],
            'query_keywords': s.get('query_keywords', []),
            'tags': s.get('tags', []),
            'source_card_id': s.get('source_card_id'),
            'rank_of_primary': r['rank_of_primary'],
            'metrics': {str(k): r['k_metrics'][k] for k in k_values if k in r['k_metrics']},
            'top3': r['top3_scores'],
        }

        if is_miss and card_kw_map:
            q_kws = set(kw.lower() for kw in s.get('query_keywords', []))
            for eid in s['expected_card_ids']:
                card_kws = set(kw.lower() for kw in card_kw_map.get(eid, []))
                overlap = q_kws & card_kws
                fuzzy = [qk for qk in q_kws if any(qk in ck or ck in qk for ck in card_kws)] if not overlap else []
                entry.setdefault('kw_diagnosis', []).append({
                    'card_id': eid,
                    'card_keywords': card_kw_map.get(eid, []),
                    'exact_overlap': list(overlap),
                    'fuzzy_overlap': fuzzy,
                })

        per_sample.append(entry)

    report = {
        'generated_at': time.strftime('%Y-%m-%d %H:%M:%S'),
        'overall': overall,
        'by_tag': by_tag,
        'per_sample': per_sample,
    }
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f"详细报告已写入: {out_path}")


# ── 可选：reranker 精排 ────────────────────────────────
def _rerank_topk(query: str, scored: list[dict], cards: list[dict],
                 top_n_candidates: int = 20, top_k: int = 5) -> list[dict]:
    """
    取 scored 前 top_n_candidates 条，调 reranker 精排，返回重排后的列表。
    同步 wrapper，内部用 asyncio.run。
    """
    import asyncio

    candidates = scored[:top_n_candidates]
    if not candidates:
        return scored

    card_map = {c['id']: c for c in cards}
    docs = []
    cand_ids = []
    for c in candidates:
        card = card_map.get(c['id'])
        if card:
            docs.append(card['content'][:512])
            cand_ids.append(c['id'])

    if not docs:
        return scored

    async def _run():
        from sentinel import fetch_rerank
        return await fetch_rerank(query, docs, top_n=min(len(docs), top_k * 2))

    rr_results = asyncio.run(_run())
    if not rr_results:
        return scored

    reranked = []
    for rr in rr_results:
        idx = rr['index']
        if idx < len(cand_ids):
            orig = next((c for c in candidates if c['id'] == cand_ids[idx]), None)
            if orig:
                entry = dict(orig)
                entry['rerank_score'] = rr['relevance_score']
                reranked.append(entry)

    remaining = [c for c in scored if c['id'] not in {r['id'] for r in reranked}]
    return reranked + remaining


# ── 可选：调 API 为查询向量化 ────────────────────────────
def _embed_queries_async(samples: list[dict]):
    """
    为 eval_dataset 里缺少 _query_vec 的样本调 embedding API 填充向量。
    修改 samples in-place。需要 API key（从 config/sentinel 模块读取）。
    """
    import asyncio

    async def _run():
        from sentinel import get_embedding
        need = [s for s in samples if '_query_vec' not in s]
        print(f"向量化查询（{len(need)} 条，调 embedding API）...")
        for i, s in enumerate(need):
            vec = await get_embedding(s['query'])
            if vec:
                s['_query_vec'] = vec
            if (i + 1) % 10 == 0 or (i + 1) == len(need):
                print(f"  {i+1}/{len(need)}")

    asyncio.run(_run())


# ── 主流程 ─────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="记忆召回质量评估（eval_dataset.json → precision/recall@K）")
    parser.add_argument('--dataset', type=str, default=str(DEFAULT_DATASET),
                        help='评估集 JSON 路径（默认 eval_dataset.json）')
    parser.add_argument('--k', type=str, default='5,10',
                        help='逗号分隔的 K 值（默认 5,10）')
    parser.add_argument('--threshold', type=float, default=0.0,
                        help='召回分数 threshold，0 = 不截断，只看排序（默认 0.0）')
    parser.add_argument('--no-vitality', action='store_true',
                        help='关闭 vitality 加权（只用 kw×0.5+vec×0.3+imp×0.2）')
    parser.add_argument('--output', type=str, default='',
                        help='将详细报告写入 JSON 文件（可选）')
    parser.add_argument('--verbose', action='store_true',
                        help='打印每条样本的命中情况')
    parser.add_argument('--embed-queries', action='store_true',
                        help='调用 embedding API 为查询向量化（需要 API key，更准确）')
    parser.add_argument('--reranker', action='store_true',
                        help='启用 reranker 精排（粗筛 top-N → reranker 重排）')
    parser.add_argument('--rerank-candidates', type=int, default=20,
                        help='reranker 粗筛候选数（默认 20）')
    parser.add_argument('--no-vec', action='store_true',
                        help='去掉向量分量，公式改为 kw×0.7 + imp×0.3')
    args = parser.parse_args()

    k_values = [int(x.strip()) for x in args.k.split(',') if x.strip()]
    use_vitality = not args.no_vitality
    threshold = args.threshold
    use_reranker = args.reranker
    no_vec = args.no_vec

    dataset_path = Path(args.dataset)
    print(f"加载评估集: {dataset_path}")
    samples = load_dataset(dataset_path)

    # 可选：调 API 为查询向量化，让 vec 分量也参与评估
    if args.embed_queries:
        _embed_queries_async(samples)
    print(f"  {len(samples)} 条评估样本")

    print("加载卡片库...")
    cards = load_cards()
    print(f"  {len(cards)} 张卡片（open/closed，有 embedding）")

    if not cards:
        print("ERROR: 卡片库为空，无法评估。")
        sys.exit(1)

    # 过滤评估集中 expected_card_ids 里的卡片是否在库中
    card_ids_in_db = {c['id'] for c in cards}
    valid_samples = []
    skipped = 0
    for s in samples:
        exp = [eid for eid in s['expected_card_ids'] if eid in card_ids_in_db]
        if not exp:
            skipped += 1
            continue
        s = dict(s)
        s['expected_card_ids'] = exp
        valid_samples.append(s)
    if skipped:
        print(f"  跳过 {skipped} 条（expected 卡片不在当前库中，可能已归档）")
    print(f"  有效评估样本: {len(valid_samples)} 条")

    if not valid_samples:
        print("ERROR: 没有有效的评估样本。请重新运行 gen_eval_dataset.py。")
        sys.exit(1)

    mode_parts = []
    if no_vec:
        mode_parts.append("no-vec(kw×0.7+imp×0.3)")
    else:
        mode_parts.append("kw×0.5+vec×0.3+imp×0.2")
    if use_reranker:
        mode_parts.append(f"reranker(top-{args.rerank_candidates})")
    print(f"\n评估配置: K={k_values}, vitality={'on' if use_vitality else 'off'}, "
          f"threshold={threshold}, mode={'+'.join(mode_parts)}")
    if use_reranker:
        print(f"评估中（含 reranker API 调用，每条样本 ~500-700ms）...")
    else:
        print("评估中（离线关键词+向量匹配，无 API 调用）...")

    now = time.time()
    _t_start = time.time()
    all_results = []
    for i, sample in enumerate(valid_samples):
        res = eval_one(sample, cards, k_values, use_vitality, threshold, now,
                       use_reranker=use_reranker, no_vec=no_vec,
                       rerank_candidates=args.rerank_candidates)
        all_results.append(res)

        if args.verbose:
            k_main = k_values[0]
            m = res['k_metrics'].get(k_main, {})
            rank = res.get('rank_of_primary')
            hit_mark = "HIT" if m.get('hit') else "MISS"
            print(f"  [{i+1:3d}/{len(valid_samples)}] {sample['id']} {hit_mark} "
                  f"@{k_main}: p={m.get('precision',0):.2f} r={m.get('recall',0):.2f} "
                  f"rank={rank}  query={sample['query'][:40]}")

    overall = aggregate_metrics(all_results, k_values)
    by_tag = tag_group_metrics(valid_samples, all_results, k_values)

    _elapsed_total = time.time() - _t_start
    print_report(overall, by_tag, k_values, len(valid_samples), len(cards), use_vitality, threshold)
    print(f"  总耗时: {_elapsed_total:.1f}s ({_elapsed_total/len(valid_samples)*1000:.0f}ms/sample)")

    if args.output:
        save_report(overall, by_tag, valid_samples, all_results, k_values, Path(args.output), cards=cards)


if __name__ == '__main__':
    main()
