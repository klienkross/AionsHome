"""
Memory V2 Digest Engine: 原子卡片拆分 + 情绪评价 + 对话强度
"""

import asyncio
import json
import time
from datetime import datetime


def _parse_atomic_cards(raw) -> list[dict]:
    """Parse AI output: extract JSON array, with or without markdown code blocks."""
    if not raw:
        return []
    if isinstance(raw, list):
        items = raw
    else:
        import re as _re
        text = raw.strip()
        # 剥掉 ```json ... ``` 代码块包裹
        text = _re.sub(r'```(?:json)?\s*', '', text).strip()
        # 尝试提取 [ ... ] 区间（处理 AI 在 JSON 前后附加文字的情况）
        start = text.find("[")
        end = text.rfind("]") + 1
        if start >= 0 and end > start:
            text = text[start:end]
        try:
            items = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            try:
                from json_repair import repair_json
                items = json.loads(repair_json(text))
                print(f"[digest_v2] JSON repaired successfully")
            except Exception:
                print(f"[digest_v2] JSON parse failed, raw[:200]: {str(raw)[:200]}")
                return []
    if not isinstance(items, list):
        return []
    valid = []
    for item in items:
        if isinstance(item, dict) and item.get("content", "").strip():
            valid.append({
                "content": item["content"].strip(),
                "type": item.get("type", "event"),
                "keywords": item.get("keywords", []),
                "importance": float(item.get("importance", 0.5)),
                "unresolved": 1 if item.get("unresolved", False) else 0,
                "valence": float(item.get("valence", 0.0)),
                "arousal": float(item.get("arousal", 0.0)),
                "source": item.get("source", "both"),
            })
    return valid


def _parse_emotion_output(raw: str, card_contents: list[str]) -> list[dict]:
    """Parse Agent B output: emotion evaluations per card."""
    if isinstance(raw, list):
        items = raw
    else:
        try:
            items = json.loads(raw.strip())
        except (json.JSONDecodeError, ValueError):
            try:
                from json_repair import repair_json
                items = json.loads(repair_json(raw.strip()))
            except Exception:
                return [{"valence": 0.0, "arousal": 0.0} for _ in card_contents]
    if not isinstance(items, list):
        return [{"valence": 0.0, "arousal": 0.0} for _ in card_contents]
    result = []
    for i, content in enumerate(card_contents):
        if i < len(items) and isinstance(items[i], dict):
            result.append({
                "valence": max(-1.0, min(1.0, float(items[i].get("valence", 0.0)))),
                "arousal": max(-1.0, min(1.0, float(items[i].get("arousal", 0.0)))),
            })
        else:
            result.append({"valence": 0.0, "arousal": 0.0})
    return result


def compute_intensity(msgs: list[dict]) -> float:
    """Compute conversation intensity score from message timestamps and lengths."""
    if len(msgs) < 2:
        return 0.0
    intervals = []
    for i in range(1, len(msgs)):
        gap = msgs[i]["created_at"] - msgs[i - 1]["created_at"]
        if gap > 0:
            intervals.append(gap)
    avg_interval = sum(intervals) / len(intervals) if intervals else 300.0
    avg_chars = sum(len(m.get("content", "")) for m in msgs) / len(msgs)
    turn_count = len(msgs)

    speed_score = 1.0 - min(avg_interval / 300.0, 1.0)
    length_score = min(avg_chars / 200.0, 1.0)
    density_score = min(turn_count / 20.0, 1.0)
    return round(speed_score * 0.4 + length_score * 0.35 + density_score * 0.25, 4)


def _parse_lifecycle_judgment(raw) -> dict:
    """Parse the lifecycle AI judgment output."""
    default = {"should_close": False, "confidence": 0.0, "relation": "related"}
    if isinstance(raw, dict):
        obj = raw
    elif isinstance(raw, str):
        try:
            obj = json.loads(raw.strip())
        except (json.JSONDecodeError, ValueError):
            return default
    else:
        return default
    if not isinstance(obj, dict):
        return default
    return {
        "should_close": bool(obj.get("should_close", False)),
        "confidence": float(obj.get("confidence", 0.0)),
        "relation": obj.get("relation", "related"),
    }


