"""
聊天室 API 路由：房间 CRUD、发消息(SSE)、AI 互聊、记忆接口
"""

import json, time, asyncio, random, re, mimetypes
from typing import Optional, List
from pathlib import Path
from datetime import date

import aiosqlite, httpx
from fastapi import APIRouter, Query, UploadFile, File
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from config import DEFAULT_MODEL, DATA_DIR, CODEX_UPLOADS_DIR
from database import get_db
from ws import manager
from ai_providers import stream_ai, CLI_STATUS_PREFIX
from tts import TTSStreamer
from chatroom import (
    send_to_connor, check_connor_online, load_chatroom_config, save_chatroom_config,
    build_aion_group_context, build_connor_group_context,
    build_connor_1v1_context, get_main_chat_recent, format_cross_context,
    recall_chatroom_memories, recall_main_chat_memories, save_chatroom_memory,
    digest_chatroom, connor_1v1_on_message, _CONNOR_TIMEOUT_SENTINEL,
    stream_connor_cli,
)
from context_builder import (
    MUSIC_CMD_PATTERN, HEART_CMD_PATTERN, MEMORY_CMD_PATTERN,
    ACTIVITY_CHECK_PATTERN, SELFIE_CMD_PATTERN, DRAW_CMD_PATTERN,
    POI_SEARCH_PATTERN, TOY_CMD_PATTERN, PET_CMD_PATTERN,
    VIDEO_CALL_CMD, META_TAG_PATTERN, strip_tool_commands,
)
from memory import get_embedding
from schedule import process_schedule_commands, ALARM_CMD, REMINDER_CMD, MONITOR_CMD, _parse_dt
from music import search_songs, get_audio_url
from camera import cam, CAM_CHECK_CMD

router = APIRouter(prefix="/api/chatroom", tags=["chatroom"])


# ══════════════════════════════════════════════════
#  群聊工具指令处理
# ══════════════════════════════════════════════════

async def _chatroom_sys_msg(room_id: str, text: str, _q: asyncio.Queue):
    """在聊天室中插入系统消息气泡"""
    now = time.time()
    msg_id = f"cm_{int(now * 1000)}_sys"
    async with get_db() as db:
        await db.execute(
            "INSERT INTO chatroom_messages (id, room_id, sender, content, created_at, attachments) VALUES (?,?,?,?,?,?)",
            (msg_id, room_id, "system", text, now, "[]"),
        )
        await db.commit()
    msg = {"id": msg_id, "room_id": room_id, "sender": "system", "content": text, "created_at": now, "attachments": []}
    await _q.put({"type": "system_msg", "message": msg})


