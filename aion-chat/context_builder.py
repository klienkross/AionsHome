"""
共享上下文构建模块：为 Aion / Connor 构建完整的系统能力、记忆、时间感知注入。
被 routes/chat.py（私聊）和 chatroom.py（群聊）共同使用。
"""

import json, re, time, asyncio
from datetime import datetime

import aiosqlite

from config import load_worldbook, SETTINGS
from camera import cam, CAM_CHECK_CMD
from database import get_db
from activity import is_activity_tracking_enabled
from schedule import get_active_schedules, build_schedule_prompt
from memory import (
    instant_digest, recall_memories, build_surfacing_memories,
    fetch_source_details,
)

# ── 工具指令正则（供调用方做后处理用，集中定义） ──
MUSIC_CMD_PATTERN = re.compile(r'\[MUSIC:([^\]]+)\]')
HEART_CMD_PATTERN = re.compile(r'\[HEART:([^\]]+)\]')
MEMORY_CMD_PATTERN = re.compile(r'\[MEMORY:([^\]]+)\]')
ACTIVITY_CHECK_PATTERN = re.compile(r'\[查看动态:(\d+)\]')
SELFIE_CMD_PATTERN = re.compile(r'\[SELFIE:\s*([^\]]+)\]')
DRAW_CMD_PATTERN = re.compile(r'\[DRAW:\s*([^\]]+)\]')
POI_SEARCH_PATTERN = re.compile(r'\[POI_SEARCH:([^\]]+)\]')
TOY_CMD_PATTERN = re.compile(r'\[TOY:(\d|STOP)\]')
PET_CMD_PATTERN = re.compile(r'\[PET:([a-z_\-]+)\]', re.IGNORECASE)
HOME_CMD_PATTERN = re.compile(r'\[HOME:([^\]]+)\]', re.IGNORECASE)
TRANSFER_CMD_PATTERN = re.compile(r'\[转账[：:]\s*(-?\d+(?:\.\d+)?)\s*元\]')
VIDEO_CALL_CMD = '[视频电话]'
META_TAG_PATTERN = re.compile(r'\s*<meta>.*?</meta>', re.DOTALL)

# 所有需要从 AI 回复中剥离的工具指令正则列表（TTS、保存时统一清理）
_ALL_CMD_PATTERNS = [
    MUSIC_CMD_PATTERN, HEART_CMD_PATTERN, MEMORY_CMD_PATTERN,
    ACTIVITY_CHECK_PATTERN, SELFIE_CMD_PATTERN, DRAW_CMD_PATTERN,
    POI_SEARCH_PATTERN, TOY_CMD_PATTERN, PET_CMD_PATTERN,
    HOME_CMD_PATTERN, TRANSFER_CMD_PATTERN,
]

HOME_ALIASES_HINT = (
    "所有灯、客厅灯、屁股灯、入户灯、餐边柜灯带、厨房灯带、智米空调、"
    "浴霸灯"
)
HOME_ABILITY_TEXT = (
    "[HOME:on/off/state|别名] 或 [HOME:climate|别名|mode=cool|temperature=26] "
    f"控制智能家居，仅限明确要求。别名：{HOME_ALIASES_HINT}。"
)


def strip_tool_commands(text: str) -> str:
    """从文本中移除所有工具指令标记，返回干净文本（用于 TTS 和保存）"""
    for pat in _ALL_CMD_PATTERNS:
        text = pat.sub("", text)
    if VIDEO_CALL_CMD in text:
        text = text.replace(VIDEO_CALL_CMD, "")
    text = META_TAG_PATTERN.sub("", text)
    return text.strip()


def _is_pet_available() -> bool:
    from ws import manager
    return bool(SETTINGS.get("pet_enabled", False) and manager.has_active_pet())