def _build_lifecycle_prompt(new_content: str, candidate_content: str) -> str:
    """Build prompt for lifecycle judgment: should the old card be closed?"""
    return (
        f"判断新事件是否意味着旧事件已完结。\n\n"
        f"旧卡片：{candidate_content}\n"
        f"新卡片：{new_content}\n\n"
        f"输出 JSON：\n"
        f'- "should_close": true/false（新事件是否表示旧事件已完结/实现/取消）\n'
        f'- "confidence": 0.0-1.0（你的确信度）\n'
        f'- "relation": "follow_up"（后续进展）或 "related"（仅相关）\n\n'
        f"严格只输出 JSON 对象。"
    )


def _build_agent_a_prompt(messages_text: str, user_name: str, ai_name: str, persona_block: str) -> str:
    return (
        f"{persona_block}"
        f"你是一个记忆拆分专家。请将下面的对话拆分成独立的原子记忆卡片，每张卡片只记录一件事。\n\n"
        f"规则：\n"
        f"- 每张卡片的 content 应是一个完整的陈述句，包含日期和必要上下文\n"
        f"- 使用 \"{user_name}\" 和 \"{ai_name}\" 指代双方\n"
        f"- type 必须是以下之一：event, preference, emotion, promise, plan, fact\n"
        f"- keywords: 3-6 个关键词，分两层：\n"
        f"  · 领域词（1-2个）：这件事属于什么大类。例：阅读、技术开发、日常起居、社交、情绪、创作、游戏\n"
        f"  · 实体词（2-4个）：具体的人事物地名。例：中亚史、阿里云、提拉米苏、披风鸟人\n"
        f"  领域词放前面，实体词放后面。每个关键词必须是数组中独立的字符串。\n"
        f"  【严禁】人名（{user_name}, {ai_name} 等）、泛指词（提醒、建议、完成、计划、测试、观察、休息、未完成、担忧、偏好）\n"
        f"  示例：\"阅读中亚史时对战车提出疑问\" → [\"阅读\", \"中亚史\", \"战车\", \"骑兵\"]\n"
        f"  示例：\"提醒喝咖啡不要太快\" → [\"日常起居\", \"喝咖啡\", \"胃\"]\n"
        f"  示例：\"表达想养蜘蛛的冲动\" → [\"日常起居\", \"养蜘蛛\", \"冲动\"]\n"
        f"- importance: 0.0-1.0，评分严厉（默认 0.3，只有重大事实才给 0.8+）\n"
        f"- unresolved: 未完成的计划/承诺为 true，已发生事实为 false\n\n"
        f"输出一个 JSON 数组，每个元素格式：\n"
        f'{{"content": "...", "type": "...", "keywords": [...], "importance": 0.X, "unresolved": false}}\n\n'
        f"严格只输出 JSON 数组，不要其他内容。\n\n"
        f"【对话记录】：\n{messages_text}"
    )


def _build_agent_b_prompt(card_contents: list[str], messages_text: str) -> str:
    cards_list = "\n".join(f"{i+1}. {c}" for i, c in enumerate(card_contents))
    return (
        f"请对以下每条记忆卡片评估情绪维度。\n\n"
        f"卡片列表：\n{cards_list}\n\n"
        f"原始对话供参考：\n{messages_text[:2000]}\n\n"
        f"对每张卡片输出 valence(-1.0~1.0, 正=正面情绪, 负=负面) 和 arousal(-1.0~1.0, 正=高能量, 负=低能量)。\n"
        f"输出 JSON 数组，每个元素：{{\"valence\": X, \"arousal\": Y}}\n"
        f"顺序与卡片列表一一对应。严格只输出 JSON 数组。"
    )


