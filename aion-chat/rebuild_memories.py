"""一次性脚本：清空 memories 表 → 重置 digest_anchor → 跑 manual_digest 全量重建。
执行前请确保已备份 chat.db 与 digest_anchor.json。
"""

import asyncio
import sqlite3

from config import DB_PATH, save_digest_anchor
from memory import manual_digest


async def main():
    # 1) 清空 memories
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute("SELECT COUNT(*) FROM memories")
    before = cur.fetchone()[0]
    conn.execute("DELETE FROM memories")
    conn.commit()
    conn.close()
    print(f"[wipe] 清空前 memories 行数: {before}")

    # 2) 重置 anchor
    save_digest_anchor(0.0)
    print("[reset] digest_anchor → 0.0")

    # 3) 全量重总结
    print("[digest] 开始 manual_digest（可能需要几分钟，每组都会调 sentinel）...")
    result = await manual_digest()
    print(f"[digest] 结果: {result}")

    # 4) 报告
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute("SELECT COUNT(*) FROM memories")
    after = cur.fetchone()[0]
    conn.close()
    print(f"[done] 新 memories 行数: {after}")


if __name__ == "__main__":
    asyncio.run(main())
