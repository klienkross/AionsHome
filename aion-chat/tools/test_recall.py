import asyncio, sys, time
sys.path.insert(0, '.')
sys.stdout.reconfigure(encoding='utf-8')
from memory import recall_memories

async def test():
    queries = [
        ("狗 三轮车 日常", ["狗", "三轮车", "日常"]),
        ("钱包 bug 前端", ["钱包", "bug", "前端"]),
        ("吃药 健康 习惯", ["吃药", "健康", "习惯"]),
    ]
    for q, kws in queries:
        print(f"\n{'='*60}")
        print(f"Query: {q}")
        matched, debug = await recall_memories(q, query_keywords=kws)
        print(f"Matched: {len(matched)}, Debug top-6:")
        for i, m in enumerate(debug):
            print(f"  {i+1}. [{m['score']:.4f}] kw={m['kw_score']:.4f} vec={m['vec_sim']:.4f} vit={m['vitality']:.4f}")
            print(f"     [{m['type']}] {m['content'][:60]}")

asyncio.run(test())
