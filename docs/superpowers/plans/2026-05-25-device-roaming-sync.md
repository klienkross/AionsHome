# Device Roaming Sync Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement multi-device incremental sync via GitHub API so any device can resume chat context seamlessly.

**Architecture:** A `github_sync.py` module handles all GitHub REST API interactions (read/write files, batch commits via Trees API). A `sync_engine.py` module orchestrates export/import of chat messages, memories, schedules, and activity summaries using timestamp-based anchors. A FastAPI route exposes `/api/sync/push` and `/api/sync/pull`. Device auto-registration on first sync.

**Tech Stack:** Python, httpx (async HTTP for GitHub API), aiosqlite, FastAPI, existing config/database modules.

---

## File Structure

| File | Responsibility |
|------|---------------|
| `aion-chat/github_sync.py` | GitHub REST API client: read file, write file, batch commit via Trees API, handle auth |
| `aion-chat/sync_engine.py` | Sync orchestration: export/import chat, memories, schedules, activity; anchor management; device registration |
| `aion-chat/routes/sync.py` | FastAPI endpoints: `/api/sync/push`, `/api/sync/pull`, `/api/sync/status` |
| `aion-chat/config.py` | Add sync config getters (token, device_id, repo info) |
| `aion-chat/main.py` | Register sync router |
| `aion-chat/tests/test_github_sync.py` | Unit tests for GitHub API client |
| `aion-chat/tests/test_sync_engine.py` | Unit tests for export/import logic |

---

### Task 1: GitHub API Client (`github_sync.py`)

**Files:**
- Create: `aion-chat/github_sync.py`
- Test: `aion-chat/tests/test_github_sync.py`

- [ ] **Step 1: Write failing test for `read_file`**

```python
# tests/test_github_sync.py
import pytest
from unittest.mock import AsyncMock, patch

@pytest.mark.asyncio
async def test_read_file_success():
    """read_file should decode base64 content from GitHub API response."""
    import base64
    mock_content = base64.b64encode(b'{"hello": "world"}').decode()
    mock_response = AsyncMock()
    mock_response.status_code = 200
    mock_response.json = lambda: {"content": mock_content, "sha": "abc123"}

    with patch("github_sync._client") as mock_client:
        mock_client.get = AsyncMock(return_value=mock_response)
        from github_sync import read_file
        result = await read_file("test.json")
        assert result == {"content": '{"hello": "world"}', "sha": "abc123"}


@pytest.mark.asyncio
async def test_read_file_not_found():
    """read_file should return None for 404."""
    mock_response = AsyncMock()
    mock_response.status_code = 404

    with patch("github_sync._client") as mock_client:
        mock_client.get = AsyncMock(return_value=mock_response)
        from github_sync import read_file
        result = await read_file("nonexistent.json")
        assert result is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd aion-chat && python -m pytest tests/test_github_sync.py -v`
