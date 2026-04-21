"""记忆切片 dry-run 预览：读全量消息，跑 _split_into_groups_smart，
打印每组的时间范围/条数/首尾消息片段，可选地调一次摘要 prompt。
零写入：不动 memories 表，不动 digest_anchor。

用法：
    python dryrun_grouping.py              # 只看分组
    python dryrun_grouping.py --summary    # 顺带跑摘要 prompt（会调 flash-lite）
    python dryrun_grouping.py --out preview.md  # 写到 markdown 文件
"""

import argparse
import asyncio
import sys
from datetime import datetime

import aiosqlite

from config import DB_PATH, load_worldbook
from memory import _split_into_groups_smart, _call_flash_lite


async def fetch_all_messages() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT id, conv_id, role, content, created_at FROM messages "
            "WHERE role IN ('user','assistant') ORDER BY created_at ASC"
        )
        rows = await cur.fetchall()
    return [dict(r) for r in rows]


def fmt_ts(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def render_group(idx: int, group: list[dict], user_name: str, ai_name: str) -> str:
    start = fmt_ts(group[0]["created_at"])
    end = fmt_ts(group[-1]["created_at"])
    dur_min = (group[-1]["created_at"] - group[0]["created_at"]) / 60
    lines = [
        f"## Group {idx}  [{len(group)} 条 · {dur_min:.0f}min]",
        f"- 时间: {start} → {end}",
        "",
        "**首条**:",
        f"> {group[0]['role']}: {group[0]['content'][:120].replace(chr(10),' ')}",
        "",
        "**末条**:",
        f"> {group[-1]['role']}: {group[-1]['content'][:120].replace(chr(10),' ')}",
        "",
    ]
    return "\n".join(lines)


async def make_summary(group: list[dict], user_name: str, ai_name: str) -> str:
    group_start = datetime.fromtimestamp(group[0]["created_at"]).strftime("%Y年%m月%d日 %H:%M")
    group_end = datetime.fromtimestamp(group[-1]["created_at"]).strftime("%Y年%m月%d日 %H:%M")
    date_header = f"[对话时间范围: {group_start} ~ {group_end}]\n"
    body = date_header + "\n".join([
        f"[{datetime.fromtimestamp(m['created_at']).strftime('%m-%d %H:%M')}] "
        f"{user_name if m['role']=='user' else ai_name}: {m['content'][:300]}"
        for m in group
    ])
    prompt = (
        f"用精简语言总结对话，使用 \"{user_name}\" 和 \"{ai_name}\" 指代双方。"
        f"输出 JSON: {{\"summary\": \"...\"}}（100 字内）。\n\n【对话】\n{body}"
    )
    result = await _call_flash_lite(prompt)
    if not result:
        return "(摘要调用失败)"
    return str(result.get("summary", "")).strip()


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--summary", action="store_true", help="也跑摘要 prompt")
    parser.add_argument("--out", default="dryrun_preview.md", help="输出到 markdown 文件（默认 dryrun_preview.md）")
    parser.add_argument("--stdout", action="store_true", help="改为打印到 stdout（Windows 上 emoji 可能编码失败）")
    args = parser.parse_args()

    wb = load_worldbook()
    user_name = wb.get("user_name", "用户")
    ai_name = wb.get("ai_name", "AI")

    msgs = await fetch_all_messages()
    print(f"总消息数: {len(msgs)}", file=sys.stderr)
    if not msgs:
        print("(数据库无消息)")
        return

    groups = await _split_into_groups_smart(msgs, user_name, ai_name)
    print(f"切分组数: {len(groups)}", file=sys.stderr)

    out_lines = [f"# 切片 dry-run · 共 {len(groups)} 组 / {len(msgs)} 条\n"]
    for i, g in enumerate(groups):
        out_lines.append(render_group(i, g, user_name, ai_name))
        if args.summary:
            s = await make_summary(g, user_name, ai_name)
            out_lines.append(f"**摘要**: {s}\n")
        out_lines.append("---\n")

    text = "\n".join(out_lines)
    if args.stdout:
        print(text)
    else:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"已写入 {args.out}", file=sys.stderr)


if __name__ == "__main__":
    asyncio.run(main())
