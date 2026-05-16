"""一次性回填已有消息的 chain_hash"""
import asyncio
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

import aiosqlite
from config import DB_PATH
from chain_hash import compute_chain_hash


async def backfill():
    async with aiosqlite.connect(DB_PATH) as db:
        # 私聊
        rows = await db.execute_fetchall("SELECT DISTINCT conv_id FROM messages")
        conv_count = len(rows)
        for i, (conv_id,) in enumerate(rows):
            msgs = await db.execute_fetchall(
                "SELECT id, content, created_at FROM messages WHERE conv_id = ? ORDER BY created_at ASC",
                (conv_id,)
            )
            prev_hash = '00000000'
            for msg_id, content, created_at in msgs:
                new_hash = compute_chain_hash(prev_hash, msg_id, content or '', created_at)
                await db.execute("UPDATE messages SET chain_hash = ? WHERE id = ?", (new_hash, msg_id))
                prev_hash = new_hash
            print(f"  messages: {i+1}/{conv_count} conv_id={conv_id} ({len(msgs)} msgs)")

        # 群聊
        rooms = await db.execute_fetchall("SELECT DISTINCT room_id FROM chatroom_messages")
        room_count = len(rooms)
        for i, (room_id,) in enumerate(rooms):
            msgs = await db.execute_fetchall(
                "SELECT id, content, created_at FROM chatroom_messages WHERE room_id = ? ORDER BY created_at ASC",
                (room_id,)
            )
            prev_hash = '00000000'
            for msg_id, content, created_at in msgs:
                new_hash = compute_chain_hash(prev_hash, msg_id, content or '', created_at)
                await db.execute("UPDATE chatroom_messages SET chain_hash = ? WHERE id = ?", (new_hash, msg_id))
                prev_hash = new_hash
            print(f"  chatroom: {i+1}/{room_count} room_id={room_id} ({len(msgs)} msgs)")

        await db.commit()
    print("Done: all messages backfilled with chain_hash")


if __name__ == '__main__':
    asyncio.run(backfill())
