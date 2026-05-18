import asyncio, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.stdout.reconfigure(encoding='utf-8')

async def main():
    from database import get_db, init_db
    await init_db()

    async with get_db() as db:
        await db.execute("DELETE FROM chatroom_memories")
        await db.execute("DELETE FROM chatroom_digest_anchors WHERE room_id='connor_unified'")
        await db.commit()
    print("已清空 chatroom_memories，锚点已重置")

    from chatroom import digest_chatroom
    result = await digest_chatroom()
    print(result)

asyncio.run(main())