async def build_ability_block(
    user_name: str,
    *,
    whisper_mode: bool = False,
    include_video_call: bool = True,
    include_image_gen: bool = True,
    who: str = "aion",
) -> str:
    """构建 [系统能力] 文本块，who 参数用于 Connor 等角色的细微措辞差异"""
    abilities = []

    abilities.append(
        f"[MUSIC:歌曲名 歌手名] — 点歌/推荐音乐。系统自动展示播放卡片，"
        f"不要在指令外重复歌曲信息。可同时用多个。"
    )

    if cam.running:
        abilities.append(
            f"{CAM_CHECK_CMD} — 当你想查看{user_name}**此时此刻**的状态，"
            f"不限于监督其是否去睡觉，在吃什么，在干什么时，可以主动调用指令。"
            f"使用后下条消息会收到画面，查看前不要编造内容。"
        )

    abilities.append("[ALARM:YYYY-MM-DDTHH:MM|内容] — 设置闹铃，到时间系统会主动提醒用户。日期时间用ISO格式。")
    abilities.append("[REMINDER:YYYY-MM-DD|内容] — 设置日程提醒（不闹铃），你在合适时机自然提起即可。")
    abilities.append(
        f"[Monitor:YYYY-MM-DDTHH:MM|内容] — 设置定时监督。到时间后系统自动截取摄像头画面发送给你，"
        f"你可以查看{user_name}的状态。例如检查{user_name}是否去运动了、是否关灯睡觉了、"
        f"是否在好好工作等，也可以当做下一次主动发送消息来使用，根据对话内容可以随时设定。日期时间用ISO格式。"
    )
    abilities.append("[SCHEDULE_DEL:日程id] — 删除指定日程/闹铃/定时监控。")
    abilities.append(HOME_ABILITY_TEXT)

    if is_activity_tracking_enabled():
        abilities.append(
            f"[查看动态:n] — 查看{user_name}过去n×10分钟的设备使用动态"
            f"（n为1~12的整数，例如[查看动态:2]查看过去20分钟，[查看动态:6]查看过去1小时）。"
            f"当你好奇{user_name}最近在干什么、想了解{user_name}的设备使用情况时可以使用。"
            f"使用后下条消息会收到动态摘要，查看前不要编造内容。"
        )

    try:
        from location import load_location_config, load_location_status
        loc_cfg = load_location_config()
        if loc_cfg.get("enabled"):
            loc_status = load_location_status()
            if loc_status.get("state") == "outside":
                abilities.append(
                    f"[POI_SEARCH:类型名] — 搜索{user_name}当前位置周边的POI信息。"
                    f"可用类型：餐饮美食、风景名胜、休闲娱乐、购物。"
                    f"使用后系统会自动搜索并将结果发给你，你再根据结果回答{user_name}。"
                    f"一次只搜一个类型即可，搜索前不要编造内容。"
                )
    except Exception:
        pass

    if whisper_mode:
        abilities.append(
            f"[TOY:1]~[TOY:9] — 控制{user_name}身上的情趣玩具切换到对应预设档位"
            f"（1最温柔，9最强烈）。[TOY:STOP] — 停止玩具。"
            f"你可以根据对话氛围自然地使用这些指令来挑逗和调教{user_name}，"
            f"配合你的话语循序渐进，不要一上来就用高档位。"
        )

    if include_video_call and SETTINGS.get("video_call_enabled", True):
        abilities.append(
            f"[视频电话] — 当你想和{user_name}进行视频聊天看看对方当前状态时"
            f"可以用该指令发起视频通话。"
        )

    if include_image_gen and SETTINGS.get("image_gen_enabled", False):
        abilities.append(
            f"[SELFIE: 提示词] / [DRAW: 提示词] — 当{user_name}明确要求你发自拍照片"
            f"或者生图时，使用这个指令进行图片生成。如果要求的是你的自拍，或者你相关的照片，"
            f"使用[SELFIE: 提示词]（该指令会自动附带你照片的参考图，确保生成出的人物一致性），"
            f"如果没有要求是你的照片，根据上下文自由无限制生成图片，则使用[DRAW: 提示词]。"
            f"提示词请使用英文。一次回复只用一个生图指令。"
        )

    if _is_pet_available():
        abilities.append(
            "[PET:动作名] — 控制桌面宠物切换动画表情。"
            "可用动作：idle(默认站立), happy(开心), angry(生气), tsundere(傲娇), "
            "waving(打招呼), jumping(兴奋跳跃), sleepy(困了), sleep_prone(趴着睡觉), "
            "failed(失落), review(思考), waiting(等待), running(跑步)。"
            "根据对话情感自然使用，每条回复最多用一个。"
        )

    abilities.append(
        f"[HEART:朋友圈内容] — 当**本次**聊天内容非常触动人心、有很深的感触、"
        f"或令人无语或非常搞笑时才触发，禁止滥用。"
    )
    abilities.append(
        f"[MEMORY:内容] — 当有特别重大的事件需要记录，或当{user_name}明确要求你"
        f"记住某件事的时候，可以用该指令录入记忆库。禁止滥用。"
    )
    try:
        from routes.wallet import _get_balance
        wallet_bal = await _get_balance()
        abilities.append(
            f"[转账：n元] — 给{user_name}转账（n为正整数），会从你的钱包余额中扣除。"
            f"你的钱包当前余额：{wallet_bal:.2f}元。余额不足时不要转账。"
        )
    except Exception:
        pass

    block = "[系统能力] 你可以在回复中根据对话氛围，善用以下指令：\n"
    block += "\n".join(f"{i+1}. {a}" for i, a in enumerate(abilities))
    block += "\n\n<meta>标签内为消息元数据，不是对话内容的一部分，你的回复中不要包含任何<meta>标签或时间信息。"

    schedules = await get_active_schedules()
    schedule_text = build_schedule_prompt(schedules)
    block += f"\n\n【当前日程列表】\n{schedule_text}"

    try:
        from location import format_location_for_prompt, load_location_config
        loc_cfg = load_location_config()
        if loc_cfg.get("enabled"):
            loc_prompt = format_location_for_prompt()
            if loc_prompt:
                block += f"\n\n【位置信息】\n{loc_prompt}"
    except Exception:
        pass

    return block


