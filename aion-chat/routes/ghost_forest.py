"""
奥罗斯幽林 — API 路由
"""

import asyncio, json
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from typing import Optional

from ghost_forest import (
    create_session, load_session, save_session, list_sessions,
    delete_session, apply_choice, build_game_state_summary,
    STAT_LABELS, BONUS_POINTS,
    list_personas, get_persona, save_persona, delete_persona,
    maybe_compress_history,
)
from ai_providers import stream_ai
from config import load_settings

router = APIRouter(prefix="/api/ghost-forest", tags=["ghost-forest"])


# ── Pydantic 模型 ──────────────────────────────────
class CreateReq(BaseModel):
    title: str = ""
    model: str = "gemini-2.5-flash"
    dm_persona_id: str = ""
    player_persona_id: str = ""

class IdeaReq(BaseModel):
    idea: str

class StatsReq(BaseModel):
    stats: dict  # {"str":5, "dex":3, ...}

class ChoiceReq(BaseModel):
    chosen: str           # "A"/"B"/"C"/"D"
    custom_input: str = ""  # D 选项的自定义输入
    dice_roll: int        # 1-20

class PersonaReq(BaseModel):
    id: str = ""
    name: str
    content: str
    category: str  # "dm" | "player"


# ── 人设 CRUD ─────────────────────────────────────
@router.get("/personas")
async def api_list_personas():
    return list_personas()

@router.post("/personas")
async def api_save_persona(req: PersonaReq):
    if req.category not in ("dm", "player"):
        raise HTTPException(400, "category must be 'dm' or 'player'")
    persona = {"id": req.id, "name": req.name, "content": req.content}
    result = save_persona(req.category, persona)
    return result

@router.delete("/personas/{pid}")
async def api_delete_persona(pid: str):
    if delete_persona(pid):
        return {"ok": True}
    raise HTTPException(404)


# ── 会话 CRUD ──────────────────────────────────────
@router.get("/sessions")
async def api_list_sessions():
    return list_sessions()


@router.post("/sessions")
async def api_create_session(req: CreateReq):
    s = create_session(title=req.title, model=req.model,
                       dm_persona_id=req.dm_persona_id,
                       player_persona_id=req.player_persona_id)
    return {"id": s["id"], "title": s["title"], "status": s["status"]}


@router.get("/sessions/{sid}")
async def api_get_session(sid: str):
    s = load_session(sid)
    if not s:
        raise HTTPException(404, "session not found")
    return s


class PatchSessionReq(BaseModel):
    model: str | None = None

@router.patch("/sessions/{sid}")
async def api_patch_session(sid: str, req: PatchSessionReq):
    s = load_session(sid)
    if not s:
        raise HTTPException(404, "session not found")
    if req.model is not None:
        s["model"] = req.model
        save_session(s)
    return {"ok": True}


@router.delete("/sessions/{sid}")
async def api_delete_session(sid: str):
    if delete_session(sid):
        return {"ok": True}
    raise HTTPException(404, "session not found")


