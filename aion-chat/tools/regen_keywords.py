"""
批量重新生成卡片关键词（分层：领域词+实体词），只改 keywords 字段。

用法:
  python tools/regen_keywords.py                # 全量重跑
  python tools/regen_keywords.py --dry-run      # 只打印不写库
  python tools/regen_keywords.py --batch-size 5 # 每批5张（默认10）
  python tools/regen_keywords.py --resume 120   # 从第120张开始（断点续跑）
"""

import asyncio
import json
import sys
import time
import sqlite3
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.stdout.reconfigure(encoding='utf-8')

from config import DB_PATH
from sentinel import call_sentinel_text

BATCH_SIZE = 10

PROMPT_TEMPLATE = """请为以下记忆卡片重新生成关键词。每张卡片生成 3-6 个关键词，分两层：
· 领域词（1-2个）：这件事属于什么大类。例：阅读、技术开发、日常起居、社交、情绪、创作、游戏、医疗、饮食、学业
· 实体词（2-4个）：具体的人事物地名。例：中亚史、阿里云、提拉米苏、披风鸟人

规则：
- 领域词放前面，实体词放后面
- 每个关键词必须是数组中独立的字符串
- 【严禁】人名（K, bot 等）
- 【严禁】泛指词：提醒、建议、完成、计划、测试、观察、休息、未完成、担忧、偏好、问候、早上、回顾、记忆、询问、解释、讨论

示例：
  "阅读中亚史时对战车提出疑问" → ["阅读", "中亚史", "战车", "骑兵"]
  "提醒喝咖啡不要太快" → ["日常起居", "喝咖啡", "胃"]
  "K打死了一只蚊子" → ["日常起居", "蚊子"]
  "K正在写统计作业" → ["学业", "统计"]

卡片列表（id: content）：
{cards_text}

输出一个 JSON 数组，每个元素：{{"id": "card_xxx", "keywords": ["领域词", "实体词1", "实体词2"]}}
顺序与输入一致。严格只输出 JSON 数组。"""


def load_all_cards():
    db = sqlite3.connect(str(DB_PATH))
    db.row_factory = sqlite3.Row
    rows = db.execute(
        "SELECT id, content, type, keywords FROM memory_cards "
        "WHERE type != 'aggregate' ORDER BY created_at ASC"
    ).fetchall()
    db.close()
    return [dict(r) for r in rows]


async def regen_batch(cards: list[dict]) -> list[dict]:
    cards_text = "\n".join(
        f"{c['id']}: [{c['type']}] {c['content'][:120]}"
        for c in cards
    )
    prompt = PROMPT_TEMPLATE.format(cards_text=cards_text)
    raw = await call_sentinel_text(prompt, timeout=60)
    if not raw:
        return []

    raw = raw.strip()
    if "```" in raw:
        s = raw.find("[")
        e = raw.rfind("]") + 1
        if s >= 0 and e > s:
            raw = raw[s:e]
    elif not raw.startswith("["):
        s = raw.find("[")
        e = raw.rfind("]") + 1
        if s >= 0 and e > s:
            raw = raw[s:e]

    try:
        results = json.loads(raw)
        if isinstance(results, list):
            return results
    except Exception as e:
        print(f"  [!] JSON解析失败: {e}")
        print(f"  [!] 原始输出: {raw[:200]}")
    return []


async def update_keywords(card_id: str, keywords: list[str]):
    import aiosqlite
    kw_json = json.dumps(keywords, ensure_ascii=False)
    async with aiosqlite.connect(str(DB_PATH)) as db:
        await db.execute(
            "UPDATE memory_cards SET keywords=?, updated_at=? WHERE id=?",
            (kw_json, time.time(), card_id),
        )
        await db.commit()


async def main():
    dry_run = "--dry-run" in sys.argv
    batch_size = BATCH_SIZE
    resume_from = 0

    for i, arg in enumerate(sys.argv):
        if arg == "--batch-size" and i + 1 < len(sys.argv):
            batch_size = int(sys.argv[i + 1])
        if arg == "--resume" and i + 1 < len(sys.argv):
            resume_from = int(sys.argv[i + 1])

    print(f"加载卡片...")
    cards = load_all_cards()
    print(f"共 {len(cards)} 张卡片，batch_size={batch_size}，resume_from={resume_from}")
    if dry_run:
        print("*** DRY RUN 模式 ***")

    cards = cards[resume_from:]
    total = len(cards)
    updated = 0
    failed = 0

    for batch_start in range(0, total, batch_size):
        batch = cards[batch_start:batch_start + batch_size]
        batch_idx = resume_from + batch_start
        print(f"\n[{batch_idx}-{batch_idx+len(batch)-1}] 处理 {len(batch)} 张...")

        results = await regen_batch(batch)

        if not results:
            print(f"  [!] 批次失败，跳过")
            failed += len(batch)
            continue

        id_to_result = {r["id"]: r["keywords"] for r in results if "id" in r and "keywords" in r}

        for c in batch:
            if c["id"] in id_to_result:
                new_kws = id_to_result[c["id"]]
                old_kws = c["keywords"] or "[]"
                if dry_run:
                    print(f"  {c['id']}: {old_kws} → {json.dumps(new_kws, ensure_ascii=False)}")
                else:
                    await update_keywords(c["id"], new_kws)
                    print(f"  ✓ {c['id']}: {json.dumps(new_kws, ensure_ascii=False)}")
                updated += 1
            else:
                print(f"  [!] {c['id']} 未在结果中找到")
                failed += 1

    print(f"\n完成！更新: {updated}，失败: {failed}")


if __name__ == "__main__":
    asyncio.run(main())
