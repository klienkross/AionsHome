"""
直接测试 RECALL + MEM_EDIT 全链路，不经过 bot。
用法:
  python tools/test_mem_edit.py recall 关键词1 关键词2
  python tools/test_mem_edit.py edit card_xxx summary 新的总结内容
  python tools/test_mem_edit.py edit card_xxx keywords kw1,kw2,kw3
  python tools/test_mem_edit.py edit card_xxx close
  python tools/test_mem_edit.py edit card_xxx delete
  python tools/test_mem_edit.py edit card_a,card_b merge 合并后的总结
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.stdout.reconfigure(encoding='utf-8')


async def do_recall(keywords: list[str]):
    from active_recall import search_memory
    print(f"搜索关键词: {keywords}")
    results = await search_memory(keywords)
    if not results:
        print("（没有找到匹配的卡片）")
        return
    print(f"找到 {len(results)} 张卡片:\n")
    for r in results:
        card_id = r.get("id", "?")
        typ = r.get("type", "")
        status = r.get("status", "")
        content = r.get("content", "")[:80].replace("\n", " ")
        kws = r.get("keywords", "")
        print(f"  [{card_id}] [{typ}/{status}]")
        print(f"    {content}")
        print(f"    关键词: {kws}")
        print()


async def do_edit(raw: str):
    from active_recall import execute_mem_edit
    print(f"执行: MEM_EDIT:{raw}")
    result = await execute_mem_edit(raw)
    print(f"结果: {result}")


async def do_show(card_id: str):
    from memory_cards import get_card, get_all_links
    card = await get_card(card_id)
    if not card:
        print(f"卡片不存在: {card_id}")
        return
    print(f"ID:       {card['id']}")
    print(f"类型:     {card['type']}")
    print(f"状态:     {card['status']}")
    print(f"内容:     {card['content'][:120]}")
    print(f"关键词:   {card.get('keywords', '')}")
    print(f"重要度:   {card.get('importance', '')}")
    links = await get_all_links(card_id)
    if links:
        print(f"链接({len(links)}):")
        for l in links:
            print(f"  {l['from_id']} --{l['relation']}--> {l['to_id']}")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return

    cmd = sys.argv[1]

    if cmd == "recall":
        if len(sys.argv) < 3:
            print("用法: python tools/test_mem_edit.py recall 关键词1 关键词2")
            return
        asyncio.run(do_recall(sys.argv[2:]))

    elif cmd == "edit":
        if len(sys.argv) < 4:
            print("用法: python tools/test_mem_edit.py edit card_id action [content]")
            return
        card_ids = sys.argv[2]
        action = sys.argv[3]
        content = " ".join(sys.argv[4:]) if len(sys.argv) > 4 else ""
        raw = f"{card_ids}|{action}|{content}" if content else f"{card_ids}|{action}"
        asyncio.run(do_edit(raw))

    elif cmd == "show":
        if len(sys.argv) < 3:
            print("用法: python tools/test_mem_edit.py show card_id")
            return
        asyncio.run(do_show(sys.argv[2]))

    else:
        print(f"未知命令: {cmd}")
        print(__doc__)


if __name__ == "__main__":
    main()