# ── 剧情大纲生成 (SSE) ────────────────────────────
@router.post("/sessions/{sid}/generate-outline")
async def api_generate_outline(sid: str, req: IdeaReq):
    session = load_session(sid)
    if not session:
        raise HTTPException(404)

    session["user_idea"] = req.idea
    save_session(session)

    # 获取人设
    dm_persona = get_persona(session.get("dm_persona_id", ""))
    player_persona = get_persona(session.get("player_persona_id", ""))

    system_prompt = """你是一位资深的TRPG剧本编剧，擅长创作黑暗奇幻、悬疑冒险风格的剧情。
用户会给你一个剧情脑洞/灵感，请你据此创作一份完整的TRPG剧情大纲。"""

    if dm_persona:
        system_prompt += f"\n\n【你的人设/风格】\n{dm_persona['content']}"

    if player_persona:
        system_prompt += f"\n\n【玩家角色设定】\n{player_persona['content']}\n请根据这个玩家角色的特点来设计适合他/她的冒险剧情。"

    system_prompt += """

你必须严格按照以下JSON格式回复（用```json代码块包裹），不要在JSON之外输出任何内容：

```json
{
  "title": "剧本标题（吸引人、有氛围感）",
  "background": "背景设定：世界观、时代、核心矛盾（2-3段，富有画面感）",
  "main_plot": "主线剧情概要：分为开端、发展、高潮、结局，简述关键节点",
  "npcs": [
    {"name": "NPC名字", "description": "性格特点和动机（1-2句）"}
  ],
  "key_items": [
    {"name": "道具名", "description": "道具说明和在剧情中的作用"}
  ],
  "branches": ["重要剧情分歧点1", "重要剧情分歧点2"],
  "atmosphere": "整体氛围基调描述（1段）"
}
```

注意：
- NPC 3-5个，道具 3-5个，分支 2-3个
- 这只是大纲，不是正式游戏文本。写得精炼有力，富有想象力
- 预计游戏轮次约20轮，请据此规划剧情节奏"""

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": f"我的剧情脑洞：\n{req.idea}"},
    ]

    queue = asyncio.Queue()

    async def _bg():
        full = ""
        try:
            async for chunk in stream_ai(messages, session["model"]):
                full += chunk
                await queue.put({"type": "chunk", "content": chunk})
        except Exception as e:
            await queue.put({"type": "error", "content": str(e)})
        else:
            session_reload = load_session(sid)
            if session_reload:
                session_reload["plot_outline"] = full
                session_reload["status"] = "outlined"
                # 解析 JSON 大纲
                parsed_outline = _parse_narrate_json(full)
                if parsed_outline:
                    session_reload["outline_data"] = parsed_outline
                    if parsed_outline.get("title"):
                        session_reload["title"] = parsed_outline["title"]
                else:
                    # fallback: 从第一行提取标题
                    first_line = full.strip().split("\n")[0].strip()
                    if first_line.startswith("#"):
                        session_reload["title"] = first_line.lstrip("# ").strip()
                save_session(session_reload)
        await queue.put({"type": "done"})

    asyncio.create_task(_bg())

    async def generate():
        while True:
            msg = await queue.get()
            yield f"data: {json.dumps(msg, ensure_ascii=False)}\n\n"
            if msg["type"] in ("done", "error"):
                break

    return StreamingResponse(generate(), media_type="text/event-stream")


# ── 开始游戏（提交属性分配） ──────────────────────
@router.post("/sessions/{sid}/start")
async def api_start_game(sid: str, req: StatsReq):
    session = load_session(sid)
    if not session:
        raise HTTPException(404)
    if session["status"] not in ("outlined", "paused"):
        raise HTTPException(400, f"cannot start from status: {session['status']}")

    # 校验点数: 基础随机15 + 额外7 = 总共22
    base_total = session["player"].get("base_stats_total", 15)
    expected_total = base_total + BONUS_POINTS
    total = sum(req.stats.values())
    if total != expected_total:
        raise HTTPException(400, f"总点数必须为 {expected_total}，当前 {total}")
    for k in ("str", "dex", "int", "cha", "lck"):
        if req.stats.get(k, 0) < 1:
            raise HTTPException(400, f"{STAT_LABELS[k]} 最少为 1")

    session["player"]["stats"] = {k: req.stats[k] for k in ("str", "dex", "int", "cha", "lck")}
    session["status"] = "playing"
    session["current_round"] = 0
    session["story"] = []
    session["ai_history"] = []
    session["inventory"] = []
    save_session(session)
    return {"ok": True, "status": "playing"}