async def _process_chatroom_commands(full_text: str, room_id: str, who: str, msg_id: str, _q: asyncio.Queue) -> tuple[str, dict]:
    """处理 AI 回复中的工具指令，执行副作用，返回 (清理后的文本, 触发的后续动作信息)。
    who: 'Aion' 或 'Connor'"""
    from ws import manager as ws_manager
    triggered = {}  # 收集需要后续处理的动作

    # ── 点歌 ──
    music_matches = MUSIC_CMD_PATTERN.findall(full_text)
    music_cards = []
    if music_matches:
        for keyword in music_matches:
            keyword = keyword.strip()
            try:
                results = search_songs(keyword, limit=5)
                if results:
                    song = results[0]
                    song["audio_url"] = get_audio_url(song["id"])
                    song["candidates"] = results[1:4]
                    music_cards.append(song)
            except Exception:
                pass
        full_text = MUSIC_CMD_PATTERN.sub("", full_text)

    if music_cards:
        parts = [f"《{s['name']}》- {s['artist']}" for s in music_cards]
        await _chatroom_sys_msg(room_id, f"🎵 {who}点了一首{' / '.join(parts)}", _q)
        music_data = {"type": "music", "msg_id": msg_id, "cards": music_cards, "autoplay": True}
        await _q.put(music_data)
        await ws_manager.broadcast({"type": "music", "data": music_data})

    # ── 日程/闹钟（先检测指令生成系统消息，再交给 schedule 模块处理） ──
    for match in ALARM_CMD.finditer(full_text):
        try:
            raw_dt, content = match.group(1), match.group(2)
            dt = _parse_dt(raw_dt)
            if dt and content.strip():
                await _chatroom_sys_msg(room_id, f"⏰ {who}设置了 {dt.replace('T', ' ')} 的闹铃：{content.strip()}", _q)
        except Exception:
            pass
    for match in REMINDER_CMD.finditer(full_text):
        try:
            raw_dt, content = match.group(1), match.group(2)
            dt = _parse_dt(raw_dt)
            if dt and content.strip():
                await _chatroom_sys_msg(room_id, f"📅 {who}设置了 {dt.replace('T', ' ')} 的日程：{content.strip()}", _q)
        except Exception:
            pass
    for match in MONITOR_CMD.finditer(full_text):
        try:
            raw_dt, content = match.group(1), match.group(2)
            dt = _parse_dt(raw_dt)
            if dt and content.strip():
                await _chatroom_sys_msg(room_id, f"👀 {who}设置了 {dt.replace('T', ' ')} 的定时查岗：{content.strip()}", _q)
        except Exception:
            pass
    full_text = await process_schedule_commands(full_text, None)

    # ── 查岗 ──
    cam_triggered = CAM_CHECK_CMD in full_text
    if cam_triggered:
        full_text = full_text.replace(CAM_CHECK_CMD, "")
        if cam.running:
            await _chatroom_sys_msg(room_id, f"📷 {who}查看了监控", _q)
            from routes.chat import _delayed_cam_check
            # 群聊查岗无 conv_id，使用特殊标记
            asyncio.create_task(_delayed_cam_check(f"chatroom:{room_id}", ""))

    # ── 查看动态 ──
    activity_match = ACTIVITY_CHECK_PATTERN.search(full_text)
    if activity_match:
        try:
            activity_n = int(activity_match.group(1))
        except (ValueError, IndexError):
            activity_n = 6
        activity_n = max(1, min(12, activity_n)) if activity_n > 0 else 6
        full_text = ACTIVITY_CHECK_PATTERN.sub("", full_text)
        await _chatroom_sys_msg(room_id, f"📊 {who}查看了用户动态", _q)
        triggered["activity"] = activity_n

    # ── HEART ──
    heart_matches = HEART_CMD_PATTERN.findall(full_text)
    if heart_matches:
        full_text = HEART_CMD_PATTERN.sub("", full_text)
        for hw_content in heart_matches:
            hw_content = hw_content.strip()
            if hw_content:
                hw_now = time.time()
                hw_id = f"hw_{int(hw_now*1000)}"
                async with get_db() as hw_db:
                    await hw_db.execute(
                        "INSERT INTO heart_whispers (id, conv_id, msg_id, content, created_at) VALUES (?,?,?,?,?)",
                        (hw_id, f"chatroom:{room_id}", msg_id, hw_content, hw_now)
                    )
                    await hw_db.commit()
                hw_data = {"type": "heart_whisper", "id": hw_id, "msg_id": msg_id, "content": hw_content, "created_at": hw_now}
                await _q.put(hw_data)
                await ws_manager.broadcast({"type": "heart_whisper", "data": hw_data})

    # ── MEMORY ──
    memory_matches = MEMORY_CMD_PATTERN.findall(full_text)
    if memory_matches:
        full_text = MEMORY_CMD_PATTERN.sub("", full_text)
        for mem_content in memory_matches:
            mem_content = mem_content.strip()
            if mem_content:
                from memory import _pack_embedding
                mem_now = time.time()
                mem_id = f"mem_{int(mem_now*1000)}"
                vec = await get_embedding(mem_content)
                # 保存到聊天室记忆库
                await save_chatroom_memory(
                    room_id=room_id, scope="group", content=mem_content,
                    keywords="", importance=0.5,
                )
                await _chatroom_sys_msg(room_id, f"💾 {who}记住了：{mem_content[:50]}", _q)

    # ── POI 搜索 ──
    poi_matches = POI_SEARCH_PATTERN.findall(full_text)
    if poi_matches:
        full_text = POI_SEARCH_PATTERN.sub("", full_text)
        triggered["poi"] = poi_matches

    # ── 玩具 ──
    toy_matches = TOY_CMD_PATTERN.findall(full_text)
    if toy_matches:
        full_text = TOY_CMD_PATTERN.sub("", full_text)
        toy_data = {"type": "toy_command", "commands": toy_matches, "msg_id": msg_id}
        await _q.put(toy_data)
        await ws_manager.broadcast({"type": "toy_command", "data": toy_data})

    # ── 桌宠 ──
    pet_matches = PET_CMD_PATTERN.findall(full_text)
    if pet_matches:
        full_text = PET_CMD_PATTERN.sub("", full_text)
        await ws_manager.broadcast({"type": "pet_command", "data": {"action": pet_matches[-1].lower()}})

    # ── 图片生成 ──
    selfie_match = SELFIE_CMD_PATTERN.search(full_text)
    draw_match = DRAW_CMD_PATTERN.search(full_text)
    if selfie_match:
        triggered["image_gen"] = {"prompt": selfie_match.group(1).strip(), "is_selfie": True}
        full_text = SELFIE_CMD_PATTERN.sub("", full_text)
    elif draw_match:
        triggered["image_gen"] = {"prompt": draw_match.group(1).strip(), "is_selfie": False}
        full_text = DRAW_CMD_PATTERN.sub("", full_text)

    # ── 视频通话 ──
    if VIDEO_CALL_CMD in full_text:
        full_text = full_text.replace(VIDEO_CALL_CMD, "")

    # 清理 META 标签
    full_text = META_TAG_PATTERN.sub("", full_text)

    return full_text.strip(), triggered


# ══════════════════════════════════════════════════
#  群聊工具指令后续动作（异步执行）
# ══════════════════════════════════════════════════

def _fire_chatroom_followups(triggered: dict, room_id: str, sender: str, model_key: str):
    """根据 _process_chatroom_commands 返回的 triggered dict，启动异步后续任务"""
    if triggered.get("activity"):
        asyncio.create_task(_chatroom_activity_check(room_id, sender, model_key, triggered["activity"]))
    if triggered.get("poi"):
        asyncio.create_task(_chatroom_poi_check(room_id, sender, model_key, triggered["poi"]))
    if triggered.get("image_gen"):
        ig = triggered["image_gen"]
        asyncio.create_task(_chatroom_image_gen(room_id, sender, ig["prompt"], ig["is_selfie"]))


