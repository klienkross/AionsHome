"""诊断 manual_digest 静默失败的脚本：包裹 _call_flash_lite，记录每次调用的
HTTP 状态码、异常类型、响应片段、耗时；不写库，不动 anchor。

输出到 stderr（计数概览）+ diag_log.md（完整记录）。
"""

import asyncio
import json
import sys
import time
import traceback
from datetime import datetime

import aiosqlite
import httpx

import memory
from config import DB_PATH, get_key, load_worldbook
from memory import _split_into_groups_smart, get_embedding


CALL_LOG: list[dict] = []
EMBED_LOG: list[dict] = []


_REAL_EMBED = memory.get_embedding


async def instrumented_get_embedding(text: str):
    entry = {"text_head": text[:60].replace("\n", " "), "elapsed_ms": None, "ok": False, "vec_len": None}
    t0 = time.time()
    try:
        vec = await _REAL_EMBED(text)
        entry["elapsed_ms"] = int((time.time() - t0) * 1000)
        if vec:
            entry["ok"] = True
            entry["vec_len"] = len(vec)
    except Exception as e:
        entry["exception"] = f"{type(e).__name__}: {str(e)[:200]}"
    EMBED_LOG.append(entry)
    return vec if entry["ok"] else None


memory.get_embedding = instrumented_get_embedding


_REAL_CALL = memory._call_flash_lite


async def instrumented_call_flash_lite(prompt: str, max_retries: int = 2) -> dict | None:
    """委托给真正的 _call_flash_lite（保留节流/重试），但把每次调用的耗时与结果记下来。"""
    entry = {
        "ts": time.time(),
        "prompt_len": len(prompt),
        "prompt_head": prompt[:80].replace("\n", " "),
        "elapsed_ms": None,
        "json_ok": False,
        "summary_head": None,
    }
    t0 = time.time()
    result = await _REAL_CALL(prompt, max_retries=max_retries)
    entry["elapsed_ms"] = int((time.time() - t0) * 1000)
    if result is not None:
        entry["json_ok"] = True
        if isinstance(result, dict):
            s = str(result.get("summary", ""))
            entry["summary_head"] = s[:80].replace("\n", " ")
    CALL_LOG.append(entry)
    return result


memory._call_flash_lite = instrumented_call_flash_lite


async def fetch_all_messages() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT id, conv_id, role, content, created_at FROM messages "
            "WHERE role IN ('user','assistant') ORDER BY created_at ASC"
        )
        rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def run_summary(group, user_name, ai_name):
    group_start = datetime.fromtimestamp(group[0]["created_at"]).strftime("%Y年%m月%d日 %H:%M")
    group_end = datetime.fromtimestamp(group[-1]["created_at"]).strftime("%Y年%m月%d日 %H:%M")
    date_header = f"[对话时间范围: {group_start} ~ {group_end}]\n"
    body = date_header + "\n".join([
        f"[{datetime.fromtimestamp(m['created_at']).strftime('%m-%d %H:%M')}] "
        f"{user_name if m['role']=='user' else ai_name}: {m['content'][:300]}"
        for m in group
    ])
    prompt = (
        f"你是记忆压缩师。使用 \"{user_name}\" 和 \"{ai_name}\" 指代双方。\n"
        f"输出 JSON: {{\"summary\": \"...\", \"keywords\": [...], \"importance\": 0.5, \"unresolved\": false}}\n\n"
        f"【对话】\n{body}"
    )
    return await instrumented_call_flash_lite(prompt)


async def main():
    wb = load_worldbook()
    user_name = wb.get("user_name", "用户")
    ai_name = wb.get("ai_name", "AI")

    msgs = await fetch_all_messages()
    print(f"[diag] 总消息数: {len(msgs)}", file=sys.stderr)

    # 跑切分（会触发语义切分调用，也被记录）
    groups = await _split_into_groups_smart(msgs, user_name, ai_name)
    print(f"[diag] 切分组数: {len(groups)}", file=sys.stderr)

    # 对每组跑一次摘要 prompt + embedding（不入库）
    summary_results = []
    for i, g in enumerate(groups):
        r = await run_summary(g, user_name, ai_name)
        if r and r.get("summary"):
            vec = await instrumented_get_embedding(r["summary"])
            ok_str = "OK  " if vec else "EMB-FAIL"
        else:
            ok_str = "SUM-FAIL"
        print(f"[diag] group {i:02d} [{len(g)}条] {ok_str}", file=sys.stderr)
        summary_results.append((i, g, r))

    # 写日志
    with open("diag_log.md", "w", encoding="utf-8") as f:
        f.write(f"# 诊断日志\n\n总消息: {len(msgs)} | 组数: {len(groups)} | flash-lite 调用: {len(CALL_LOG)} | embedding 调用: {len(EMBED_LOG)}\n\n")
        f.write(f"## Embedding 统计\n- 成功: {sum(1 for e in EMBED_LOG if e['ok'])}/{len(EMBED_LOG)}\n\n")

        ok_count = sum(1 for c in CALL_LOG if c["json_ok"])
        avg_ms = int(sum(c["elapsed_ms"] for c in CALL_LOG) / max(len(CALL_LOG), 1))
        f.write(f"## flash-lite 调用统计\n- 成功: {ok_count}/{len(CALL_LOG)}\n- 平均耗时: {avg_ms}ms\n\n")

        # 摘要结果（成功/失败）
        f.write("## 各组摘要结果\n\n")
        for i, g, r in summary_results:
            start = datetime.fromtimestamp(g[0]["created_at"]).strftime("%m-%d %H:%M")
            end = datetime.fromtimestamp(g[-1]["created_at"]).strftime("%m-%d %H:%M")
            status = "✓" if r and r.get("summary") else "✗"
            f.write(f"- {status} group {i:02d} [{len(g)}条 {start}-{end}]")
            if r and r.get("summary"):
                f.write(f" → {r['summary'][:80]}\n")
            else:
                f.write(" → (无摘要)\n")
        f.write("\n")

        f.write("## 完整调用日志\n\n")
        for i, c in enumerate(CALL_LOG):
            ok = "✓" if c["json_ok"] else "✗"
            f.write(f"- {ok} call {i:02d} [{c['prompt_len']}字 / {c['elapsed_ms']}ms] {c['prompt_head']}\n")
            if c.get("summary_head"):
                f.write(f"  → {c['summary_head']}\n")
        f.write("\n## Embedding 日志\n\n")
        for i, e in enumerate(EMBED_LOG):
            ok = "✓" if e["ok"] else "✗"
            f.write(f"- {ok} embed {i:02d} [{e['elapsed_ms']}ms vec_len={e['vec_len']}] {e['text_head']}\n")
            if e.get("exception"):
                f.write(f"  ⚠ {e['exception']}\n")

    print(f"[diag] 日志写入 diag_log.md", file=sys.stderr)


if __name__ == "__main__":
    asyncio.run(main())