# ── 生成当轮剧情 (SSE) ────────────────────────────
@router.post("/sessions/{sid}/narrate")
async def api_narrate(sid: str):
    session = load_session(sid)
    if not session:
        raise HTTPException(404)
    if session["status"] != "playing":
        raise HTTPException(400, "game not in playing state")

    session["current_round"] += 1
    save_session(session)

    is_first = session["current_round"] == 1
    state_summary = build_game_state_summary(session)

    system_prompt = _build_dm_system_prompt(session, is_first)

    messages = [{"role": "system", "content": system_prompt}]

    # 加入 AI 历史对话
    messages.extend(session.get("ai_history", []))

    # 当轮指令
    if is_first:
        user_msg = f"""游戏开始！这是第1轮。
请根据剧情大纲，生成开场剧情。同时请给予玩家1-3个初始道具。

{state_summary}

你必须严格按照以下JSON格式回复（用```json代码块包裹），不要输出JSON之外的任何内容：

```json
{{{{
  "narration": "开场剧情叙述（200-400字，富有画面感和沉浸感）",
  "options": [
    {{"key": "A", "text": "选项描述", "stat": "str", "dc": 12, "item_cost": null}},
    {{"key": "B", "text": "选项描述", "stat": "dex", "dc": 10, "item_cost": null}},
    {{"key": "C", "text": "选项描述", "stat": "int", "dc": 14, "item_cost": null}},
    {{"key": "D", "text": "自由行动", "stat": "lck", "dc": 0, "item_cost": null}}
  ],
  "items_gained": [{{"name": "道具名", "count": 1, "description": "描述"}}],
  "stat_changes": {{}}
}}}}
```

说明：
- stat 取值: str(力量), dex(敏捷), int(智力), cha(魅力), lck(幸运)
- dc: 通过鉴定的难度值(Difficulty Check)，范围8-18，简单=8-10，普通=11-13，困难=14-16，极难=17-18。D选项dc固定为0
- 鉴定公式: D20 + 属性值÷2 ≥ dc 则成功
- D选项固定为自由行动，stat固定为lck
- item_cost 示例: {{"name": "钥匙", "count": 1}}，无消耗填null
- items_gained 是本轮给予玩家的新道具（开局请给1-3个）
- stat_changes 是本轮对玩家属性的直接影响（开局一般为空{{}})"""
    else:
        user_msg = f"""请继续生成第{session['current_round']}轮剧情和选项。

{state_summary}

你必须严格按照以下JSON格式回复（用```json代码块包裹），不要输出JSON之外的任何内容：

```json
{{{{
  "narration": "本轮剧情叙述（200-400字）",
  "options": [
    {{"key": "A", "text": "选项描述", "stat": "str", "dc": 12, "item_cost": null}},
    {{"key": "B", "text": "选项描述", "stat": "dex", "dc": 10, "item_cost": null}},
    {{"key": "C", "text": "选项描述", "stat": "int", "dc": 14, "item_cost": null}},
    {{"key": "D", "text": "自由行动", "stat": "lck", "dc": 0, "item_cost": null}}
  ],
  "items_gained": [],
  "stat_changes": {{}}
}}}}
```

注意：如果玩家持有道具，请至少设计一个选项可以使用已有道具（通过item_cost消耗），使用道具应降低dc。"""

    messages.append({"role": "user", "content": user_msg})

    queue = asyncio.Queue()

    async def _bg():
        full = ""
        try:
            async for chunk in stream_ai(messages, session["model"]):
                full += chunk
                await queue.put({"type": "chunk", "content": chunk})
        except Exception as e:
            await queue.put({"type": "error", "content": str(e)})
        else:
            s = load_session(sid)
            if s:
                s["ai_history"].append({"role": "user", "content": user_msg})
                s["ai_history"].append({"role": "assistant", "content": full})

                # 解析 JSON 结构
                parsed = _parse_narrate_json(full)
                if parsed:
                    # 应用道具/属性变化
                    if parsed.get("items_gained"):
                        for item in parsed["items_gained"]:
                            apply_choice(s, 0, "", 0, items_gained=[item])
                    if parsed.get("stat_changes"):
                        apply_choice(s, 0, "", 0, stat_changes=parsed["stat_changes"])

                s["story"].append({
                    "round": s["current_round"],
                    "narration": parsed.get("narration", full) if parsed else full,
                    "options": parsed.get("options", []) if parsed else [],
                    "chosen": None,
                    "dice_roll": None,
                })
                save_session(s)

                # 发送解析后的结构化数据
                if parsed:
                    await queue.put({"type": "parsed", "data": parsed})
        await queue.put({"type": "done"})
        # 趁玩家阅读剧情时，后台压缩历史
        asyncio.create_task(maybe_compress_history(sid))

    asyncio.create_task(_bg())

    async def generate():
        while True:
            msg = await queue.get()
            yield f"data: {json.dumps(msg, ensure_ascii=False)}\n\n"
            if msg["type"] in ("done", "error"):
                break

    return StreamingResponse(generate(), media_type="text/event-stream")