async def _chatroom_activity_check(room_id: str, sender: str, model_key: str, n: int):
    """聊天室版查看动态：获取摘要 → AI 追加回复到聊天室"""
    from activity import get_activity_summary_for_prompt
    from config import load_worldbook, SETTINGS

    n = max(1, min(12, n))
    summary_text = get_activity_summary_for_prompt(n)
    if not summary_text:
        summary_text = "（当前没有设备活动记录）"

    wb = load_worldbook()
    user_name = wb.get("user_name", "用户")
    minutes = n * 10

    # 获取聊天室最近消息作为上下文
    _, msgs = await _load_room_and_messages(room_id, limit=10)
    recent = []
    for m in msgs:
        role = "assistant" if m["sender"] in ("aion", "connor") else "user"
        prefix = ""
        if m["sender"] == "connor":
            prefix = "[Connor] "
        elif m["sender"] == "aion":
            prefix = "[Aion] "
        recent.append({"role": role, "content": prefix + (m.get("content") or "")})

    activity_prompt = (
        f"你刚才想了解{user_name}最近在干什么，以下是系统采集到的{user_name}过去{minutes}分钟的设备使用动态（每10分钟一条摘要）：\n\n"
        f"【设备活动动态】\n{summary_text}\n\n"
        f"请根据这些动态信息，自然地和{user_name}聊聊。不需要再说\"让我看看\"之类的话，直接根据动态内容回应即可。"
    )

    # 构建 prompt
    prefix_msgs = []
    if wb.get("ai_persona") and sender == "aion":
        prefix_msgs.append({"role": "user", "content": f"[系统设定 - AI人设]\n{wb['ai_persona']}"})
        prefix_msgs.append({"role": "assistant", "content": "收到，我会按照设定扮演角色。"})
    if wb.get("user_persona"):
        prefix_msgs.append({"role": "user", "content": f"[系统设定 - 用户信息]\n{wb['user_persona']}"})
        prefix_msgs.append({"role": "assistant", "content": "收到，我会记住你的信息。"})

    messages = prefix_msgs + recent + [{"role": "user", "content": activity_prompt}]

    full_text = ""
    try:
        if sender == "aion":
            _temp = SETTINGS.get("temperature")
            async for chunk in stream_ai(messages, model_key, temperature=_temp):
                if chunk.startswith(CLI_STATUS_PREFIX):
                    continue
                full_text += chunk
        else:
            async for chunk in stream_connor_cli(messages=messages):
                if chunk.startswith(CLI_STATUS_PREFIX):
                    continue
                full_text += chunk
    except Exception as e:
        full_text = f"[查看动态失败] {e}"

    if not full_text.strip():
        return

    full_text = strip_tool_commands(full_text)
    await _save_msg(room_id, sender, full_text)
    print(f"[CHATROOM_ACTIVITY] {sender} 查看动态完成, room={room_id}, n={n}")


async def _chatroom_poi_check(room_id: str, sender: str, model_key: str, categories: list[str]):
    """聊天室版 POI 搜索：搜索周边 → AI 追加回复到聊天室"""
    from location import (
        load_location_config, load_location_status, save_location_status,
        amap_poi_search, amap_regeo, format_location_for_prompt,
    )
    from config import load_worldbook, SETTINGS

    cfg = load_location_config()
    amap_key = cfg.get("amap_key", "")
    if not amap_key:
        return

    status = load_location_status()
    lng = status.get("lng", 0)
    lat = status.get("lat", 0)
    if not lng or not lat:
        return

    geo_info = await amap_regeo(lng, lat, amap_key)
    if geo_info:
        status["address"] = geo_info["address"]
        status["adcode"] = geo_info["adcode"]

    search_results = {}
    poi_types = cfg.get("poi_types", {})
    for cat in categories:
        cat = cat.strip()
        type_code = poi_types.get(cat)
        if type_code:
            pois = await amap_poi_search(lng, lat, type_code, amap_key, cfg.get("poi_radius", 2000))
            search_results[cat] = pois
            if "nearby_pois" not in status:
                status["nearby_pois"] = {}
            status["nearby_pois"][cat] = pois

    status["last_api_lng"] = lng
    status["last_api_lat"] = lat
    save_location_status(status)

    if not search_results:
        return

    result_lines = []
    for cat, pois in search_results.items():
        if not pois:
            result_lines.append(f"【{cat}】附近暂无相关结果")
            continue
        result_lines.append(f"【{cat}】")
        for p in pois[:10]:
            entry = f"  - {p['name']}"
            if p.get("distance"):
                entry += f"（{int(p['distance'])}m）"
            if p.get("rating") and p["rating"] != "[]":
                entry += f" ⭐{p['rating']}"
            if p.get("cost") and p["cost"] != "[]":
                entry += f" 人均¥{p['cost']}"
            if p.get("address") and p["address"] != "[]":
                entry += f" | {p['address']}"
            result_lines.append(entry)
    poi_text = "\n".join(result_lines)

    wb = load_worldbook()
    user_name = wb.get("user_name", "用户")
    loc_prompt = format_location_for_prompt()
    poi_prompt = (
        f"你刚才想帮{user_name}搜索周边信息，以下是系统根据{user_name}最新实时坐标搜索到的结果：\n\n"
        f"{poi_text}\n\n"
        f"{loc_prompt}\n\n"
        f"请根据搜索结果，自然地向{user_name}推荐或回答。不需要再说\"让我帮你搜一下\"之类的话，直接根据结果回复即可。"
    )

    _, msgs = await _load_room_and_messages(room_id, limit=10)
    recent = []
    for m in msgs:
        role = "assistant" if m["sender"] in ("aion", "connor") else "user"
        prefix = ""
        if m["sender"] == "connor":
            prefix = "[Connor] "
        elif m["sender"] == "aion":
            prefix = "[Aion] "
        recent.append({"role": role, "content": prefix + (m.get("content") or "")})

    prefix_msgs = []
    if wb.get("ai_persona") and sender == "aion":
        prefix_msgs.append({"role": "user", "content": f"[系统设定 - AI人设]\n{wb['ai_persona']}"})
        prefix_msgs.append({"role": "assistant", "content": "收到，我会按照设定扮演角色。"})

    messages = prefix_msgs + recent + [{"role": "user", "content": poi_prompt}]

    full_text = ""
    try:
        if sender == "aion":
            _temp = SETTINGS.get("temperature")
            async for chunk in stream_ai(messages, model_key, temperature=_temp):
                if chunk.startswith(CLI_STATUS_PREFIX):
                    continue
                full_text += chunk
        else:
            async for chunk in stream_connor_cli(messages=messages):
                if chunk.startswith(CLI_STATUS_PREFIX):
                    continue
                full_text += chunk
    except Exception as e:
        full_text = f"[周边搜索完成但回复生成失败] {e}"

    if not full_text.strip():
        return

    full_text = strip_tool_commands(full_text)
    await _save_msg(room_id, sender, full_text)
    searched_cats = "、".join(c.strip() for c in categories)
    print(f"[CHATROOM_POI] {sender} 搜索完成, room={room_id}, categories={searched_cats}")


