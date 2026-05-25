"""End-to-end sync test with mocked GitHub API."""

import asyncio
import base64
import json
import struct
import time
from unittest.mock import patch

import aiosqlite


def test_push_then_pull_roundtrip(tmp_path, monkeypatch):
    """Full push -> pull roundtrip: data pushed should be pullable."""
    async def _run():
        db_path = tmp_path / "chat.db"
        async with aiosqlite.connect(str(db_path)) as db:
            await db.execute("""CREATE TABLE conversations (
                id TEXT PRIMARY KEY, title TEXT, model TEXT, created_at REAL, updated_at REAL)""")
            await db.execute("""CREATE TABLE messages (
                id TEXT PRIMARY KEY, conv_id TEXT, role TEXT, content TEXT,
                created_at REAL, attachments TEXT DEFAULT '', starred INTEGER DEFAULT 0)""")
            await db.execute("""CREATE TABLE memories (
                id TEXT PRIMARY KEY, content TEXT, type TEXT, created_at REAL,
                source_conv TEXT, embedding BLOB, keywords TEXT DEFAULT '',
                importance REAL DEFAULT 0.5, source_start_ts REAL, source_end_ts REAL,
                unresolved INTEGER DEFAULT 0, source_msg_id TEXT, valence REAL DEFAULT 0.0, arousal REAL DEFAULT 0.0)""")
            await db.execute("""CREATE TABLE schedules (
                id TEXT PRIMARY KEY, type TEXT, trigger_at TEXT, content TEXT,
                created_at REAL, status TEXT DEFAULT 'active', repeat TEXT,
                origin TEXT DEFAULT 'aion', origin_room_id TEXT DEFAULT '')""")

            now = time.time()
            await db.execute("INSERT INTO conversations VALUES (?, ?, ?, ?, ?)",
                             ("c1", "Test Chat", "gemini", now - 200, now))
            await db.execute("INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?)",
                             ("m1", "c1", "user", "hello", now - 100, "", 0))
            await db.execute("INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?)",
                             ("m2", "c1", "assistant", "hi there", now - 90, "", 0))
            emb = struct.pack("3f", 0.5, 0.6, 0.7)
            await db.execute(
                "INSERT INTO memories (id, content, type, created_at, embedding) VALUES (?, ?, ?, ?, ?)",
                ("mem1", "user likes cats", "preference", now - 80, emb))
            await db.execute(
                "INSERT INTO schedules (id, type, trigger_at, content, created_at, status) VALUES (?, ?, ?, ?, ?, ?)",
                ("s1", "reminder", "2026-05-26 09:00", "buy milk", now - 60, "active"))
            await db.commit()

        monkeypatch.setattr("sync_engine.DB_PATH", db_path)
        monkeypatch.setattr("config.SETTINGS", {
            "github_sync_token": "ghp_test",
            "sync_repo": "testuser/Aions_memory",
            "device_id": "test-pc-1234",
            "device_name": "Test PC",
        })

        cloud_storage = {}

        async def mock_read_file(path):
            if path in cloud_storage:
                return {"content": cloud_storage[path], "sha": "fake_sha"}
            return None

        async def mock_write_file(path, content, message, sha=None):
            cloud_storage[path] = content
            return "new_sha"

        async def mock_batch_commit(files, message):
            for path, content in files.items():
                cloud_storage[path] = content
            return "commit_sha_abc"

        monkeypatch.setattr("sync_engine.generate_activity_summary", lambda *a, **kw: [])

        with patch("github_sync.read_file", side_effect=mock_read_file), \
             patch("github_sync.write_file", side_effect=mock_write_file), \
             patch("github_sync.batch_commit", side_effect=mock_batch_commit):

            from sync_engine import sync_push, sync_pull

            # Push
            push_result = await sync_push()
            assert push_result["ok"] is True
            assert push_result["conversations_pushed"] == 1
            assert push_result["memories_pushed"] == 1

            # Verify cloud has data
            assert "chats/conversations.json" in cloud_storage
            assert "memories/memories.json" in cloud_storage
            assert "schedules.json" in cloud_storage

            # Simulate pulling on a new device (different device_id)
            monkeypatch.setattr("config.SETTINGS", {
                "github_sync_token": "ghp_test",
                "sync_repo": "testuser/Aions_memory",
                "device_id": "new-laptop-5678",
                "device_name": "New Laptop",
            })

            # Pull (import into same db — messages already exist so dedup should work)
            pull_result = await sync_pull()
            assert pull_result["ok"] is True
            assert pull_result["memories"]["skipped"] == 1  # mem1 already exists

    asyncio.run(_run())