# ── 提交选择 (SSE，AI 根据选择+骰子生成结果) ─────
@router.post("/sessions/{sid}/choose")
async def api_choose(sid: str, req: ChoiceReq):
    session = load_session(sid)
    if not session:
        raise HTTPException(404)
    if session["status"] != "playing":
        raise HTTPException(400, "game not playing")

    state_summary = build_game_state_summary(session)

    # 查找选项文本
    last_story = session["story"][-1] if session["story"] else None
    option_text = req.chosen
    if last_story and last_story.get("options"):
        for opt in last_story["options"]:
            if opt["key"] == req.chosen:
                option_text = f"{req.chosen}. {opt['text']}"
                break

    if req.chosen == "D" and req.custom_input:
        option_text = f"D（自由行动：{req.custom_input}）"

    # 找出选项对应的属性和难度值
    stat_key = "lck"
    option_dc = 0  # AI 设定的难度值
    last_story = session["story"][-1] if session["story"] else None
    if last_story and last_story.get("options"):
        for opt in last_story["options"]:
            if opt["key"] == req.chosen and opt.get("stat"):
                stat_key = opt["stat"]
                option_dc = opt.get("dc", 0)
                break

    # 属性值砍半参与鉴定
    raw_stat = session["player"]["stats"].get(stat_key, 0)
    stat_val = raw_stat // 2
    total_check = req.dice_roll + stat_val
    stat_label = STAT_LABELS.get(stat_key, stat_key)

    # 骰子结果描述
    dice_desc = _dice_description(req.dice_roll, total_check, option_dc)

    # 判定是否通过（有DC用DC，无DC用固定阈值）
    if option_dc > 0:
        check_detail = f"难度DC={option_dc}，"
        if req.dice_roll == 1:
            check_detail += "大失败（无论如何失败）"
        elif req.dice_roll == 20:
            check_detail += "完美成功（无论如何成功）"
        elif total_check >= option_dc:
            check_detail += f"通过（{total_check} ≥ {option_dc}）"
        else:
            check_detail += f"未通过（{total_check} < {option_dc}）"
    else:
        check_detail = dice_desc

    # ── 鉴定结果影响属性：成功+1，失败-1 ──
    check_passed = False
    if req.dice_roll == 1:
        check_passed = False
    elif req.dice_roll == 20:
        check_passed = True
    elif option_dc > 0:
        check_passed = total_check >= option_dc
    else:
        check_passed = total_check >= 11  # D选项(幸运)以11为分界

    stat_adj = 1 if check_passed else -1
    old_stat_val = session["player"]["stats"].get(stat_key, 0)
    new_stat_val = max(1, old_stat_val + stat_adj)  # 最低为1
    session["player"]["stats"][stat_key] = new_stat_val
    save_session(session)

    # 重新构建状态摘要（含最新属性）
    state_summary = build_game_state_summary(session)

    stat_adj_desc = f"鉴定{'成功' if check_passed else '失败'}，{stat_label} {'+'  if stat_adj > 0 else ''}{stat_adj}（{old_stat_val}→{new_stat_val}）"

    user_msg = f"""玩家选择了：{option_text}
掷骰结果：D20({req.dice_roll}) + {stat_label}({raw_stat})÷2 = D20({req.dice_roll}) + {stat_val} = {total_check}
{check_detail}
属性影响：{stat_adj_desc}

{state_summary}

请根据玩家的选择和骰子结果生成结果剧情，然后给出下一轮的选项。
你必须严格按照以下JSON格式回复（用```json代码块包裹），不要输出JSON之外的任何内容：

```json
{{{{
  "result_narration": "本次选择的结果剧情（100-200字，描述选择的后果）",
  "narration": "下一轮的剧情叙述（200-400字，承接结果继续推进）",
  "options": [
    {{"key": "A", "text": "选项描述", "stat": "str", "dc": 12, "item_cost": null}},
    {{"key": "B", "text": "选项描述", "stat": "dex", "dc": 10, "item_cost": null}},
    {{"key": "C", "text": "选项描述", "stat": "int", "dc": 14, "item_cost": null}},
    {{"key": "D", "text": "自由行动", "stat": "lck", "dc": 0, "item_cost": null}}
  ],
  "stat_changes": {{}},
  "items_gained": [],
  "items_consumed": [],
  "game_over": false,
  "game_over_reason": ""
}}}}
```

说明：
- stat 取值: str(力量), dex(敏捷), int(智力), cha(魅力), lck(幸运)
- dc: 通过鉴定的难度值(Difficulty Check)，越高越难。范围8-18，简单=8-10，普通=11-13，困难=14-16，极难=17-18。D选项dc固定为0(由幸运属性裸骰判定)
- 鉴定公式: D20 + 属性值÷2 ≥ dc 则成功，D20=1必失败，D20=20必成功
- stat_changes: 只写有变化的属性，如 {{"hp": -10, "str": 1}}。注意：鉴定属性的±1已自动处理，不要重复写
- item_cost 示例: {{"name": "钥匙", "count": 1}}，无消耗填null。如果玩家背包有道具，请至少设计一个选项可以使用道具
- game_over: 玩家HP归零或剧情结束时设为true（此时options可为空[]）
- 道具很重要！请根据玩家背包适时设计消耗道具的选项，也可以在结果中给予新道具"""

    messages = [{"role": "system", "content": _build_dm_system_prompt(session, False)}]
    messages.extend(session.get("ai_history", []))
    messages.append({"role": "user", "content": user_msg})

    queue = asyncio.Queue()

    async def _bg():
        full = ""
        try:
            async for chunk in stream_ai(messages, session["model"]):
                full += chunk
                await queue.put({"type": "chunk", "content": chunk})
        except Exception as e:
            await queue.put({"type": "error", "content": str(e)})
        else:
            s = load_session(sid)
            if s:
                s["ai_history"].append({"role": "user", "content": user_msg})
                s["ai_history"].append({"role": "assistant", "content": full})

                # 更新上一轮的选择记录
                if s["story"]:
                    s["story"][-1]["chosen"] = req.chosen
                    s["story"][-1]["dice_roll"] = req.dice_roll

                # 解析 JSON
                parsed = _parse_narrate_json(full)
                if parsed:
                    apply_choice(
                        s, len(s["story"]) - 1,
                        req.chosen, req.dice_roll,
                        parsed.get("stat_changes"),
                        parsed.get("items_gained"),
                        parsed.get("items_consumed"),
                    )
                    if parsed.get("game_over"):
                        s["status"] = "finished"

                    # 如果有下一轮剧情，追加到 story
                    if parsed.get("narration") and not parsed.get("game_over"):
                        s["current_round"] += 1
                        s["story"].append({
                            "round": s["current_round"],
                            "result_narration": parsed.get("result_narration", ""),
                            "narration": parsed["narration"],
                            "options": parsed.get("options", []),
                            "chosen": None,
                            "dice_roll": None,
                        })

                save_session(s)
                if parsed:
                    await queue.put({"type": "parsed", "data": parsed})
        await queue.put({"type": "done"})
        # 趁玩家阅读剧情时，后台压缩历史
        asyncio.create_task(maybe_compress_history(sid))

    asyncio.create_task(_bg())

    async def generate():
        while True:
            msg = await queue.get()
            yield f"data: {json.dumps(msg, ensure_ascii=False)}\n\n"
            if msg["type"] in ("done", "error"):
                break

    return StreamingResponse(generate(), media_type="text/event-stream")