async def _chatroom_image_gen(room_id: str, sender: str, prompt: str, is_selfie: bool):
    """聊天室版图片生成"""
    from image_gen import generate_image

    try:
        filename = await generate_image(prompt, is_selfie=is_selfie)
        if filename:
            await _save_msg(room_id, sender, "", attachments=[f"/uploads/{filename}"])
            print(f"[CHATROOM_IMG_GEN] {sender} 生图完成, room={room_id}")
        else:
            print(f"[CHATROOM_IMG_GEN] {sender} 生图失败, room={room_id}")
    except Exception as e:
        print(f"[CHATROOM_IMG_GEN] {sender} 生图异常: {e}")


# ── 图片 URL 检测 & 下载保存 ──
_IMG_URL_RE = re.compile(r'(https?://\S+\.(?:jpg|jpeg|png|gif|webp)(?:\?\S*)?)', re.IGNORECASE)
_MD_IMG_RE = re.compile(r'!\[.*?\]\((https?://\S+?)\)')

ALLOWED_IMG_TYPES = {'image/jpeg', 'image/png', 'image/gif', 'image/webp'}


def _cr_upload_dir() -> Path:
    """返回当天的聊天室图片目录 Connor-Codex/uploads/YYYY-MM-DD/"""
    day_dir = CODEX_UPLOADS_DIR / date.today().isoformat()
    day_dir.mkdir(parents=True, exist_ok=True)
    return day_dir


async def _extract_and_save_images(text: str) -> list[str]:
    """从文本中提取图片 URL，下载并保存到本地，返回本地 URL 列表"""
    urls = set(_IMG_URL_RE.findall(text)) | set(_MD_IMG_RE.findall(text))
    if not urls:
        return []
    saved = []
    day_dir = _cr_upload_dir()
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        for url in urls:
            try:
                resp = await client.get(url)
                if resp.status_code != 200:
                    continue
                ct = resp.headers.get("content-type", "")
                if not ct.startswith("image/"):
                    continue
                ext = mimetypes.guess_extension(ct.split(";")[0].strip()) or ".jpg"
                if ext == ".jpe":
                    ext = ".jpg"
                fname = f"{int(time.time()*1000)}{ext}"
                fpath = day_dir / fname
                fpath.write_bytes(resp.content)
                local_url = f"/cr-uploads/{date.today().isoformat()}/{fname}"
                saved.append(local_url)
            except Exception:
                continue
    return saved


_CONNOR_IMG_TAG_RE = re.compile(r'\[\[image:/uploads/')


def _rewrite_connor_paths(text: str) -> str:
    """将 Connor 回复中的 [[image:/uploads/...]] 重写为 [[image:/cr-uploads/...]]
    Connor 端 /uploads/ 对应本地 Connor-Codex/uploads/，
    aion-chat 端挂载在 /cr-uploads/"""
    return _CONNOR_IMG_TAG_RE.sub('[[image:/cr-uploads/', text)


def _attachments_to_connor_images(attachments: list) -> list[dict]:
    """将 /cr-uploads/... 附件列表转为 Connor 需要的 {url, path} 格式"""
    images = []
    for att in (attachments or []):
        url = att if isinstance(att, str) else (att.get("url") or "")
        if not url:
            continue
        # /cr-uploads/2026-05-07/xxx.jpg → Connor-Codex/uploads/2026-05-07/xxx.jpg
        if url.startswith("/cr-uploads/"):
            rel = url[len("/cr-uploads/"):]
            abs_path = str(CODEX_UPLOADS_DIR / rel).replace("/", "\\")
        else:
            abs_path = url
        images.append({"url": url, "path": abs_path})
    return images


def _collect_last_user_images(msgs: list[dict]) -> list[dict]:
    """从消息列表中提取最后一条用户消息的图片，转为 Connor images 格式"""
    for m in reversed(msgs):
        if m.get("sender") == "user":
            atts = m.get("attachments", [])
            if isinstance(atts, str):
                try: atts = json.loads(atts) if atts else []
                except: atts = []
            if atts:
                return _attachments_to_connor_images(atts)
            break
    return []


# ── Pydantic 模型 ──

class RoomCreate(BaseModel):
    title: str = "新聊天室"
    type: str = "group"  # "group" | "connor_1v1"
    aion_persona: str = ""
    connor_persona: str = ""


class RoomUpdate(BaseModel):
    title: Optional[str] = None
    aion_persona: Optional[str] = None
    connor_persona: Optional[str] = None
    context_minutes: Optional[int] = None
    ai_chat_rounds: Optional[int] = None