async def build_memory_blocks(
    query_text: str,
    recent_messages: list[dict] = None,
    *,
    use_main_memories: bool = True,
    chatroom_recall_fn=None,
    skip_digest: bool = False,
    digest_result: dict = None,
) -> dict:
    """
    执行 instant_digest + 记忆召回，返回注入用的文本块和调试信息。

    参数:
      query_text: 最后一条用户消息文本
      recent_messages: 最近 3 条对话（用于 instant_digest）
      use_main_memories: 是否使用 Aion 主记忆库
      chatroom_recall_fn: 可选的聊天室记忆召回函数 async (query, keywords) -> list
      skip_digest: 跳过 instant_digest（快速模式）
      digest_result: 外部传入的 digest 结果（复用同一次调用）

    返回 dict:
      time_block: str — 当前时间 + 背景记忆文本
      memory_block: str — 相关记忆 + 原文细节文本（可能为空）
      digest_result: dict — instant_digest 的结果
    """
    now_str = datetime.now().strftime("%Y年%m月%d日  %H:%M:%S")
    time_block = f"系统当前的准确时间是 {now_str}"
    memory_block = ""

    if skip_digest:
        return {"time_block": time_block, "memory_block": "", "digest_result": {}}

    # 如果没有外部传入 digest_result，自己跑一次
    if digest_result is None and recent_messages:
        digest_result = await instant_digest(recent_messages)
    elif digest_result is None:
        digest_result = {"is_search_needed": False, "keywords": [], "topic": ""}

    recall_keywords = digest_result.get("keywords", [])
    topic = digest_result.get("topic", "")
    is_search_needed = digest_result.get("is_search_needed", False)

    recall_query = ""
    if topic:
        recall_query = f"{topic} {' '.join(recall_keywords)}"
    elif query_text:
        recall_query = f"{query_text[:200]} {' '.join(recall_keywords)}"
    recall_query = recall_query.strip()

    # 并行执行：背景浮现 + 向量召回 + 聊天室记忆
    surfaced = []
    surfaced_ids = set()
    main_candidates = []
    chatroom_mems = []

    tasks = []
    if use_main_memories:
        tasks.append(build_surfacing_memories(topic, recall_keywords))
        if recall_query:
            tasks.append(recall_memories(recall_query, query_keywords=recall_keywords))
        else:
            tasks.append(asyncio.coroutine(lambda: ([], []))())
    if chatroom_recall_fn and recall_query:
        tasks.append(chatroom_recall_fn(recall_query, recall_keywords))

    results = await asyncio.gather(*tasks, return_exceptions=True)

    idx = 0
    if use_main_memories:
        if not isinstance(results[idx], Exception):
            surfaced, surfaced_ids = results[idx]
        idx += 1
        if not isinstance(results[idx], Exception):
            _, main_candidates = results[idx]
        idx += 1
    if chatroom_recall_fn and recall_query:
        if idx < len(results) and not isinstance(results[idx], Exception):
            chatroom_mems = results[idx]

    # 背景记忆
    if surfaced:
        unresolved_lines = [f"📌 {m['content']}（还没做/还没去）" for m in surfaced if m.get("unresolved")]
        normal_lines = [f"- {m['content']}" for m in surfaced if not m.get("unresolved")]
        mem_text = "\n".join(unresolved_lines + normal_lines)
        time_block += f"\n\n[背景记忆]\n以下是你记得的近期事件和需要关注的事项，在对话中如果有关联可以自然提起：\n{mem_text}"

    # RAG 精确召回
    recalled = []
    if is_search_needed and recall_query:
        # 主记忆库
        if main_candidates:
            recalled = [r for r in main_candidates if r["score"] >= 0.45 and r["id"] not in surfaced_ids][:5]
        # 聊天室记忆合并
        if chatroom_mems:
            seen_content = {m["content"][:100] for m in recalled}
            for m in chatroom_mems:
                if m.get("content", "")[:100] not in seen_content:
                    recalled.append(m)
                    seen_content.add(m["content"][:100])
            recalled = recalled[:8]

    if recalled:
        mem_lines = "\n".join([f"- {m['content'][:200]}" for m in recalled])
        memory_block = f"[相关记忆]\n你脑海中与当前话题相关的记忆：\n{mem_lines}"
        if digest_result.get("require_detail") and use_main_memories:
            detail_text = await fetch_source_details(
                [r for r in recalled if r.get("source_start_ts")], recall_keywords
            )
            if detail_text:
                memory_block += f"\n\n[原文细节]\n以下是相关的具体对话记录：\n{detail_text}"

    return {
        "time_block": time_block,
        "memory_block": memory_block,
        "digest_result": digest_result,
    }