# ── 暂停 / 恢复 ──────────────────────────────────
@router.post("/sessions/{sid}/pause")
async def api_pause(sid: str):
    s = load_session(sid)
    if not s:
        raise HTTPException(404)
    s["status"] = "paused"
    save_session(s)
    return {"ok": True}


@router.post("/sessions/{sid}/resume")
async def api_resume(sid: str):
    s = load_session(sid)
    if not s:
        raise HTTPException(404)
    if s["status"] != "paused":
        raise HTTPException(400, "not paused")
    s["status"] = "playing"
    save_session(s)
    return s


# ── 大结局 (SSE) ─────────────────────────────────
@router.post("/sessions/{sid}/finale")
async def api_finale(sid: str):
    session = load_session(sid)
    if not session:
        raise HTTPException(404)

    state_summary = build_game_state_summary(session)

    dm_persona = get_persona(session.get("dm_persona_id", ""))
    player_persona = get_persona(session.get("player_persona_id", ""))

    system_prompt = f"""你是「奥罗斯幽林」TRPG游戏的DM（地下城主）。
现在是这场冒险的大结局，请为整个故事画上一个完美的句号。"""

    if dm_persona:
        system_prompt += f"\n\n【你的人设/风格】\n{dm_persona['content']}"
    if player_persona:
        system_prompt += f"\n\n【玩家角色设定】\n{player_persona['content']}"

    system_prompt += f"""

【剧情大纲】
{session.get('plot_outline', '（无大纲）')}

【要求】
你需要写一篇完整的大结局章节，不需要JSON格式，直接输出纯文本叙述。
- 回顾冒险旅程中的关键时刻
- 交代所有角色和线索的最终命运
- 给予玩家一个有余韵和满足感的结局
- 篇幅400-800字，文笔优美，有画面感
- 根据玩家的HP和状态决定结局基调（壮烈/圆满/苦涩等）
- 最后以一句富有哲理或诗意的话收尾"""

    # 构建历史回顾
    story_recap = []
    for entry in session.get("story", []):
        recap = f"第{entry['round']}轮：{entry.get('narration', '')[:200]}"
        if entry.get("chosen"):
            recap += f"\n  → 玩家选择: {entry['chosen']}"
        story_recap.append(recap)

    user_msg = f"""冒险已到达终点，请为这段旅程写下大结局。

{state_summary}

【冒险回顾】
{chr(10).join(story_recap)}

请直接写出大结局的叙述文本（400-800字），不需要JSON格式，不需要选项。"""

    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(session.get("ai_history", []))
    messages.append({"role": "user", "content": user_msg})

    queue = asyncio.Queue()

    async def _bg():
        full = ""
        try:
            async for chunk in stream_ai(messages, session["model"]):
                full += chunk
                await queue.put({"type": "chunk", "content": chunk})
        except Exception as e:
            await queue.put({"type": "error", "content": str(e)})
        else:
            s = load_session(sid)
            if s:
                s["status"] = "finished"
                s["finale"] = full
                s["ai_history"].append({"role": "user", "content": user_msg})
                s["ai_history"].append({"role": "assistant", "content": full})
                save_session(s)
        await queue.put({"type": "done"})

    asyncio.create_task(_bg())

    async def generate():
        while True:
            msg = await queue.get()
            yield f"data: {json.dumps(msg, ensure_ascii=False)}\n\n"
            if msg["type"] in ("done", "error"):
                break

    return StreamingResponse(generate(), media_type="text/event-stream")