class MsgSend(BaseModel):
    content: str
    sender: str = "user"  # "user"
    model: str = DEFAULT_MODEL
    attachments: list = []
    tts_enabled: bool = False
    tts_aion_voice: str = ""
    tts_connor_voice: str = ""


class AiChatTrigger(BaseModel):
    rounds: Optional[int] = None
    model: str = DEFAULT_MODEL
    tts_enabled: bool = False
    tts_aion_voice: str = ""
    tts_connor_voice: str = ""


class MemoryCreate(BaseModel):
    content: str
    keywords: str = ""
    importance: float = 0.5


class MemoryUpdate(BaseModel):
    content: Optional[str] = None
    keywords: Optional[str] = None
    importance: Optional[float] = None


class ConfigUpdate(BaseModel):
    connor_url: Optional[str] = None
    connor_poll_interval: Optional[float] = None
    connor_poll_timeout: Optional[int] = None


# ══════════════════════════════════════════════════
#  配置
# ══════════════════════════════════════════════════

@router.get("/config")
async def get_config():
    cfg = load_chatroom_config()
    # connor_online 不再阻塞返回，前端可通过 /connor-status 单独查询
    return {**cfg, "connor_online": None}


@router.put("/config")
async def update_config(body: ConfigUpdate):
    cfg = load_chatroom_config()
    if body.connor_url is not None:
        cfg["connor_url"] = body.connor_url
    if body.connor_poll_interval is not None:
        cfg["connor_poll_interval"] = body.connor_poll_interval
    if body.connor_poll_timeout is not None:
        cfg["connor_poll_timeout"] = body.connor_poll_timeout
    save_chatroom_config(cfg)
    return {"ok": True}


@router.get("/connor-status")
async def connor_status():
    online = await check_connor_online()
    return {"online": online}


# ══════════════════════════════════════════════════
#  聊天室图片上传
# ══════════════════════════════════════════════════

@router.post("/upload")
async def chatroom_upload(file: UploadFile = File(...)):
    """聊天室专用上传，保存到 Connor-Codex/uploads/YYYY-MM-DD/"""
    base_type = (file.content_type or "").split(";")[0].strip()
    if base_type not in ALLOWED_IMG_TYPES:
        return {"error": f"不支持的文件类型: {file.content_type}"}
    ext = mimetypes.guess_extension(base_type) or ".jpg"
    if ext == ".jpe":
        ext = ".jpg"
    content = await file.read()
    if len(content) > 20 * 1024 * 1024:
        return {"error": "文件太大，最大 20MB"}
    day_dir = _cr_upload_dir()
    fname = f"{int(time.time()*1000)}{ext}"
    fpath = day_dir / fname
    fpath.write_bytes(content)
    url = f"/cr-uploads/{date.today().isoformat()}/{fname}"
    return {"url": url, "type": file.content_type, "name": file.filename}


# ══════════════════════════════════════════════════
#  房间 CRUD
# ══════════════════════════════════════════════════

@router.get("/rooms")
async def list_rooms():
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT r.*, "
            "(SELECT COUNT(*) FROM chatroom_messages m WHERE m.room_id = r.id) AS message_count "
            "FROM chatroom_rooms r ORDER BY r.updated_at DESC"
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


@router.post("/rooms")
async def create_room(body: RoomCreate):
    now = time.time()
    room_id = f"cr_{int(now * 1000)}"

    async with get_db() as db:
        await db.execute(
            "INSERT INTO chatroom_rooms (id, title, type, aion_persona, connor_persona, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (room_id, body.title, body.type, body.aion_persona, body.connor_persona, now, now),
        )
        await db.commit()

    room = {
        "id": room_id, "title": body.title, "type": body.type,
        "aion_persona": body.aion_persona, "connor_persona": body.connor_persona,
        "context_minutes": 30, "ai_chat_rounds": 3,
        "created_at": now, "updated_at": now, "message_count": 0,
    }
    await manager.broadcast({"type": "chatroom_room_created", "data": room})
    return room


@router.put("/rooms/{room_id}")
async def update_room(room_id: str, body: RoomUpdate):
    async with get_db() as db:
        sets, vals = [], []
        for field in ["title", "aion_persona", "connor_persona", "context_minutes", "ai_chat_rounds"]:
            v = getattr(body, field, None)
            if v is not None:
                sets.append(f"{field}=?")
                vals.append(v)
        if sets:
            sets.append("updated_at=?")
            vals.append(time.time())
            vals.append(room_id)
            await db.execute(f"UPDATE chatroom_rooms SET {', '.join(sets)} WHERE id=?", vals)
            await db.commit()
    await manager.broadcast({"type": "chatroom_room_updated", "data": {"id": room_id, **body.dict(exclude_none=True)}})
    return {"ok": True}


@router.delete("/rooms/{room_id}")
async def delete_room(room_id: str):
    async with get_db() as db:
        await db.execute("PRAGMA foreign_keys = ON")
        await db.execute("DELETE FROM chatroom_rooms WHERE id=?", (room_id,))
        # 清理锚点（记忆跨房间共享，不随房间删除）
        await db.execute("DELETE FROM chatroom_digest_anchors WHERE room_id=?", (room_id,))
        await db.commit()
    await manager.broadcast({"type": "chatroom_room_deleted", "data": {"id": room_id}})
    return {"ok": True}


@router.get("/rooms/{room_id}")
async def get_room(room_id: str):
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM chatroom_rooms WHERE id=?", (room_id,))
        row = await cur.fetchone()
        if not row:
            return {"error": "房间不存在"}
        return dict(row)


# ══════════════════════════════════════════════════
#  消息
# ══════════════════════════════════════════════════

