import asyncio
import base64
import struct
import time

import aiosqlite


def test_export_conversations_since(tmp_path, monkeypatch):
    """export_conversations should only return messages after the anchor timestamp."""
    async def _run():
        db_path = tmp_path / "chat.db"
        async with aiosqlite.connect(str(db_path)) as db:
            await db.execute("""CREATE TABLE conversations (
                id TEXT PRIMARY KEY, title TEXT, model TEXT, created_at REAL, updated_at REAL
            )""")
            await db.execute("""CREATE TABLE messages (
                id TEXT PRIMARY KEY, conv_id TEXT, role TEXT, content TEXT,
                created_at REAL, attachments TEXT DEFAULT '', starred INTEGER DEFAULT 0
            )""")
            now = time.time()
            await db.execute("INSERT INTO conversations VALUES (?, ?, ?, ?, ?)",
                             ("conv1", "Test Conv", "gemini", now - 100, now))
            await db.execute("INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?)",
                             ("msg1", "conv1", "user", "old message", now - 50, "", 0))
            await db.execute("INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?)",
                             ("msg2", "conv1", "assistant", "new message", now - 10, "", 0))
            await db.commit()

        monkeypatch.setattr("sync_engine.DB_PATH", db_path)
        from sync_engine import export_conversations
        result = await export_conversations(since_ts=now - 30)

        assert len(result["conversations"]) == 1
        assert result["conversations"][0]["id"] == "conv1"
        msgs = result["messages"]["conv1"]
        assert len(msgs) == 1
        assert msgs[0]["id"] == "msg2"

    asyncio.run(_run())


def test_export_memories_since(tmp_path, monkeypatch):
    """export_memories should base64-encode embedding blobs."""
    async def _run():
        db_path = tmp_path / "chat.db"
        async with aiosqlite.connect(str(db_path)) as db:
            await db.execute("""CREATE TABLE memories (
                id TEXT PRIMARY KEY, content TEXT, type TEXT, created_at REAL,
                source_conv TEXT, embedding BLOB, keywords TEXT DEFAULT '',
                importance REAL DEFAULT 0.5, source_start_ts REAL,
                source_end_ts REAL, unresolved INTEGER DEFAULT 0,
                source_msg_id TEXT, valence REAL DEFAULT 0.0, arousal REAL DEFAULT 0.0
            )""")
            now = time.time()
            fake_emb = struct.pack("3f", 0.1, 0.2, 0.3)
            await db.execute(
                "INSERT INTO memories (id, content, type, created_at, embedding) VALUES (?, ?, ?, ?, ?)",
                ("mem1", "test memory", "event", now - 10, fake_emb),
            )
            await db.commit()

        monkeypatch.setattr("sync_engine.DB_PATH", db_path)
        from sync_engine import export_memories
        result = await export_memories(since_ts=now - 20)

        assert len(result) == 1
        assert result[0]["id"] == "mem1"
        decoded = base64.b64decode(result[0]["embedding"])
        assert decoded == fake_emb

    asyncio.run(_run())


def test_import_conversations_dedup(tmp_path, monkeypatch):
    """import_conversations should skip messages that already exist (dedup by id)."""
    async def _run():
        db_path = tmp_path / "chat.db"
        async with aiosqlite.connect(str(db_path)) as db:
            await db.execute("""CREATE TABLE conversations (
                id TEXT PRIMARY KEY, title TEXT, model TEXT, created_at REAL, updated_at REAL
            )""")
            await db.execute("""CREATE TABLE messages (
                id TEXT PRIMARY KEY, conv_id TEXT, role TEXT, content TEXT,
                created_at REAL, attachments TEXT DEFAULT '', starred INTEGER DEFAULT 0
            )""")
            now = time.time()
            await db.execute("INSERT INTO conversations VALUES (?, ?, ?, ?, ?)",
                             ("conv1", "Existing", "gemini", now - 100, now))
            await db.execute("INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?)",
                             ("msg1", "conv1", "user", "existing", now - 50, "", 0))
            await db.commit()

        monkeypatch.setattr("sync_engine.DB_PATH", db_path)
        from sync_engine import import_conversations

        payload = {
            "conversations": [{"id": "conv1", "title": "Updated", "model": "gemini", "created_at": now - 100, "updated_at": now}],
            "messages": {
                "conv1": [
                    {"id": "msg1", "conv_id": "conv1", "role": "user", "content": "existing", "created_at": now - 50, "attachments": "", "starred": 0},
                    {"id": "msg2", "conv_id": "conv1", "role": "assistant", "content": "new from cloud", "created_at": now - 5, "attachments": "", "starred": 0},
                ]
            },
        }
        stats = await import_conversations(payload)
        assert stats["messages_imported"] == 1

        async with aiosqlite.connect(str(db_path)) as db:
            cur = await db.execute("SELECT COUNT(*) FROM messages WHERE conv_id = 'conv1'")
            count = (await cur.fetchone())[0]
            assert count == 2

    asyncio.run(_run())


def test_import_memories_dedup(tmp_path, monkeypatch):
    """import_memories should skip existing ids and decode base64 embedding."""
    async def _run():
        db_path = tmp_path / "chat.db"
        async with aiosqlite.connect(str(db_path)) as db:
            await db.execute("""CREATE TABLE memories (
                id TEXT PRIMARY KEY, content TEXT, type TEXT, created_at REAL,
                source_conv TEXT, embedding BLOB, keywords TEXT DEFAULT '',
                importance REAL DEFAULT 0.5, source_start_ts REAL,
                source_end_ts REAL, unresolved INTEGER DEFAULT 0,
                source_msg_id TEXT, valence REAL DEFAULT 0.0, arousal REAL DEFAULT 0.0
            )""")
            now = time.time()
            await db.execute(
                "INSERT INTO memories (id, content, type, created_at) VALUES (?, ?, ?, ?)",
                ("mem_existing", "old", "event", now - 100),
            )
            await db.commit()

        monkeypatch.setattr("sync_engine.DB_PATH", db_path)
        from sync_engine import import_memories

        fake_emb = base64.b64encode(struct.pack("3f", 0.1, 0.2, 0.3)).decode()
        payload = [
            {"id": "mem_existing", "content": "old", "type": "event", "created_at": now - 100},
            {"id": "mem_new", "content": "new memory", "type": "event", "created_at": now - 5, "embedding": fake_emb},
        ]
        stats = await import_memories(payload)
        assert stats["imported"] == 1
        assert stats["skipped"] == 1

        async with aiosqlite.connect(str(db_path)) as db:
            cur = await db.execute("SELECT embedding FROM memories WHERE id = 'mem_new'")
            row = await cur.fetchone()
            assert row[0] == struct.pack("3f", 0.1, 0.2, 0.3)

    asyncio.run(_run())