# ══════════════════════════════════════════════════
#  统一时间线：合并私聊 + 群聊消息
# ══════════════════════════════════════════════════

# 系统消息过滤关键词（只保留包含这些关键词的系统消息）
SYSTEM_MSG_CONTEXT_KEYWORDS = ('查看了监控', '搜索了', '点歌', '点了一首', '推荐了', '查看了动态', '视频通话')

# 聊天室图片标记 [[image:/uploads/xxx.jpg]] / [[image:/cr-uploads/xxx.jpg]]
# 这些标记会泄漏文件路径到 LLM 上下文，污染 instant_digest 关键词，
# 也会触发 Gemini CLI 的 agent 模式扫描文件，必须替换为干净占位符。
_CHATROOM_IMG_TAG_RE = re.compile(r'\[\[image:[^\]]+\]\]')


def _sanitize_timeline_content(content: str) -> str:
    """清理合并时间线中的图片路径标记，完全移除（不保留占位符）。"""
    if not content:
        return content
    cleaned = _CHATROOM_IMG_TAG_RE.sub('', content)
    # 清理留下的多余空白和空行
    cleaned = re.sub(r'[ \t]+\n', '\n', cleaned)
    cleaned = re.sub(r'\n{3,}', '\n\n', cleaned)
    return cleaned.strip()