@router.get("/rooms/{room_id}/messages")
async def list_messages(room_id: str, limit: int = Query(50, ge=1, le=500), before: Optional[float] = Query(None)):
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        if before:
            cur = await db.execute(
                "SELECT * FROM chatroom_messages WHERE room_id=? AND created_at<? ORDER BY created_at DESC LIMIT ?",
                (room_id, before, limit),
            )
        else:
            cur = await db.execute(
                "SELECT * FROM chatroom_messages WHERE room_id=? ORDER BY created_at DESC LIMIT ?",
                (room_id, limit),
            )
        rows = await cur.fetchall()
        result = []
        for r in reversed(rows):
            d = dict(r)
            d["attachments"] = json.loads(d.get("attachments") or "[]") if d.get("attachments") else []
            result.append(d)
        return result


@router.delete("/messages/{msg_id}")
async def delete_message(msg_id: str):
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT room_id FROM chatroom_messages WHERE id=?", (msg_id,))
        row = await cur.fetchone()
        if row:
            await db.execute("DELETE FROM chatroom_messages WHERE id=?", (msg_id,))
            await db.commit()
            await manager.broadcast({"type": "chatroom_msg_deleted", "data": {"id": msg_id, "room_id": row["room_id"]}})
    return {"ok": True}


# ══════════════════════════════════════════════════
#  发送消息 + AI 回复 (SSE)
# ══════════════════════════════════════════════════

async def _save_msg(room_id: str, sender: str, content: str, msg_id: str = None, attachments: list = None) -> dict:
    """保存消息到数据库"""
    now = time.time()
    if not msg_id:
        msg_id = f"cm_{int(now * 1000)}_{sender[:1]}"
    att_list = attachments or []
    att_json = json.dumps(att_list, ensure_ascii=False) if att_list else "[]"
    async with get_db() as db:
        await db.execute(
            "INSERT INTO chatroom_messages (id, room_id, sender, content, attachments, created_at) VALUES (?,?,?,?,?,?)",
            (msg_id, room_id, sender, content, att_json, now),
        )
        await db.execute("UPDATE chatroom_rooms SET updated_at=? WHERE id=?", (now, room_id))
        await db.commit()
    msg = {"id": msg_id, "room_id": room_id, "sender": sender, "content": content,
           "created_at": now, "attachments": att_list}
    await manager.broadcast({"type": "chatroom_msg_created", "data": msg})

    # Connor 相关消息产生时重置自动总结计时器（私聊和群聊都触发）
    async with get_db() as _db:
        _db.row_factory = aiosqlite.Row
        _cur = await _db.execute("SELECT type FROM chatroom_rooms WHERE id=?", (room_id,))
        _room = await _cur.fetchone()
        if _room and _room["type"] in ("connor_1v1", "group"):
            connor_1v1_on_message()

    return msg


async def _load_room_and_messages(room_id: str, limit: int = 50) -> tuple[dict, list[dict]]:
    """加载房间信息和最近消息"""
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM chatroom_rooms WHERE id=?", (room_id,))
        room = await cur.fetchone()
        if not room:
            return None, []
        room = dict(room)

        cur = await db.execute(
            "SELECT * FROM chatroom_messages WHERE room_id=? ORDER BY created_at DESC LIMIT ?",
            (room_id, limit),
        )
        rows = await cur.fetchall()
        msgs = []
        for r in reversed(rows):
            d = dict(r)
            d["attachments"] = json.loads(d.get("attachments") or "[]") if d.get("attachments") else []
            msgs.append(d)
    return room, msgs


@router.post("/rooms/{room_id}/send")
async def send_message(room_id: str, body: MsgSend):
    """用户发消息，触发 AI 回复"""

    # 保存用户消息
    user_msg = await _save_msg(room_id, "user", body.content, attachments=body.attachments)

    # 加载房间信息
    room, msgs = await _load_room_and_messages(room_id)
    if not room:
        return {"error": "房间不存在"}

    room_type = room["type"]
    model_key = body.model
    context_minutes = room.get("context_minutes", 30)

    # TTS 参数
    tts_enabled = body.tts_enabled
    tts_aion_voice = body.tts_aion_voice
    tts_connor_voice = body.tts_connor_voice

    _q: asyncio.Queue = asyncio.Queue()

    async def _bg_generate():
        try:
            if room_type == "connor_1v1":
                # Connor 单聊：只请求 Connor
                await _generate_connor_reply(room_id, room, msgs, _q, context_minutes,
                                             tts_enabled=tts_enabled, tts_connor_voice=tts_connor_voice)
            else:
                # 群聊：Aion 和 Connor 都回复
                await _generate_group_replies(room_id, room, msgs, model_key, _q, context_minutes,
                                              tts_enabled=tts_enabled, tts_aion_voice=tts_aion_voice, tts_connor_voice=tts_connor_voice)
        except Exception as e:
            import traceback
            traceback.print_exc()
            await _q.put({"type": "error", "content": str(e)})
        finally:
            await _q.put({"type": "done"})

    asyncio.create_task(_bg_generate())

    async def generate():
        while True:
            data = await _q.get()
            if data.get("type") == "done":
                break
            yield f"data: {json.dumps(data, ensure_ascii=False)}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")


