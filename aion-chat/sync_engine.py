"""Sync engine: export/import chat, memories, schedules, activity between local db and cloud."""

import base64
import json
import logging
import time
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

    lines = ["# Activity Summary\n", f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"]
    for s in summaries:
        time_range = s.get("time_range", "")
        device = s.get("device", "")
        app = s.get("app", "")
        duration = s.get("duration_display", "")
        lines.append(f"- [{time_range}] {device}: {app} ({duration})")
    return "\n".join(lines) + "\n"


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

    existing = await github_sync.read_file(DEVICE_STATE_PATH)
    await github_sync.write_file(
        DEVICE_STATE_PATH,
        json.dumps(state, ensure_ascii=False, indent=2),
        f"device register: {device_id}",
        sha=existing["sha"] if existing else None,
    )
    return device_id


async def sync_push() -> dict:
    """推送本地增量到云端。"""
    if not is_sync_configured():
        return {"ok": False, "error": "Sync not configured (missing token or repo)"}

    cfg = get_sync_config()
    device_id = cfg["device_id"]
    now = time.time()
    now_iso = datetime.now().astimezone().isoformat()

    anchor = await _get_my_anchor()
    last_msg_at = anchor.get("last_msg_at", 0)
    last_memory_at = anchor.get("last_memory_at", 0)

    conv_data = await export_conversations(since_ts=last_msg_at)
    memories_data = await export_memories(since_ts=last_memory_at)
    schedules_data = await export_schedules()
    activity_md = export_activity_summary()

    files = {}

    if conv_data["conversations"]:
        files["chats/conversations.json"] = json.dumps(conv_data["conversations"], ensure_ascii=False, indent=2)
        for conv_id, msgs in conv_data["messages"].items():
            files[f"chats/{conv_id}.json"] = json.dumps(msgs, ensure_ascii=False, indent=2)

    if memories_data:
        files["memories/memories.json"] = json.dumps(memories_data, ensure_ascii=False, indent=2)

    files["schedules.json"] = json.dumps(schedules_data, ensure_ascii=False, indent=2)
    files["activity_summary.md"] = activity_md

    anchors = await _read_cloud_json(ANCHOR_PATH)
    anchors[device_id] = {
        "last_msg_at": now,
        "last_memory_at": now,
        "last_sync_at": now_iso,
    }
    files[ANCHOR_PATH] = json.dumps(anchors, ensure_ascii=False, indent=2)

    state = await _read_cloud_json(DEVICE_STATE_PATH)
    if "devices" not in state:
        state = {"active_device": "", "last_active_at": "", "devices": {}}
    state["devices"].setdefault(device_id, {})
    state["devices"][device_id].update({"last_seen": now_iso, "status": "idle"})
    state["active_device"] = ""
    state["last_active_at"] = now_iso
    files[DEVICE_STATE_PATH] = json.dumps(state, ensure_ascii=False, indent=2)

    commit_sha = await github_sync.batch_commit(files, f"sync-out from {device_id} at {now_iso}")

    log.info(f"sync_push complete: {len(conv_data['conversations'])} convs, {len(memories_data)} memories, commit={commit_sha[:8]}")
    return {
        "ok": True,
        "conversations_pushed": len(conv_data["conversations"]),
        "memories_pushed": len(memories_data),
        "schedules_pushed": len(schedules_data),
        "commit": commit_sha,
    }


async def sync_pull() -> dict:
    """从云端拉取增量导入本地。"""
    if not is_sync_configured():
        return {"ok": False, "error": "Sync not configured (missing token or repo)"}

    cfg = get_sync_config()
    device_id = cfg["device_id"]
    now_iso = datetime.now().astimezone().isoformat()

    conv_result = await github_sync.read_file("chats/conversations.json")
    conversations = json.loads(conv_result["content"]) if conv_result else []

    messages = {}
    for conv in conversations:
        msg_result = await github_sync.read_file(f"chats/{conv['id']}.json")
        if msg_result:
            messages[conv["id"]] = json.loads(msg_result["content"])

    conv_stats = await import_conversations({"conversations": conversations, "messages": messages})

    mem_result = await github_sync.read_file("memories/memories.json")
    memories = json.loads(mem_result["content"]) if mem_result else []
    mem_stats = await import_memories(memories)

    sched_result = await github_sync.read_file("schedules.json")
    schedules = json.loads(sched_result["content"]) if sched_result else []
    sched_stats = await import_schedules(schedules)

    await register_device()

    now = time.time()
    anchors = await _read_cloud_json(ANCHOR_PATH)
    anchors[device_id] = {
        "last_msg_at": now,
        "last_memory_at": now,
        "last_sync_at": now_iso,
    }
    anchor_sha_result = await github_sync.read_file(ANCHOR_PATH)
    await github_sync.write_file(
        ANCHOR_PATH,
        json.dumps(anchors, ensure_ascii=False, indent=2),
        f"sync-back anchor update from {device_id}",
        sha=anchor_sha_result["sha"] if anchor_sha_result else None,
    )

    log.info(f"sync_pull complete: {conv_stats}, {mem_stats}, {sched_stats}")
    return {
        "ok": True,
        "conversations": conv_stats,
        "memories": mem_stats,
        "schedules": sched_stats,
    }