# ── 生成游戏总结 ──────────────────────────────────
@router.post("/sessions/{sid}/summary")
async def api_generate_summary(sid: str):
    session = load_session(sid)
    if not session:
        raise HTTPException(404)

    # 构建总结文本
    lines = [f"🎮 奥罗斯幽林 — {session['title']}"]
    lines.append(f"状态: {session['status']} | 轮次: {session['current_round']}/{session['max_rounds']}")
    p = session["player"]
    lines.append(f"HP: {p['hp']}/{p['max_hp']}")
    if session["inventory"]:
        inv = ", ".join(f"{i['name']}×{i['count']}" for i in session["inventory"])
        lines.append(f"道具: {inv}")

    for entry in session.get("story", []):
        lines.append(f"\n--- 第{entry['round']}轮 ---")
        narration = entry.get("narration", "")
        if len(narration) > 200:
            narration = narration[:200] + "..."
        lines.append(narration)
        if entry.get("chosen"):
            lines.append(f"选择: {entry['chosen']} | 骰子: {entry.get('dice_roll', '?')}")

    return {"summary": "\n".join(lines)}


# ── 辅助函数 ──────────────────────────────────────
def _parse_narrate_json(text: str) -> dict | None:
    """从 AI 回复中提取 JSON 结构"""
    import re
    # 尝试 ```json ... ``` 格式
    m = re.search(r'```json\s*(.*?)\s*```', text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    # 尝试直接解析整段文本为 JSON
    text_stripped = text.strip()
    if text_stripped.startswith('{'):
        try:
            return json.loads(text_stripped)
        except json.JSONDecodeError:
            pass
    return None


def _build_dm_system_prompt(session: dict, is_first: bool) -> str:
    outline = session.get("plot_outline", "（无大纲）")
    remaining = session["max_rounds"] - session["current_round"]

    # 获取人设
    dm_persona = get_persona(session.get("dm_persona_id", ""))
    player_persona = get_persona(session.get("player_persona_id", ""))

    prompt = f"""你是「奥罗斯幽林」TRPG游戏的DM（地下城主）。
你需要根据以下剧情大纲来推进游戏，但可以根据玩家的选择灵活调整。"""

    if dm_persona:
        prompt += f"\n\n【你的人设/风格】\n{dm_persona['content']}"

    if player_persona:
        prompt += f"\n\n【玩家角色设定】\n{player_persona['content']}\n请根据玩家角色的特点来称呼和互动。"

    prompt += f"""

【剧情大纲】
{outline}

【规则】
1. 你的所有回复必须是严格的JSON格式（用```json代码块包裹），不要在JSON之外输出任何内容
2. 每轮生成一段沉浸感强的剧情叙述（200-400字），然后给出4个选项
3. A/B/C 为具体的行动选择，每个标注对应的属性鉴定（str/dex/int/cha）和难度值dc
4. D 固定为自由行动，使用幸运(lck)鉴定，dc固定为0
5. dc(Difficulty Check)范围8-18：简单=8-10，普通=11-13，困难=14-16，极难=17-18。请根据选项的合理性和剧情难度设定dc，不要全部简单
6. 选项可以要求消耗道具，通过 item_cost 字段指定。如果玩家持有道具，请优先设计至少一个可以使用已有道具的选项，使用道具应降低dc或带来额外优势
7. 鉴定公式: D20 + 属性值÷2 ≥ dc 则成功，D20=1必失败，D20=20必成功
8. 每轮可以对玩家属性产生影响（增减HP、属性值）
9. 道具管理很重要！主动给予新道具，并在后续选项中设计使用/消耗道具的机会，让玩家感受到道具的价值
10. 总轮次约{session['max_rounds']}轮，当前第{session['current_round']}轮
11. 每次鉴定后，对应属性会自动+1（成功）或-1（失败），你不需要在stat_changes中重复这个变化"""

    if remaining <= 5:
        prompt += f"\n\n⚠️ 剩余约{remaining}轮，请开始收束剧情线，引导走向高潮和结局。不要草草结束，要给玩家一个有满足感的结局。"

    if remaining <= 0:
        prompt += "\n\n🔚 已达到最大轮次，这应该是最后一轮，请给出结局。"

    return prompt


def _dice_description(roll: int, total: int = None, dc: int = 0) -> str:
    if total is None:
        total = roll
    if roll == 1:
        return "💀 大失败！"
    elif roll == 20:
        return "✨ 完美！"
    elif dc > 0:
        # 有DC，根据DC判定
        diff = total - dc
        if diff >= 5:
            return "🎯 大成功！"
        elif diff >= 0:
            return "😊 通过"
        elif diff >= -3:
            return "😐 勉强失败"
        else:
            return "😰 失败"
    else:
        # D选项（幸运裸骰）
        if total <= 7:
            return "😰 失败"
        elif total <= 13:
            return "😐 勉强"
        elif total <= 19:
            return "😊 成功"
        else:
            return "🎯 大成功！"