async def _generate_connor_reply(room_id, room, msgs, _q, context_minutes, *, tts_enabled=False, tts_connor_voice=""):
    """Connor 单聊回复（Codex CLI 流式调用）"""
    connor_persona = room.get("connor_persona", "")
    query_text = msgs[-1]["content"] if msgs else ""

    connor_messages = await build_connor_1v1_context(
        room_id, msgs, connor_persona,
        query_text=query_text,
    )

    connor_msg_id = f"cm_{int(time.time() * 1000)}_c"
    await _q.put({"type": "connor_start", "id": connor_msg_id})

    full_text = ""
    has_reply = False
    try:
        async for chunk in stream_connor_cli(messages=connor_messages):
            if chunk.startswith(CLI_STATUS_PREFIX):
                await _q.put({"type": "connor_status", "text": chunk[len(CLI_STATUS_PREFIX):]})
                continue
            has_reply = True
            full_text += chunk
            await _q.put({"type": "connor_chunk", "content": chunk})
    except Exception as e:
        full_text += f"\n[Connor 回复出错: {e}]"
        await _q.put({"type": "connor_chunk", "content": f"\n[回复出错: {e}]"})

    full_text = full_text.strip()
    if not full_text:
        full_text = "Connor 暂时无法回复，请稍后再试。"

    reply = _rewrite_connor_paths(full_text)
    saved_imgs = await _extract_and_save_images(reply)
    msg = await _save_msg(room_id, "connor", reply, connor_msg_id, attachments=saved_imgs)
    await _q.put({"type": "connor_done", "message": msg})

    if tts_enabled and tts_connor_voice:
        tts = TTSStreamer(msg["id"], tts_connor_voice, sse_queue=_q)
        tts.feed(reply)
        await tts.flush()


async def _generate_group_replies(room_id, room, msgs, model_key, _q, context_minutes, *, tts_enabled=False, tts_aion_voice="", tts_connor_voice=""):
    """群聊回复：顺序执行，第二个 AI 能看到第一个的回复和工具执行结果"""
    aion_persona = room.get("aion_persona", "")
    connor_persona = room.get("connor_persona", "")
    query_text = msgs[-1]["content"] if msgs else ""

    aion_first = random.choice([True, False])

    if aion_first:
        digest = await _reply_aion(room_id, msgs, aion_persona, context_minutes, query_text, model_key, _q,
                                   tts_enabled=tts_enabled, tts_voice=tts_aion_voice)
        _, updated_msgs = await _load_room_and_messages(room_id)
        await _reply_connor(room_id, updated_msgs, connor_persona, context_minutes, query_text, _q,
                            tts_enabled=tts_enabled, tts_voice=tts_connor_voice, digest_result=digest)
    else:
        digest = await _reply_connor(room_id, msgs, connor_persona, context_minutes, query_text, _q,
                                     tts_enabled=tts_enabled, tts_voice=tts_connor_voice)
        _, updated_msgs = await _load_room_and_messages(room_id)
        await _reply_aion(room_id, updated_msgs, aion_persona, context_minutes, query_text, model_key, _q,
                          tts_enabled=tts_enabled, tts_voice=tts_aion_voice, digest_result=digest)


async def _reply_aion(room_id, msgs, aion_persona, context_minutes, query_text, model_key, _q, *, tts_enabled=False, tts_voice="", digest_result=None):
    aion_history, digest_out = await build_aion_group_context(
        room_id, msgs, aion_persona, context_minutes, query_text,
        digest_result=digest_result,
    )
    aion_msg_id = f"cm_{int(time.time() * 1000)}_a"
    await _q.put({"type": "aion_start", "id": aion_msg_id})

    full_text = ""
    try:
        async for chunk in stream_ai(aion_history, model_key, {}):
            if chunk.startswith(CLI_STATUS_PREFIX):
                await _q.put({"type": "aion_status", "text": chunk[len(CLI_STATUS_PREFIX):]})
                continue
            full_text += chunk
            await _q.put({"type": "aion_chunk", "content": chunk})
    except Exception as e:
        full_text += f"\n[Aion 回复出错: {e}]"
        await _q.put({"type": "aion_chunk", "content": f"\n[回复出错: {e}]"})

    # 工具指令处理（从文本中剥离并执行）
    clean_text, triggered = await _process_chatroom_commands(full_text, room_id, "Aion", aion_msg_id, _q)

    # TTS 用干净文本
    if tts_enabled and tts_voice and clean_text:
        tts = TTSStreamer(aion_msg_id, tts_voice, sse_queue=_q)
        tts.feed(clean_text)
        await tts.flush()

    # 保存干净文本
    saved_imgs = await _extract_and_save_images(clean_text)
    aion_msg = await _save_msg(room_id, "aion", clean_text, aion_msg_id, attachments=saved_imgs)
    await _q.put({"type": "aion_done", "message": aion_msg})

    # 触发后续动作（异步，不阻塞后续 AI 回复）
    _fire_chatroom_followups(triggered, room_id, "aion", model_key)

    return digest_out