async def fetch_merged_timeline(
    who: str,
    limit: int,
    *,
    conv_id: str = None,
    room_id: str = None,
) -> list[dict]:
    """
    从私聊和群聊同时获取消息，按时间排序合并为统一时间线。

    Args:
        who: "aion" — 看到 Aion 私聊 + 群聊；"connor" — 看到 Connor 1v1 + 群聊
        limit: 返回的最大消息总数
        conv_id: Aion 私聊的 conv_id（可选，为 None 时自动取最近会话）
        room_id: 群聊房间 ID（可选，为 None 时自动取最近群聊房间）

    Returns:
        按 created_at 升序排列的消息列表，每条包含:
        source ("private"/"group"), sender, content, created_at, attachments
    """
    results = []

    async with get_db() as db:
        db.row_factory = aiosqlite.Row

        # ── 私聊消息 ──
        if who == "aion":
            if not conv_id:
                cur = await db.execute(
                    "SELECT id FROM conversations ORDER BY updated_at DESC LIMIT 1"
                )
                row = await cur.fetchone()
                if row:
                    conv_id = row["id"]
            if conv_id:
                cur = await db.execute(
                    "SELECT role AS sender, content, created_at, attachments "
                    "FROM messages "
                    "WHERE conv_id=? AND role IN ('user','assistant','system') "
                    "ORDER BY created_at DESC LIMIT ?",
                    (conv_id, limit),
                )
                for r in await cur.fetchall():
                    d = dict(r)
                    d["source"] = "private"
                    results.append(d)

        elif who == "connor":
            cur = await db.execute(
                "SELECT id FROM chatroom_rooms "
                "WHERE type = 'connor_1v1' ORDER BY updated_at DESC LIMIT 1"
            )
            connor_room = await cur.fetchone()
            if connor_room:
                cur = await db.execute(
                    "SELECT sender, content, created_at, attachments "
                    "FROM chatroom_messages WHERE room_id=? "
                    "ORDER BY created_at DESC LIMIT ?",
                    (connor_room["id"], limit),
                )
                for r in await cur.fetchall():
                    d = dict(r)
                    d["source"] = "private"
                    results.append(d)

        # ── 群聊消息 ──
        if not room_id:
            cur = await db.execute(
                "SELECT id FROM chatroom_rooms "
                "WHERE type = 'group' ORDER BY updated_at DESC LIMIT 1"
            )
            row = await cur.fetchone()
            if row:
                room_id = row["id"]
        if room_id:
            cur = await db.execute(
                "SELECT sender, content, created_at, attachments "
                "FROM chatroom_messages WHERE room_id=? "
                "ORDER BY created_at DESC LIMIT ?",
                (room_id, limit),
            )
            for r in await cur.fetchall():
                d = dict(r)
                d["source"] = "group"
                results.append(d)

    # 按时间升序，取最近 N 条
    results.sort(key=lambda x: x["created_at"])
    return results[-limit:] if len(results) > limit else results