Expected: FAIL with ModuleNotFoundError (github_sync doesn't exist yet)

- [ ] **Step 3: Implement `github_sync.py`**

```python
# aion-chat/github_sync.py
"""GitHub REST API client for Aions_memory repo sync."""

import base64, json, logging
import httpx

from config import SETTINGS

log = logging.getLogger("github_sync")

_client: httpx.AsyncClient | None = None


def _get_config() -> dict:
    token = SETTINGS.get("github_sync_token", "")
    repo = SETTINGS.get("sync_repo", "")
    if not token or not repo:
        raise ValueError("github_sync_token and sync_repo must be set in settings.json")
    return {"token": token, "repo": repo}


def _headers() -> dict:
    cfg = _get_config()
    return {
        "Authorization": f"token {cfg['token']}",
        "Accept": "application/vnd.github.v3+json",
    }


def _base_url() -> str:
    cfg = _get_config()
    return f"https://api.github.com/repos/{cfg['repo']}"


async def _ensure_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=30.0)
    return _client


async def read_file(path: str) -> dict | None:
    """Read a file from the repo. Returns {"content": str, "sha": str} or None if not found."""
    client = await _ensure_client()
    url = f"{_base_url()}/contents/{path}"
    resp = await client.get(url, headers=_headers())
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    data = resp.json()
    content = base64.b64decode(data["content"]).decode("utf-8")
    return {"content": content, "sha": data["sha"]}


async def write_file(path: str, content: str, message: str, sha: str | None = None) -> str:
    """Write/update a file in the repo. Returns new sha."""
    client = await _ensure_client()
    url = f"{_base_url()}/contents/{path}"
    body = {
        "message": message,
        "content": base64.b64encode(content.encode("utf-8")).decode(),
    }
    if sha:
        body["sha"] = sha
    resp = await client.put(url, headers=_headers(), json=body)
    resp.raise_for_status()
    return resp.json()["content"]["sha"]


async def batch_commit(files: dict[str, str], message: str) -> str:
    """
    Commit multiple files at once using Git Trees API.
    files: {path: content_string}
    Returns the new commit sha.
    """
    client = await _ensure_client()
    headers = _headers()
    base = _base_url()

    # 1. Get ref for main branch
    ref_resp = await client.get(f"{base}/git/ref/heads/main", headers=headers)
    ref_resp.raise_for_status()
    head_sha = ref_resp.json()["object"]["sha"]

    # 2. Get current commit's tree sha
    commit_resp = await client.get(f"{base}/git/commits/{head_sha}", headers=headers)
    commit_resp.raise_for_status()
    base_tree_sha = commit_resp.json()["tree"]["sha"]

    # 3. Create tree with all file blobs
    tree_items = []
    for path, content in files.items():
        tree_items.append({
            "path": path,
            "mode": "100644",
            "type": "blob",
            "content": content,
        })

    tree_resp = await client.post(
        f"{base}/git/trees",
        headers=headers,
        json={"base_tree": base_tree_sha, "tree": tree_items},
    )
    tree_resp.raise_for_status()
    new_tree_sha = tree_resp.json()["sha"]

    # 4. Create commit
    commit_create_resp = await client.post(
        f"{base}/git/commits",
        headers=headers,
        json={
            "message": message,
            "tree": new_tree_sha,
            "parents": [head_sha],
        },
    )
    commit_create_resp.raise_for_status()
    new_commit_sha = commit_create_resp.json()["sha"]

    # 5. Update ref
    await client.patch(
        f"{base}/git/ref/heads/main",
        headers=headers,
        json={"sha": new_commit_sha},
    )

    return new_commit_sha


async def close():
    global _client
    if _client and not _client.is_closed:
        await _client.aclose()
        _client = None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd aion-chat && python -m pytest tests/test_github_sync.py -v`
Expected: PASS

- [ ] **Step 5: Write test for `write_file`**

```python
# Append to tests/test_github_sync.py

@pytest.mark.asyncio
async def test_write_file_new():
    """write_file should PUT base64-encoded content and return sha."""
    mock_response = AsyncMock()
    mock_response.status_code = 201
    mock_response.raise_for_status = lambda: None
    mock_response.json = lambda: {"content": {"sha": "new_sha_456"}}

    with patch("github_sync._client") as mock_client:
        mock_client.put = AsyncMock(return_value=mock_response)
        from github_sync import write_file
        sha = await write_file("test.json", '{"data": 1}', "test commit")
        assert sha == "new_sha_456"
```

- [ ] **Step 6: Run all tests**

Run: `cd aion-chat && python -m pytest tests/test_github_sync.py -v`
Expected: PASS

- [ ] **Step 7: Commit**

```
git add aion-chat/github_sync.py aion-chat/tests/test_github_sync.py
git commit -m "feat: GitHub API client for device sync"
```

---

### Task 2: Config Extension

**Files:**
- Modify: `aion-chat/config.py`

- [ ] **Step 1: Add sync config helpers to `config.py`**

Append after the existing `get_embedding_config` function:

```python
# ── Sync 配置 ──────────────────────────────────────
import platform, secrets

def get_sync_config() -> dict:
    """返回同步配置。首次调用时自动生成 device_id。"""
    device_id = SETTINGS.get("device_id", "")
    if not device_id:
        hostname = platform.node()[:12]
        suffix = secrets.token_hex(2).upper()
        device_id = f"{hostname}-{suffix}"
        SETTINGS["device_id"] = device_id
        save_settings(SETTINGS)
    return {
        "github_sync_token": SETTINGS.get("github_sync_token", ""),
        "sync_repo": SETTINGS.get("sync_repo", ""),
        "device_id": device_id,
        "device_name": SETTINGS.get("device_name", device_id),
    }


def is_sync_configured() -> bool:
    """检查同步是否已配置（token + repo 都有值）。"""
    return bool(SETTINGS.get("github_sync_token")) and bool(SETTINGS.get("sync_repo"))
```

- [ ] **Step 2: Verify import works**

Run: `cd aion-chat && python -c "from config import get_sync_config, is_sync_configured; print(is_sync_configured())"`
Expected: prints `False` (no token configured yet)

- [ ] **Step 3: Commit**

```
git add aion-chat/config.py
git commit -m "feat: add sync config helpers (device_id auto-gen)"
```

---

### Task 3: Sync Engine — Export Logic (`sync_engine.py`)

**Files:**
- Create: `aion-chat/sync_engine.py`
- Test: `aion-chat/tests/test_sync_engine.py`

- [ ] **Step 1: Write failing test for `export_conversations`**

```python
# tests/test_sync_engine.py
import pytest, json, time

@pytest.mark.asyncio
async def test_export_conversations_since(tmp_path, monkeypatch):
    """export_conversations should only return messages after the anchor timestamp."""
    import aiosqlite
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd aion-chat && python -m pytest tests/test_sync_engine.py::test_export_conversations_since -v`
Expected: FAIL (module doesn't exist)

- [ ] **Step 3: Implement export functions in `sync_engine.py`**

```python
# aion-chat/sync_engine.py
"""Sync engine: export/import chat, memories, schedules, activity between local db and cloud."""

import json, time, base64, struct, logging
from datetime import datetime

import aiosqlite

from config import DB_PATH, get_sync_config, is_sync_configured
from activity import generate_activity_summary

log = logging.getLogger("sync_engine")

# ── Export ───────────────────────────────────────────

async def export_conversations(since_ts: float = 0) -> dict:
    """导出 since_ts 之后有新消息的对话及其增量消息。"""
    result = {"conversations": [], "messages": {}}
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        # 找到有新消息的对话
        cur = await db.execute(
            "SELECT DISTINCT conv_id FROM messages WHERE created_at > ?", (since_ts,)
        )
        conv_ids = [row["conv_id"] for row in await cur.fetchall()]
        if not conv_ids:
            return result

        placeholders = ",".join("?" * len(conv_ids))
        cur = await db.execute(
            f"SELECT id, title, model, created_at, updated_at FROM conversations WHERE id IN ({placeholders})",
            conv_ids,
        )
        for row in await cur.fetchall():
            result["conversations"].append(dict(row))

        for conv_id in conv_ids:
            cur = await db.execute(
                "SELECT id, conv_id, role, content, created_at, attachments, starred "
                "FROM messages WHERE conv_id = ? AND created_at > ? ORDER BY created_at",
                (conv_id, since_ts),
            )
            result["messages"][conv_id] = [dict(row) for row in await cur.fetchall()]

    return result


async def export_memories(since_ts: float = 0) -> list[dict]:
    """导出 since_ts 之后新增的记忆。embedding 转为 base64 字符串。"""
    memories = []
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM memories WHERE created_at > ? ORDER BY created_at",
            (since_ts,),
        )
        for row in await cur.fetchall():
            entry = dict(row)
            if entry.get("embedding") and isinstance(entry["embedding"], bytes):
                entry["embedding"] = base64.b64encode(entry["embedding"]).decode("ascii")
            memories.append(entry)
    return memories


async def export_schedules() -> list[dict]:
    """导出所有活跃日程。"""
    schedules = []
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM schedules WHERE status = 'active'"
        )
        for row in await cur.fetchall():
            schedules.append(dict(row))
    return schedules


def export_activity_summary() -> str:
    """生成最近活动摘要的 Markdown 文本。"""
    summaries = generate_activity_summary()
    if not summaries:
        return "# Activity Summary\n\nNo recent activity.\n"

    lines = [f"# Activity Summary\n", f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"]
    for s in summaries:
        time_range = s.get("time_range", "")
        device = s.get("device", "")
        app = s.get("app", "")
        duration = s.get("duration_display", "")
        lines.append(f"- [{time_range}] {device}: {app} ({duration})")
    return "\n".join(lines) + "\n"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd aion-chat && python -m pytest tests/test_sync_engine.py::test_export_conversations_since -v`
Expected: PASS

- [ ] **Step 5: Write failing test for `export_memories`**

```python
# Append to tests/test_sync_engine.py
import struct

@pytest.mark.asyncio
async def test_export_memories_since(tmp_path, monkeypatch):
    """export_memories should base64-encode embedding blobs."""
    import aiosqlite
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
```

- [ ] **Step 6: Run test**

Run: `cd aion-chat && python -m pytest tests/test_sync_engine.py -v`
Expected: PASS

- [ ] **Step 7: Commit**

```
git add aion-chat/sync_engine.py aion-chat/tests/test_sync_engine.py
git commit -m "feat: sync engine export logic (conversations, memories, schedules, activity)"
```

---

### Task 4: Sync Engine — Import Logic

**Files:**
- Modify: `aion-chat/sync_engine.py`
- Modify: `aion-chat/tests/test_sync_engine.py`

- [ ] **Step 1: Write failing test for `import_conversations`**

```python
# Append to tests/test_sync_engine.py

@pytest.mark.asyncio
async def test_import_conversations_dedup(tmp_path, monkeypatch):
    """import_conversations should skip messages that already exist (dedup by id)."""
    import aiosqlite
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd aion-chat && python -m pytest tests/test_sync_engine.py::test_import_conversations_dedup -v`
Expected: FAIL (import_conversations not defined)

- [ ] **Step 3: Implement import functions**

Append to `aion-chat/sync_engine.py`:

```python
# ── Import ───────────────────────────────────────────

async def import_conversations(payload: dict) -> dict:
    """导入对话和消息，跳过已存在的（按 id 去重）。"""
    stats = {"conversations_imported": 0, "messages_imported": 0}
    async with aiosqlite.connect(str(DB_PATH)) as db:
        for conv in payload.get("conversations", []):
            cur = await db.execute("SELECT id FROM conversations WHERE id = ?", (conv["id"],))
            if await cur.fetchone():
                await db.execute(
                    "UPDATE conversations SET title=?, updated_at=? WHERE id=?",
                    (conv["title"], conv["updated_at"], conv["id"]),
                )
            else:
                await db.execute(
                    "INSERT INTO conversations (id, title, model, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                    (conv["id"], conv["title"], conv["model"], conv["created_at"], conv["updated_at"]),
                )
                stats["conversations_imported"] += 1

        for conv_id, messages in payload.get("messages", {}).items():
            for msg in messages:
                cur = await db.execute("SELECT id FROM messages WHERE id = ?", (msg["id"],))
                if await cur.fetchone():
                    continue
                await db.execute(
                    "INSERT INTO messages (id, conv_id, role, content, created_at, attachments, starred) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (msg["id"], msg["conv_id"], msg["role"], msg["content"],
                     msg["created_at"], msg.get("attachments", ""), msg.get("starred", 0)),
                )
                stats["messages_imported"] += 1
        await db.commit()
    return stats


async def import_memories(memories: list[dict]) -> dict:
    """导入记忆，跳过已存在的。embedding 从 base64 解码为 blob。"""
    stats = {"imported": 0, "skipped": 0}
    async with aiosqlite.connect(str(DB_PATH)) as db:
        for mem in memories:
            cur = await db.execute("SELECT id FROM memories WHERE id = ?", (mem["id"],))
            if await cur.fetchone():
                stats["skipped"] += 1
                continue
            embedding = mem.get("embedding")
            if embedding and isinstance(embedding, str):
                embedding = base64.b64decode(embedding)
            await db.execute(
                "INSERT INTO memories (id, content, type, created_at, source_conv, embedding, "
                "keywords, importance, source_start_ts, source_end_ts, unresolved, source_msg_id, valence, arousal) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (mem["id"], mem["content"], mem.get("type", "event"), mem["created_at"],
                 mem.get("source_conv"), embedding, mem.get("keywords", ""),
                 mem.get("importance", 0.5), mem.get("source_start_ts"),
                 mem.get("source_end_ts"), mem.get("unresolved", 0),
                 mem.get("source_msg_id"), mem.get("valence", 0.0), mem.get("arousal", 0.0)),
            )
            stats["imported"] += 1
        await db.commit()
    return stats


async def import_schedules(schedules: list[dict]) -> dict:
    """导入日程，跳过已存在的。"""
    stats = {"imported": 0, "skipped": 0}
    async with aiosqlite.connect(str(DB_PATH)) as db:
        for sched in schedules:
            cur = await db.execute("SELECT id FROM schedules WHERE id = ?", (sched["id"],))
            if await cur.fetchone():
                stats["skipped"] += 1
                continue
            await db.execute(
                "INSERT INTO schedules (id, type, trigger_at, content, created_at, status, repeat, origin, origin_room_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (sched["id"], sched["type"], sched["trigger_at"], sched["content"],
                 sched["created_at"], sched.get("status", "active"), sched.get("repeat"),
                 sched.get("origin", "aion"), sched.get("origin_room_id", "")),
            )
            stats["imported"] += 1
        await db.commit()
    return stats
```

- [ ] **Step 4: Run tests**

Run: `cd aion-chat && python -m pytest tests/test_sync_engine.py -v`
Expected: ALL PASS

- [ ] **Step 5: Write test for `import_memories`**

```python
# Append to tests/test_sync_engine.py

@pytest.mark.asyncio
async def test_import_memories_dedup(tmp_path, monkeypatch):
    """import_memories should skip existing ids and decode base64 embedding."""
    import aiosqlite
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
```

- [ ] **Step 6: Run tests**

Run: `cd aion-chat && python -m pytest tests/test_sync_engine.py -v`
Expected: ALL PASS

- [ ] **Step 7: Commit**

```
git add aion-chat/sync_engine.py aion-chat/tests/test_sync_engine.py
git commit -m "feat: sync engine import logic (conversations, memories, schedules)"
```

---

### Task 5: Sync Engine — Push/Pull Orchestration

**Files:**
- Modify: `aion-chat/sync_engine.py`

- [ ] **Step 1: Implement anchor management and device registration**

Append to `aion-chat/sync_engine.py`:

```python
# ── Anchor & Device State ────────────────────────────

import github_sync

ANCHOR_PATH = "sync_anchor.json"
DEVICE_STATE_PATH = "device_state.json"


async def _read_cloud_json(path: str) -> dict:
    """Read a JSON file from cloud, return empty dict if not found."""
    result = await github_sync.read_file(path)
    if result is None:
        return {}
    return json.loads(result["content"])


async def _get_my_anchor() -> dict:
    """获取本设备的同步锚点。"""
    cfg = get_sync_config()
    anchors = await _read_cloud_json(ANCHOR_PATH)
    return anchors.get(cfg["device_id"], {"last_msg_at": 0, "last_memory_at": 0, "last_sync_at": ""})


async def register_device() -> str:
    """注册当前设备到云端 device_state.json，返回 device_id。"""
    cfg = get_sync_config()
    device_id = cfg["device_id"]
    device_name = cfg["device_name"]

    state = await _read_cloud_json(DEVICE_STATE_PATH)
    if "devices" not in state:
        state = {"active_device": "", "last_active_at": "", "devices": {}}

    now_iso = datetime.now().astimezone().isoformat()
    state["devices"][device_id] = {
        "name": device_name,
        "last_seen": now_iso,
        "status": "active",
    }
    state["active_device"] = device_id
    state["last_active_at"] = now_iso

    await github_sync.write_file(
        DEVICE_STATE_PATH,
        json.dumps(state, ensure_ascii=False, indent=2),
        f"device register: {device_id}",
        sha=(await github_sync.read_file(DEVICE_STATE_PATH) or {}).get("sha"),
    )
    return device_id
```

- [ ] **Step 2: Implement `sync_push` orchestrator**

Append to `aion-chat/sync_engine.py`:

```python
async def sync_push() -> dict:
    """推送本地增量到云端。"""
    if not is_sync_configured():
        return {"ok": False, "error": "Sync not configured (missing token or repo)"}

    cfg = get_sync_config()
    device_id = cfg["device_id"]
    now = time.time()
    now_iso = datetime.now().astimezone().isoformat()

    # 读取锚点
    anchor = await _get_my_anchor()
    last_msg_at = anchor.get("last_msg_at", 0)
    last_memory_at = anchor.get("last_memory_at", 0)

    # 导出增量
    conv_data = await export_conversations(since_ts=last_msg_at)
    memories_data = await export_memories(since_ts=last_memory_at)
    schedules_data = await export_schedules()
    activity_md = export_activity_summary()

    # 构建提交文件
    files = {}

    if conv_data["conversations"]:
        files["chats/conversations.json"] = json.dumps(conv_data["conversations"], ensure_ascii=False, indent=2)
        for conv_id, msgs in conv_data["messages"].items():
            files[f"chats/{conv_id}.json"] = json.dumps(msgs, ensure_ascii=False, indent=2)

    if memories_data:
        files["memories/memories.json"] = json.dumps(memories_data, ensure_ascii=False, indent=2)

    files["schedules.json"] = json.dumps(schedules_data, ensure_ascii=False, indent=2)
    files["activity_summary.md"] = activity_md

    # 更新锚点
    anchors = await _read_cloud_json(ANCHOR_PATH)
    anchors[device_id] = {
        "last_msg_at": now,
        "last_memory_at": now,
        "last_sync_at": now_iso,
    }
    files[ANCHOR_PATH] = json.dumps(anchors, ensure_ascii=False, indent=2)

    # 更新设备状态（标记自己为 idle）
    state = await _read_cloud_json(DEVICE_STATE_PATH)
    if "devices" not in state:
        state = {"active_device": "", "last_active_at": "", "devices": {}}
    state["devices"].setdefault(device_id, {})
    state["devices"][device_id].update({"last_seen": now_iso, "status": "idle"})
    state["active_device"] = ""
    state["last_active_at"] = now_iso
    files[DEVICE_STATE_PATH] = json.dumps(state, ensure_ascii=False, indent=2)

    # 批量提交
    commit_sha = await github_sync.batch_commit(files, f"sync-out from {device_id} at {now_iso}")

    log.info(f"sync_push complete: {len(conv_data['conversations'])} convs, {len(memories_data)} memories, commit={commit_sha[:8]}")
    return {
        "ok": True,
        "conversations_pushed": len(conv_data["conversations"]),
        "memories_pushed": len(memories_data),
        "schedules_pushed": len(schedules_data),
        "commit": commit_sha,
    }
```

- [ ] **Step 3: Implement `sync_pull` orchestrator**

Append to `aion-chat/sync_engine.py`:

```python
async def sync_pull() -> dict:
    """从云端拉取增量导入本地。"""
    if not is_sync_configured():
        return {"ok": False, "error": "Sync not configured (missing token or repo)"}

    cfg = get_sync_config()
    device_id = cfg["device_id"]
    now_iso = datetime.now().astimezone().isoformat()

    # 读取云端对话数据
    conv_result = await github_sync.read_file("chats/conversations.json")
    conversations = json.loads(conv_result["content"]) if conv_result else []

    messages = {}
    for conv in conversations:
        msg_result = await github_sync.read_file(f"chats/{conv['id']}.json")
        if msg_result:
            messages[conv["id"]] = json.loads(msg_result["content"])

    conv_stats = await import_conversations({"conversations": conversations, "messages": messages})

    # 读取记忆
    mem_result = await github_sync.read_file("memories/memories.json")
    memories = json.loads(mem_result["content"]) if mem_result else []
    mem_stats = await import_memories(memories)

    # 读取日程
    sched_result = await github_sync.read_file("schedules.json")
    schedules = json.loads(sched_result["content"]) if sched_result else []
    sched_stats = await import_schedules(schedules)

    # 注册设备 + 标记 active
    await register_device()

    # 更新锚点
    now = time.time()
    anchors = await _read_cloud_json(ANCHOR_PATH)
    anchors[device_id] = {
        "last_msg_at": now,
        "last_memory_at": now,
        "last_sync_at": now_iso,
    }
    anchor_sha = (await github_sync.read_file(ANCHOR_PATH) or {}).get("sha")
    await github_sync.write_file(
        ANCHOR_PATH,
        json.dumps(anchors, ensure_ascii=False, indent=2),
        f"sync-back anchor update from {device_id}",
        sha=anchor_sha,
    )

    log.info(f"sync_pull complete: {conv_stats}, {mem_stats}, {sched_stats}")
    return {
        "ok": True,
        "conversations": conv_stats,
        "memories": mem_stats,
        "schedules": sched_stats,
    }
```

- [ ] **Step 4: Verify module loads**

Run: `cd aion-chat && python -c "import sync_engine; print('OK')"`
Expected: prints `OK`

- [ ] **Step 5: Commit**

```
git add aion-chat/sync_engine.py
git commit -m "feat: sync_push/sync_pull orchestration with anchor management"
```

---

### Task 6: FastAPI Route (`routes/sync.py`)

**Files:**
- Create: `aion-chat/routes/sync.py`
- Modify: `aion-chat/main.py`

- [ ] **Step 1: Create the sync route**

```python
# aion-chat/routes/sync.py
"""同步 API：push/pull/status 端点"""

from fastapi import APIRouter
from config import is_sync_configured, get_sync_config

router = APIRouter()


@router.post("/api/sync/push")
async def api_sync_push():
    """推送本地增量到云端 GitHub 仓库。"""
    if not is_sync_configured():
        return {"ok": False, "error": "Sync not configured. Set github_sync_token and sync_repo in settings."}
    from sync_engine import sync_push
    return await sync_push()


@router.post("/api/sync/pull")
async def api_sync_pull():
    """从云端拉取增量到本地。"""
    if not is_sync_configured():
        return {"ok": False, "error": "Sync not configured. Set github_sync_token and sync_repo in settings."}
    from sync_engine import sync_pull
    return await sync_pull()


@router.get("/api/sync/status")
async def api_sync_status():
    """返回同步配置状态和设备信息。"""
    configured = is_sync_configured()
    if not configured:
        return {"configured": False, "device_id": None}
    cfg = get_sync_config()
    return {
        "configured": True,
        "device_id": cfg["device_id"],
        "device_name": cfg["device_name"],
        "sync_repo": cfg["sync_repo"],
    }
```

- [ ] **Step 2: Register the route in `main.py`**

Add after the existing route imports (around line 50):

```python
from routes import sync as sync_routes
```

Add after the existing `app.include_router` calls (around line 226):

```python
app.include_router(sync_routes.router)
```

- [ ] **Step 3: Verify server starts without error**

Run: `cd aion-chat && python -c "from routes.sync import router; print(f'{len(router.routes)} routes loaded')"`
Expected: prints `3 routes loaded`

- [ ] **Step 4: Commit**

```
git add aion-chat/routes/sync.py aion-chat/main.py
git commit -m "feat: /api/sync/push, /api/sync/pull, /api/sync/status endpoints"
```

---

### Task 7: CLI Interface (rewrite `sync_to_cloud.py`)

**Files:**
- Modify: `aion-chat/sync_to_cloud.py`

- [ ] **Step 1: Rewrite `sync_to_cloud.py` as CLI entry point**

```python
# aion-chat/sync_to_cloud.py
"""CLI 入口：多设备同步推送/拉取。

Usage:
    python sync_to_cloud.py --push     # 推送本地增量到 GitHub
    python sync_to_cloud.py --pull     # 从 GitHub 拉取增量到本地
    python sync_to_cloud.py --status   # 查看同步状态
"""

import asyncio, argparse, json, sys


async def main():
    parser = argparse.ArgumentParser(description="Aion multi-device sync")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--push", action="store_true", help="Push local changes to cloud")
    group.add_argument("--pull", action="store_true", help="Pull cloud changes to local")
    group.add_argument("--status", action="store_true", help="Show sync status")
    args = parser.parse_args()

    from config import is_sync_configured, get_sync_config

    if not is_sync_configured():
        print("ERROR: Sync not configured.")
        print("Set 'github_sync_token' and 'sync_repo' in data/settings.json")
        print('Example: {"github_sync_token": "ghp_xxx", "sync_repo": "owner/Aions_memory"}')
        sys.exit(1)

    if args.status:
        cfg = get_sync_config()
        print(f"Device ID:   {cfg['device_id']}")
        print(f"Device Name: {cfg['device_name']}")
        print(f"Repo:        {cfg['sync_repo']}")
        print(f"Token:       {'***' + cfg['github_sync_token'][-4:]}")
        return

    if args.push:
        from sync_engine import sync_push
        print("Pushing local changes to cloud...")
        result = await sync_push()
        if result["ok"]:
            print(f"Done! Pushed {result['conversations_pushed']} conversations, "
                  f"{result['memories_pushed']} memories, {result['schedules_pushed']} schedules.")
            print(f"Commit: {result['commit'][:12]}")
        else:
            print(f"ERROR: {result['error']}")
            sys.exit(1)

    if args.pull:
        from sync_engine import sync_pull
        print("Pulling cloud changes to local...")
        result = await sync_pull()
        if result["ok"]:
            print(f"Done! Imported:")
            print(f"  Conversations: {result['conversations']}")
            print(f"  Memories: {result['memories']}")
            print(f"  Schedules: {result['schedules']}")
        else:
            print(f"ERROR: {result['error']}")
            sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 2: Verify CLI help**

Run: `cd aion-chat && python sync_to_cloud.py --help`
Expected: Shows usage with --push, --pull, --status options

- [ ] **Step 3: Verify --status works (should error about config)**

Run: `cd aion-chat && python sync_to_cloud.py --status`
Expected: Either shows config info or "ERROR: Sync not configured" (depending on whether token is set)

- [ ] **Step 4: Commit**

```
git add aion-chat/sync_to_cloud.py
git commit -m "feat: rewrite sync_to_cloud.py as CLI with --push/--pull/--status"
```

---

### Task 8: Integration Test (end-to-end with mocked GitHub)

**Files:**
- Create: `aion-chat/tests/test_sync_integration.py`

- [ ] **Step 1: Write integration test**

```python
# aion-chat/tests/test_sync_integration.py
"""End-to-end sync test with mocked GitHub API."""

import pytest, json, time, base64, struct
from unittest.mock import AsyncMock, patch
import aiosqlite


@pytest.fixture
async def setup_db(tmp_path, monkeypatch):
    """Create a test database with sample data."""
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
    return db_path


@pytest.mark.asyncio
async def test_push_then_pull_roundtrip(setup_db, monkeypatch):
    """Full push → pull roundtrip: data pushed should be pullable."""
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

        # Simulate pulling on a new device (empty anchor for this device)
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
```

- [ ] **Step 2: Run integration test**

Run: `cd aion-chat && python -m pytest tests/test_sync_integration.py -v`
Expected: PASS

- [ ] **Step 3: Commit**

```
git add aion-chat/tests/test_sync_integration.py
git commit -m "test: end-to-end sync integration test with mocked GitHub"
```

---

### Task 9: Final Wiring & Manual Test

**Files:**
- No new files, validation only

- [ ] **Step 1: Verify all tests pass together**

Run: `cd aion-chat && python -m pytest tests/test_github_sync.py tests/test_sync_engine.py tests/test_sync_integration.py -v`
Expected: ALL PASS

- [ ] **Step 2: Verify server starts with sync route**

Run: `cd aion-chat && python -c "from main import app; routes = [r.path for r in app.routes if hasattr(r, 'path')]; assert '/api/sync/push' in routes; assert '/api/sync/pull' in routes; print('All sync routes registered')"`
Expected: prints "All sync routes registered"

- [ ] **Step 3: Commit final state**

```
git add -A
git commit -m "chore: final wiring verification for device roaming sync"
```

(Only if there are any uncommitted changes from wiring fixes)

---

## Summary

| Task | What it builds | Depends on |
|------|---------------|------------|
| 1 | GitHub API client | — |
| 2 | Config helpers (device_id, token) | — |
| 3 | Export logic (conversations, memories, schedules, activity) | 2 |
| 4 | Import logic (conversations, memories, schedules) | 2 |
| 5 | Push/Pull orchestrators + anchor/device management | 1, 3, 4 |
| 6 | FastAPI routes | 5 |
| 7 | CLI rewrite | 5 |
| 8 | Integration test | 1-5 |
| 9 | Final wiring & verification | 6, 7, 8 |