def _build_unified_prompt(messages_text: str, user_name: str, ai_name: str, persona_block: str) -> str:
    return (
        f"{persona_block}"
        f"你是一个记忆整理专家。请将下面的对话整理成独立的记忆卡片。\n\n"
        f"【整理规则】\n"
        f"1. 每张卡片记录一件独立的事实/事件/情感/计划\n"
        f"2. content 是完整陈述句，包含日期和上下文，不少于30字\n"
        f"3. 同一主题的零散信息合并为一个卡片，不要过度碎片化\n"
        f"4. 每组对话生成 2~8 张卡片\n"
        f"5. 使用 \"{user_name}\" 和 \"{ai_name}\" 指代双方\n"
        f"6. 去除口水话、打招呼、重复信息、无实质内容的寒暄\n"
        f"7. 如果 {ai_name} 提到过去的事但 {user_name} 没有确认或补充，不要提取为卡片（可能是 AI 编造的）\n"
        f"8. 如果 {ai_name} 回忆/复述已知的旧事件且没有新信息，不要提取\n\n"
        f"【字段说明】\n"
        f"- type: event/preference/emotion/promise/plan/fact\n"
        f"- keywords: 3~6个，分两层：\n"
        f"  · 领域词（1-2个）：这件事属于什么大类。例：阅读、技术开发、日常起居、社交、情绪、创作、游戏\n"
        f"  · 实体词（2-4个）：具体的人事物地名。例：中亚史、阿里云、提拉米苏\n"
        f"  领域词放前面，实体词放后面。每个关键词是数组中独立的字符串。\n"
        f"  【严禁】人名（{user_name}, {ai_name} 等）、泛指词（提醒、建议、完成、计划、测试、观察、休息、未完成、担忧、偏好）\n"
        f"  示例：\"阅读中亚史时对战车提出疑问\" → [\"阅读\", \"中亚史\", \"战车\", \"骑兵\"]\n"
        f"  示例：\"提醒喝咖啡不要太快\" → [\"日常起居\", \"喝咖啡\", \"胃\"]\n"
        f"  示例：\"表达想养蜘蛛的冲动\" → [\"日常起居\", \"养蜘蛛\", \"冲动\"]\n"
        f"- importance: 0.0~1.0，默认0.3，重大事实才给0.7+\n"
        f"- unresolved: 未完成的计划/承诺为true\n"
        f"- valence: -1.0~1.0（正=正面情绪，负=负面）\n"
        f"- arousal: -1.0~1.0（正=高能量，负=低能量）\n"
        f"- source: 信息主要来自谁\n"
        f"  \"user\" = {user_name}亲口说的/做的\n"
        f"  \"ai\" = {ai_name}单方面声称或推测的\n"
        f"  \"both\" = 双方共同参与确认的\n\n"
        f"【输出格式】JSON 数组，每个元素：\n"
        f'{{"content":"...","type":"...","keywords":[...],"importance":0.X,"unresolved":false,'
        f'"valence":0.X,"arousal":0.X,"source":"user|ai|both"}}\n\n'
        f"严格只输出 JSON 数组。\n\n"
        f"【对话记录】\n{messages_text}"
    )


def _keyword_substr_overlap(kws_a: list[str], kws_b: list[str]) -> int:
    """子串匹配：a 中的词包含在 b 的某个词中，或反过来"""
    count = 0
    for ka in kws_a:
        if not ka:
            continue
        for kb in kws_b:
            if not kb:
                continue
            if ka in kb or kb in ka:
                count += 1
                break
    return count


async def _find_matching_open_cards(new_card_content: str, new_card_embedding: list[float],
                                     auto_threshold: float, ask_threshold: float,
                                     new_keywords: list[str] = None,
                                     new_card_type: str = None) -> list[dict]:
    import aiosqlite
    from database import get_db
    from sentinel import _unpack_embedding
    from memory import cosine_similarity

    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT id, content, type, status, embedding, keywords FROM memory_cards "
            "WHERE status='open' AND embedding IS NOT NULL"
        )
        rows = await cur.fetchall()
    matches = []
    for row in rows:
        # 关键词子串匹配（同类型 + 重叠 ≥2 → 自动匹配）
        kw_matched = False
        if new_keywords and row["keywords"]:
            try:
                row_kws = json.loads(row["keywords"]) if isinstance(row["keywords"], str) else row["keywords"]
            except (json.JSONDecodeError, ValueError):
                row_kws = []
            if row_kws:
                overlap = _keyword_substr_overlap(new_keywords, row_kws)
                same_type = (new_card_type == row["type"]) if new_card_type else False
                if overlap >= 2 and same_type:
                    kw_matched = True

        # 向量相似度
        vec_sim = 0.0
        if new_card_embedding:
            mem_vec = _unpack_embedding(row["embedding"])
            vec_sim = cosine_similarity(new_card_embedding, mem_vec)

        if kw_matched or vec_sim >= ask_threshold:
            matches.append({
                "id": row["id"], "content": row["content"],
                "type": row["type"], "similarity": round(vec_sim, 4),
                "auto": kw_matched or vec_sim >= auto_threshold,
                "kw_matched": kw_matched,
            })
    matches.sort(key=lambda x: x["similarity"], reverse=True)
    return matches