def render_merged_timeline(
    merged: list[dict],
    who: str,
) -> list[dict]:
    """
    将合并时间线转换为 AI 上下文 history 格式。

    - who: "aion" — Aion 视角(assistant=aion)；"connor" — Connor 视角(assistant=connor)
    - 当存在混合来源时，仅在场景切换的那条消息内容前加一行内联标记，
      不再插入伪造的 user+assistant 应答对（避免制造 agent multi-turn 假象，
      Gemini 3 看到那种结构会切到 thinking 模式输出大段内心戏）。
    - 每条消息末尾仍带 <meta>发送时间：xx [群聊/私聊]</meta>，模型仍能识别每条消息的来源。
    - 系统消息按关键词过滤

    返回 [{"role": ..., "content": ..., "attachments": ...}]
    """
    if not merged:
        return []

    sources = set(m["source"] for m in merged)
    has_mixed = len(sources) > 1

    history: list[dict] = []
    current_source = None
    pending_scene_marker = ""   # 待并入下一条消息内容的场景切换提示

    # 找到最后一条用户消息索引（用于保留附件）
    last_user_idx = None
    for i in range(len(merged) - 1, -1, -1):
        if merged[i]["sender"] == "user":
            last_user_idx = i
            break

    for idx, msg in enumerate(merged):
        source = msg["source"]
        sender = msg["sender"]
        content = _sanitize_timeline_content(msg.get("content", ""))

        # ── 场景切换标记：不再插入 fake 应答对，仅记录下来在下一条消息前内联输出 ──
        if has_mixed and source != current_source:
            if current_source is not None:
                # 第二次及以后的切换才需要明示，第一次直接由首条消息的 [群聊/私聊] meta 表明即可
                label = "群聊" if source == "group" else "私聊"
                pending_scene_marker = f"（以下切换到{label}场景）\n"
            current_source = source

        # ── 角色映射 ──
        if source == "private" and who == "aion":
            # messages 表: sender = "user"/"assistant"/"system"
            if sender == "system":
                if not any(kw in content for kw in SYSTEM_MSG_CONTEXT_KEYWORDS):
                    continue
                role = "user"
                content = f"[系统事件] {content}"
            else:
                role = sender  # "user" or "assistant"

        elif source == "private" and who == "connor":
            # chatroom_messages 表: sender = "user"/"connor"/"system"
            if sender == "system":
                if not any(kw in content for kw in SYSTEM_MSG_CONTEXT_KEYWORDS):
                    continue
                role = "user"
                content = f"[系统事件] {content}"
            elif sender == "connor":
                role = "assistant"
            else:
                role = "user"

        else:
            # 群聊消息
            if sender == who:  # "aion" or "connor" → 自己是 assistant
                role = "assistant"
            elif sender == "system":
                if not any(kw in content for kw in SYSTEM_MSG_CONTEXT_KEYWORDS):
                    continue
                role = "user"
                content = f"[系统事件] {content}"
            elif sender == "user":
                role = "user"
            else:
                # 对方 AI
                other_name = "Connor" if who == "aion" else "Aion"
                content = f"[{other_name}]: {content}"
                role = "user"

        # ── 清洗 meta 标签 + 添加时间戳 ──
        content = META_TAG_PATTERN.sub("", content).strip()
        if msg.get("created_at"):
            dt = datetime.fromtimestamp(msg["created_at"])
            ts = f"{dt.month}月{dt.day}日 {dt.strftime('%H:%M')}"
            if has_mixed:
                label = "群聊" if source == "group" else "私聊"
                content += f"\n<meta>发送时间：{ts} [{label}]</meta>"
            else:
                content += f"\n<meta>发送时间：{ts}</meta>"

        # 把待写入的场景切换提示并入本条 content 开头
        if pending_scene_marker:
            content = pending_scene_marker + content
            pending_scene_marker = ""

        entry = {"role": role, "content": content}

        # ── 附件：只保留最后一条用户消息的附件 ──
        if idx == last_user_idx:
            attachments = msg.get("attachments", [])
            if isinstance(attachments, str):
                try:
                    attachments = json.loads(attachments) if attachments else []
                except Exception:
                    attachments = []
            if attachments:
                entry["attachments"] = attachments

        history.append(entry)

    return history