async def _reply_connor(room_id, msgs, connor_persona, context_minutes, query_text, _q, *, tts_enabled=False, tts_voice="", digest_result=None):
    connor_history, digest_out = await build_connor_group_context(
        room_id, msgs, connor_persona, context_minutes, query_text,
        digest_result=digest_result,
    )
    connor_msg_id = f"cm_{int(time.time() * 1000)}_c"
    await _q.put({"type": "connor_start", "id": connor_msg_id})

    # Connor 使用 Codex CLI，直接传 messages（保留附件），由 _build_cli_prompt 处理
    full_text = ""
    try:
        async for chunk in stream_connor_cli(messages=connor_history):
            if chunk.startswith(CLI_STATUS_PREFIX):
                await _q.put({"type": "connor_status", "text": chunk[len(CLI_STATUS_PREFIX):]})
                continue
            full_text += chunk
            await _q.put({"type": "connor_chunk", "content": chunk})
    except Exception as e:
        full_text += f"\n[Connor 回复出错: {e}]"
        await _q.put({"type": "connor_chunk", "content": f"\n[回复出错: {e}]"})

    full_text = full_text.strip()
    if not full_text:
        full_text = "Connor 暂时无法回复，请稍后再试。"

    # 工具指令处理
    clean_text, triggered = await _process_chatroom_commands(full_text, room_id, "Connor", connor_msg_id, _q)

    # TTS 用干净文本
    if tts_enabled and tts_voice and clean_text:
        tts = TTSStreamer(connor_msg_id, tts_voice, sse_queue=_q)
        tts.feed(clean_text)
        await tts.flush()

    clean_text = _rewrite_connor_paths(clean_text)
    saved_imgs = await _extract_and_save_images(clean_text)
    connor_msg = await _save_msg(room_id, "connor", clean_text, connor_msg_id, attachments=saved_imgs)
    await _q.put({"type": "connor_done", "message": connor_msg})

    # 触发后续动作
    _fire_chatroom_followups(triggered, room_id, "connor", "")

    return digest_out


# ══════════════════════════════════════════════════
#  AI 互聊
# ══════════════════════════════════════════════════

@router.post("/rooms/{room_id}/ai-chat")
async def trigger_ai_chat(room_id: str, body: AiChatTrigger):
    """触发 AI 互聊（Aion 和 Connor 轮流对话）"""
    room, msgs = await _load_room_and_messages(room_id)
    if not room:
        return {"error": "房间不存在"}

    max_rounds = body.rounds or room.get("ai_chat_rounds", 3)
    model_key = body.model
    context_minutes = room.get("context_minutes", 30)
    aion_persona = room.get("aion_persona", "")
    connor_persona = room.get("connor_persona", "")
    tts_enabled = body.tts_enabled
    tts_aion_voice = body.tts_aion_voice
    tts_connor_voice = body.tts_connor_voice

    _q: asyncio.Queue = asyncio.Queue()

    async def _bg_ai_chat():
        nonlocal msgs
        try:
            digest = None
            for round_num in range(max_rounds):
                await _q.put({"type": "round_start", "round": round_num + 1, "total": max_rounds})

                query_text = msgs[-1]["content"] if msgs else ""

                # ── Aion 回复（使用完整管线） ──
                digest = await _reply_aion(
                    room_id, msgs, aion_persona, context_minutes, query_text, model_key, _q,
                    tts_enabled=tts_enabled, tts_voice=tts_aion_voice, digest_result=digest,
                )

                # 重新加载消息（包含 Aion 的回复和系统事件）
                _, msgs = await _load_room_and_messages(room_id)

                # ── Connor 回复（使用完整管线，共享 digest） ──
                digest = await _reply_connor(
                    room_id, msgs, connor_persona, context_minutes, query_text, _q,
                    tts_enabled=tts_enabled, tts_voice=tts_connor_voice, digest_result=digest,
                )

                # 重新加载消息
                _, msgs = await _load_room_and_messages(room_id)

        except Exception as e:
            import traceback
            traceback.print_exc()
            await _q.put({"type": "error", "content": str(e)})
        finally:
            await _q.put({"type": "done"})

    asyncio.create_task(_bg_ai_chat())

    async def generate():
        while True:
            data = await _q.get()
            if data.get("type") == "done":
                break
            yield f"data: {json.dumps(data, ensure_ascii=False)}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")


# ══════════════════════════════════════════════════
#  记忆
# ══════════════════════════════════════════════════

@router.get("/rooms/{room_id}/memories")
async def list_room_memories(room_id: str):
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT id, room_id, scope, content, keywords, importance, created_at, unresolved "
            "FROM chatroom_memories ORDER BY created_at DESC",
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


@router.post("/rooms/{room_id}/digest")
async def trigger_digest(room_id: str, model: str = DEFAULT_MODEL):
    result = await digest_chatroom()
    return result


@router.post("/rooms/{room_id}/memories")
async def create_memory(room_id: str, body: MemoryCreate):
    # 确定 scope
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT type FROM chatroom_rooms WHERE id=?", (room_id,))
        room = await cur.fetchone()
    scope = "connor" if room and room["type"] == "connor_1v1" else "group"

    mem_id = await save_chatroom_memory(
        room_id=room_id,
        scope=scope,
        content=body.content,
        keywords=body.keywords,
        importance=body.importance,
    )
    return {"ok": True, "id": mem_id}


@router.put("/memories/{mem_id}")
async def update_memory(mem_id: str, body: MemoryUpdate):
    async with get_db() as db:
        sets, vals = [], []
        if body.content is not None:
            sets.append("content=?")
            vals.append(body.content)
        if body.keywords is not None:
            sets.append("keywords=?")
            vals.append(body.keywords)
        if body.importance is not None:
            sets.append("importance=?")
            vals.append(body.importance)
        if sets:
            vals.append(mem_id)
            await db.execute(f"UPDATE chatroom_memories SET {', '.join(sets)} WHERE id=?", vals)
            await db.commit()

            # 如果内容修改了，重新生成 embedding
            if body.content is not None:
                emb = await get_embedding(body.content)
                if emb:
                    from memory import _pack_embedding
                    await db.execute("UPDATE chatroom_memories SET embedding=? WHERE id=?",
                                     (_pack_embedding(emb), mem_id))
                    await db.commit()
    return {"ok": True}


@router.delete("/memories/{mem_id}")
async def delete_memory(mem_id: str):
    async with get_db() as db:
        await db.execute("DELETE FROM chatroom_memories WHERE id=?", (mem_id,))
        await db.commit()
    return {"ok": True}