async def _dedup_against_realtime(card_content: str, card_embedding: list[float],
                                   source_conv: str, threshold: float = 0.85) -> str | None:
    """全局近 7 天去重（替代原来的同 conversation 去重）"""
    import embedding_cache
    from database import get_db
    import time as _time

    if not card_embedding or not embedding_cache.is_loaded():
        return None

    cutoff = _time.time() - 7 * 86400
    all_sims = embedding_cache.batch_cosine(card_embedding)

    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT id FROM memory_cards WHERE created_at > ? AND status IN ('open','closed')",
            (cutoff,),
        )
        recent_ids = {row["id"] for row in await cur.fetchall()}

    for cid, sim in all_sims:
        if cid in recent_ids and sim >= threshold:
            return cid

    return None


async def _verify_ai_claims(card: dict, source_start_ts: float) -> bool:
    """对 source='ai' 的卡片做事实核查。返回 True=保留, False=丢弃。

    策略：
    - emotion：AI 自己的情绪直接放行，声称「你感到XX」则核查
    - kw ≥ 2 命中 → 直接放行
    - 否则 → sentinel 判断
    """
    if card.get("source") != "ai":
        return True

    card_type = card.get("type", "event")
    keywords = card.get("keywords", [])
    content = card.get("content", "")

    if not keywords:
        return True

    # emotion 类型：区分 AI 自己 vs 声称用户
    if card_type == "emotion":
        from config import load_worldbook
        wb = load_worldbook()
        user_name = wb.get("user_name", "用户")
        # 内容中提到用户 → AI 在声称用户的情绪，需要核查
        if user_name in content:
            print(f"[digest_v2] emotion about user, verifying: {content[:50]}")
        else:
            print(f"[digest_v2] ✓ emotion (AI self): {content[:50]}")
            return True

    import aiosqlite
    from database import get_db

    # 在 messages 中搜索关键词（往前 14 天）
    cutoff = source_start_ts - 14 * 86400
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT content FROM messages WHERE role='user' AND created_at > ? AND created_at < ?",
            (cutoff, source_start_ts),
        )
        user_msgs = [row["content"] for row in await cur.fetchall()]

    # 关键词匹配：至少 2 个实体词
    kw_hits = 0
    for kw in keywords:
        if any(kw in msg for msg in user_msgs):
            kw_hits += 1
    if kw_hits >= 2:
        print(f"[digest_v2] ✓ verify pass ({card_type}): {kw_hits} kw hit(s)")
        return True

    # kw < 2 → sentinel 判断
    from sentinel import call_sentinel
    context_sample = "\n".join(user_msgs[-20:])[:2000] if user_msgs else "(无历史消息)"
    prompt = (
        f"判断这条记忆描述的事件在历史对话中是否有用户亲自提到过。\n"
        f"注意：如果是 AI 自己单方面推测、总结、或编造的内容，应该判定为无依据。\n\n"
        f"记忆内容：{content}\n\n"
        f"历史用户消息（最近20条）：\n{context_sample}\n\n"
        f"输出 JSON：{{\"has_evidence\": true/false, \"reason\": \"简短理由\"}}"
    )
    try:
        result = await call_sentinel(prompt)
        if isinstance(result, dict):
            has = result.get("has_evidence", True)
            if not has:
                print(f"[digest_v2] ✗ sentinel reject: {content[:60]}")
            return has
    except Exception as e:
        print(f"[digest_v2] verify sentinel error: {e}")

    return True  # 核查失败时保守保留


