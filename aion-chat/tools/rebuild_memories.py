"""一次性脚本：清空 memory_cards / memory_links 表 → 重置 digest_anchor → 跑 manual_digest_v2 全量重建。
执行前请确保已备份 chat.db 与 digest_anchor.json。
"""

import asyncio
import sqlite3

from config import DB_PATH, save_digest_anchor
from digest_v2 import manual_digest_v2


async def main():
    # 1) 清空 memory_cards + memory_links
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute("SELECT COUNT(*) FROM memory_cards")
    before = cur.fetchone()[0]
    conn.execute("DELETE FROM memory_links")
    conn.execute("DELETE FROM memory_cards")
    conn.commit()
    conn.close()
    print(f"[wipe] 清空前 memory_cards 行数: {before}")

    # 2) 重置 anchor
    save_digest_anchor(0.0)
    print("[reset] digest_anchor → 0.0")

    # 3) 全量重总结 (V2)
    print("[digest_v2] 开始 manual_digest_v2（可能需要几分钟）...")
    result = await manual_digest_v2()
    print(f"[digest_v2] 结果: {result}")

    # 4) 报告
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute("SELECT COUNT(*) FROM memory_cards")
    after = cur.fetchone()[0]
    cur2 = conn.execute("SELECT COUNT(*) FROM memory_links")
    links = cur2.fetchone()[0]
    conn.close()
    print(f"[done] 新 memory_cards 行数: {after}, memory_links 行数: {links}")


if __name__ == "__main__":
    asyncio.run(main())