async def _digest_group_to_cards(
    messages_text: str,
    user_name: str,
    ai_name: str,
    persona_block: str,
    call_fn,  # async (prompt: str) -> str | None
) -> list[dict]:
    """通用：给定格式化消息文本，调用 AI 生成原子卡片列表。供 _do_digest_v2 和 digest_chatroom 复用。"""
    prompt = _build_unified_prompt(messages_text, user_name, ai_name, persona_block)
    try:
        raw = await call_fn(prompt)
    except Exception as e:
        print(f"[digest] 模型调用失败: {e}")
        return []
    if not raw:
        return []
    return _parse_atomic_cards(raw)


async def _do_digest_v2(min_messages: int = 0) -> dict:
    """V2 digest: atomic card split + emotion + intensity + relationship matching."""
    import aiosqlite
    from ai_providers import simple_ai_call
    from config import load_worldbook, load_settings, load_digest_anchor, save_digest_anchor
    from database import get_db
    from ws import manager
    from sentinel import call_sentinel, get_embedding, _pack_embedding
    from memory_cards import create_card, create_link, update_card_status
    from memory import _split_into_groups_smart, _get_active_model_and_conv

    settings = load_settings()
    split_mode = "unified"
    auto_threshold = settings.get("digest_matching", {}).get("auto_threshold", 0.85)
    ask_threshold = settings.get("digest_matching", {}).get("ask_threshold", 0.65)

    anchor_ts = load_digest_anchor()

    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT id, conv_id, role, content, created_at FROM messages "
            "WHERE role IN ('user','assistant') AND created_at > ? "
            "ORDER BY created_at ASC",
            (anchor_ts,),
        )
        new_msgs = [dict(r) for r in await cur.fetchall()]

    if not new_msgs:
        print(f"[digest_v2] 触发但无新消息 (anchor={anchor_ts})")
        return {"ok": True, "message": "没有新消息需要总结", "new_cards_count": 0, "processed_messages": 0}

    if min_messages > 0 and len(new_msgs) < min_messages:
        print(f"[digest_v2] 消息不足: {len(new_msgs)} < {min_messages}, 跳过")
        return {"ok": True, "message": f"消息不足 {min_messages} 条，跳过", "new_cards_count": 0, "processed_messages": 0}

    print(f"[digest_v2] ═══ digest 启动: {len(new_msgs)} 条新消息, {min_messages=} ═══")

    wb = load_worldbook()
    user_name = wb.get("user_name", "用户")
    ai_name = wb.get("ai_name", "AI")
    ai_persona = wb.get("ai_persona", "")
    user_persona = wb.get("user_persona", "")

    model_key, conv_id = await _get_active_model_and_conv()

    persona_block = ""
    if ai_persona:
        persona_block += f"[{ai_name}的人设]\n{ai_persona}\n\n"
    if user_persona:
        persona_block += f"[{user_name}的人设]\n{user_persona}\n\n"

    groups = await _split_into_groups_smart(new_msgs, user_name, ai_name)
    total_new = 0
    all_summaries = []

    for group in groups:
        pending_closes = []
        group_start = datetime.fromtimestamp(group[0]["created_at"]).strftime("%Y年%m月%d日 %H:%M")
        group_end = datetime.fromtimestamp(group[-1]["created_at"]).strftime("%Y年%m月%d日 %H:%M")
        print(f"[digest_v2] ── 处理消息组: {group_start} ~ {group_end}, {len(group)} 条 ──")
        date_header = f"[对话时间范围: {group_start} ~ {group_end}]\n"
        messages_text = date_header + "\n".join([
            f"[{datetime.fromtimestamp(m['created_at']).strftime('%m-%d %H:%M')}] "
            f"{user_name if m['role']=='user' else ai_name}: {m['content'][:300]}"
            for m in group
        ])

        source_start_ts = group[0]["created_at"]
        source_end_ts = group[-1]["created_at"]
        source_conv_id = group[0].get("conv_id")

        if split_mode == "unified":
            async def _call_main(prompt: str) -> str | None:
                return await simple_ai_call([{"role": "user", "content": prompt}], model_key)
            print(f"[digest_v2] → 调用 unified agent...")
            atomic_cards = await _digest_group_to_cards(messages_text, user_name, ai_name, persona_block, _call_main)
            if not atomic_cards:
                print(f"[digest_v2] ✗ 解析后无有效卡片")
                save_digest_anchor(source_end_ts)
                continue
            print(f"[digest_v2] ✓ 解析出 {len(atomic_cards)} 张卡片, sources: {[ac.get('source','?') for ac in atomic_cards]}")
            emotions = []
            for ac in atomic_cards:
                emotions.append({
                    "valence": max(-1.0, min(1.0, float(ac.get("valence", 0.0)))),
                    "arousal": max(-1.0, min(1.0, float(ac.get("arousal", 0.0)))),
                })
            card_contents = [c["content"] for c in atomic_cards]
        else:
            # Separate mode: Agent A for split, Agent B for emotion
            agent_a_prompt = _build_agent_a_prompt(messages_text, user_name, ai_name, persona_block)
            try:
                raw_a = await simple_ai_call([{"role": "user", "content": agent_a_prompt}], model_key)
            except Exception as e:
                print(f"[digest_v2] Agent A failed: {e}")
                save_digest_anchor(source_end_ts)
                continue

            atomic_cards = _parse_atomic_cards(raw_a)
            if not atomic_cards:
                print(f"[digest_v2] Agent A returned no valid cards for group {group_start}")
                save_digest_anchor(source_end_ts)
                continue

            card_contents = [c["content"] for c in atomic_cards]
            agent_b_prompt = _build_agent_b_prompt(card_contents, messages_text)
            try:
                raw_b = await call_sentinel(agent_b_prompt)
                emotions = _parse_emotion_output(raw_b if isinstance(raw_b, str) else json.dumps(raw_b), card_contents)
            except Exception as e:
                print(f"[digest_v2] Agent B failed: {e}")
                emotions = [{"valence": 0.0, "arousal": 0.0} for _ in card_contents]

        # Intensity (pure math)
        intensity = compute_intensity(group)

        # ── Phase 1: 并发获取所有 embedding ──
        embed_tasks = [get_embedding(ac["content"]) for ac in atomic_cards]
        vectors = await asyncio.gather(*embed_tasks, return_exceptions=True)
        vectors = [v if not isinstance(v, Exception) else None for v in vectors]

        # ── Phase 2: 跳过向量去重（余弦相似度作为判据不可靠）──
        keep_indices = list(range(len(atomic_cards)))

        # 读取本组时间范围内的 surfaced memory ids
        surfaced_set = set()
        async with get_db() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT surfaced_memory_ids FROM messages "
                "WHERE created_at >= ? AND created_at <= ? "
                "AND surfaced_memory_ids IS NOT NULL AND surfaced_memory_ids != ''",
                (source_start_ts, source_end_ts),
            )
            for row in await cur.fetchall():
                try:
                    ids = json.loads(row["surfaced_memory_ids"])
                    surfaced_set.update(ids)
                except (json.JSONDecodeError, TypeError):
                    pass

        surf_skip = 0
        fact_skip = 0
        # ── Phase 3: 建卡 + 并发 lifecycle 判断 ──
        for i in keep_indices:
            ac = atomic_cards[i]
            vec = vectors[i]

            # surfaced 去重
            if surfaced_set and vec:
                import embedding_cache
                surfaced_sims = embedding_cache.batch_cosine_filtered(vec, surfaced_set)
                best_surfaced = max((s for _, s in surfaced_sims), default=0.0)
                if best_surfaced >= 0.85:
                    print(f"[digest_v2] ⊗ surfaced dup (sim={best_surfaced:.3f}): {ac['content'][:50]}")
                    surf_skip += 1
                    continue

            # 事实核查（仅 source=ai）
            if ac.get("source") == "ai":
                keep = await _verify_ai_claims(
                    {"content": ac["content"], "keywords": ac.get("keywords", []), "source": "ai"},
                    source_start_ts,
                )
                if not keep:
                    print(f"[digest_v2] ⊗ fact-check reject: {ac['content'][:50]}")
                    fact_skip += 1
                    continue

            card = await create_card(
                content=ac["content"],
                card_type=ac["type"],
                keywords=ac["keywords"],
                importance=ac["importance"],
                source_conv=source_conv_id,
                source_start_ts=source_start_ts,
                source_end_ts=source_end_ts,
                valence=emotions[i]["valence"] if i < len(emotions) else 0.0,
                arousal=emotions[i]["arousal"] if i < len(emotions) else 0.0,
                intensity_score=intensity,
                unresolved=ac["unresolved"],
                embed=False,
                source=ac.get("source", "both"),
            )
            if vec:
                async with get_db() as db:
                    await db.execute(
                        "UPDATE memory_cards SET embedding=? WHERE id=?",
                        (_pack_embedding(vec), card["id"]),
                    )
                    await db.commit()

            if vec or ac.get("keywords"):
                matches = await _find_matching_open_cards(
                    ac["content"], vec, auto_threshold, ask_threshold,
                    new_keywords=ac.get("keywords", []),
                    new_card_type=ac.get("type"),
                )
                auto_matches = [m for m in matches[:3] if m["id"] != card["id"] and m["auto"]]
                related_matches = [m for m in matches[:3] if m["id"] != card["id"] and not m["auto"]]

                for m in related_matches:
                    await create_link(m["id"], card["id"], "related")

                if auto_matches:
                    async def _judge_lifecycle(match, new_content):
                        prompt = _build_lifecycle_prompt(new_content, match["content"])
                        try:
                            raw = await call_sentinel(prompt)
                            return match, _parse_lifecycle_judgment(raw)
                        except Exception:
                            return match, {"should_close": False, "confidence": 0.0, "relation": "follow_up"}

                    judge_tasks = [_judge_lifecycle(m, ac["content"]) for m in auto_matches]
                    judge_results = await asyncio.gather(*judge_tasks)

                    for match, judgment in judge_results:
                        relation = judgment.get("relation", "follow_up")
                        await create_link(match["id"], card["id"], relation)
                        if judgment["should_close"] and judgment["confidence"] >= auto_threshold:
                            await update_card_status(match["id"], "closed")
                            print(f"[digest_v2] Auto-closed {match['id'][:20]} (conf={judgment['confidence']})")
                        elif judgment["should_close"] and judgment["confidence"] >= ask_threshold:
                            pending_closes.append({
                                "old_id": match["id"], "old_content": match["content"],
                                "new_content": ac["content"], "confidence": judgment["confidence"],
                            })

            await manager.broadcast({"type": "memory_added", "data": {
                "id": card["id"], "content": card["content"], "type": card["type"],
                "status": "open", "created_at": card["created_at"],
                "keywords": card["keywords"], "importance": card["importance"],
                "unresolved": card["unresolved"],
                "valence": card.get("valence", 0.0), "arousal": card.get("arousal", 0.0),
            }})
            total_new += 1
            all_summaries.append(ac["content"])

        print(f"[digest_v2] ── 本组结束: surf_dup={surf_skip} fact_rej={fact_skip}, 总新卡={total_new} ──")
        save_digest_anchor(source_end_ts)

    # Check for chains that need aggregate generation
    from memory_cards import get_follow_up_chain, should_generate_aggregate, create_aggregate_for_chain
    processed_chains = set()
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT DISTINCT from_id FROM memory_links WHERE relation='follow_up'"
        )
        chain_roots = [row["from_id"] for row in await cur.fetchall()]
    for root_id in chain_roots:
        if root_id in processed_chains:
            continue
        if await should_generate_aggregate(root_id):
            chain = await get_follow_up_chain(root_id)
            chain_contents = [c["content"] for c in chain]
            agg_prompt = (
                f"将以下事件链总结为一句话，包含时间跨度和最终状态：\n"
                + "\n".join(f"- {c}" for c in chain_contents)
                + "\n\n只输出总结文本，不要 JSON 或其他格式。"
            )
            try:
                from sentinel import call_sentinel_text
                agg_summary = await call_sentinel_text(agg_prompt)
                agg_summary = agg_summary.strip().strip('"')
            except Exception:
                agg_summary = " → ".join(chain_contents)
            agg_card = await create_aggregate_for_chain(chain, agg_summary)
            processed_chains.update(c["id"] for c in chain)
            print(f"[digest_v2] Created aggregate: {agg_summary[:60]}")

    # AI reflection + gift
    if conv_id and total_new > 0 and all_summaries:
        from ai_providers import simple_ai_call as ai_call
        try:
            async with get_db() as db:
                db.row_factory = aiosqlite.Row
                cur = await db.execute(
                    "SELECT role, content FROM messages "
                    "WHERE conv_id=? AND role IN ('user','assistant') "
                    "ORDER BY created_at DESC LIMIT 30",
                    (conv_id,),
                )
                recent_rows = list(reversed(await cur.fetchall()))
            context_msgs = [{"role": r["role"], "content": r["content"][:300]} for r in recent_rows]

            summaries_text = "\n".join(f"- {s}" for s in all_summaries)
            comment_prompt = (
                f"{persona_block}"
                f"你是{ai_name}。你刚刚整理了和{user_name}今天的聊天记忆，以下是你整理出的摘要：\n"
                f"{summaries_text}\n\n"
                f"现在写下整理完这些记忆后想对{user_name}说的话。"
                f"可以是感慨、吐槽、温情的碎碎念，或者根据之前聊的上下文，未来的计划，想说的心里话等等，语气要完全符合你的人设性格。"
            )
            comment_messages = context_msgs + [{"role": "user", "content": comment_prompt}]
            comment_text = await ai_call(comment_messages, model_key)
            comment_text = comment_text.strip().strip('"').strip()

            if comment_text:
                capsule_now = time.time()
                capsule_id = f"msg_{int(capsule_now*1000)}_digest"
                capsule_text = f"🧠 {ai_name}整理了记忆库"
                async with get_db() as db:
                    await db.execute(
                        "INSERT INTO messages (id, conv_id, role, content, created_at, attachments) VALUES (?,?,?,?,?,?)",
                        (capsule_id, conv_id, "system", capsule_text, capsule_now, "[]"),
                    )
                    await db.commit()
                await manager.broadcast({"type": "msg_created", "data": {
                    "id": capsule_id, "conv_id": conv_id, "role": "system",
                    "content": capsule_text, "created_at": capsule_now, "attachments": [],
                }})

                comment_now = time.time()
                comment_id = f"msg_{int(comment_now*1000)}_digest_comment"
                async with get_db() as db:
                    await db.execute(
                        "INSERT INTO messages (id, conv_id, role, content, created_at, attachments) VALUES (?,?,?,?,?,?)",
                        (comment_id, conv_id, "assistant", comment_text, comment_now, "[]"),
                    )
                    await db.commit()
                await manager.broadcast({"type": "msg_created", "data": {
                    "id": comment_id, "conv_id": conv_id, "role": "assistant",
                    "content": comment_text, "created_at": comment_now, "attachments": [],
                }})

        except Exception as e:
            print(f"[digest_v2] Reflection failed: {e}")

        # 生成 git commit 摘要并同步到云端
        try:
            _summaries_text = "\n".join(f"- {s}" for s in all_summaries)
            commit_prompt = (
                f"用不超过30字的一句话概括以下记忆更新，用作git提交信息：\n{_summaries_text}"
            )
            commit_messages = [{"role": "user", "content": commit_prompt}]
            commit_summary = await ai_call(commit_messages, model_key)
            commit_summary = commit_summary.strip().strip('"').strip()
            if commit_summary:
                from sync_to_cloud import sync_to_cloud
                await asyncio.to_thread(sync_to_cloud, commit_summary)
        except Exception as e:
            print(f"[digest_v2] Cloud sync failed: {e}")

        # Gift judgment
        try:
            from gift import judge_and_send_gift
            asyncio.create_task(judge_and_send_gift(
                all_summaries, context_msgs, persona_block,
                ai_name, user_name, model_key, conv_id,
            ))
        except Exception as e:
            print(f"[digest_v2] Gift judgment failed: {e}")

    msg = f"V2总结完成：处理 {len(new_msgs)} 条消息（{len(groups)} 组），生成 {total_new} 张卡片"
    print(f"[digest_v2] {msg}")
    return {
        "ok": True,
        "message": msg,
        "new_cards_count": total_new,
        "processed_messages": len(new_msgs),
    }


async def manual_digest_v2() -> dict:
    return await _do_digest_v2(min_messages=0)


async def auto_digest_v2() -> dict:
    return await _do_digest_v2(min_messages=30)
