"""
Memory V2 Digest Engine: 原子卡片拆分 + 情绪评价 + 对话强度
"""

import asyncio
import json
import time
from datetime import datetime


def _parse_atomic_cards(raw: str) -> list[dict]:
    """Parse Agent A output: a JSON array of atomic cards."""
    if isinstance(raw, list):
        items = raw
    else:
        raw = raw.strip()
        if "```" in raw:
            start = raw.find("[")
            end = raw.rfind("]") + 1
            if start >= 0 and end > start:
                raw = raw[start:end]
        try:
            items = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
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
    """Build a single prompt that does atomic split + emotion in one call."""
    return (
        f"{persona_block}"
        f"你是一个记忆拆分专家。请将下面的对话拆分成独立的原子记忆卡片，每张卡片只记录一件事。\n\n"
        f"规则：\n"
        f"- 每张卡片的 content 应是一个完整的陈述句，包含日期和必要上下文\n"
        f"- 使用 \"{user_name}\" 和 \"{ai_name}\" 指代双方\n"
        f"- type: event/preference/emotion/promise/plan/fact\n"
        f"- keywords: 3-6 个关键词，分两层：\n"
        f"  · 领域词（1-2个）：大类，如 阅读、技术开发、日常起居、社交、情绪、创作、游戏\n"
        f"  · 实体词（2-4个）：具体人事物地名\n"
        f"  领域词在前，实体词在后，每个关键词是数组中独立的字符串。\n"
        f"  禁止人名（{user_name}, {ai_name}）和泛指词（提醒、建议、完成、计划、测试、观察、休息、未完成、担忧、偏好）\n"
        f"- importance: 0.0-1.0，默认 0.3\n"
        f"- unresolved: 未完成=true，已发生=false\n"
        f"- valence: -1.0~1.0（正=正面情绪，负=负面）\n"
        f"- arousal: -1.0~1.0（正=高能量，负=低能量）\n\n"
        f"输出 JSON 数组，每个元素：\n"
        f'{{"content":"...","type":"...","keywords":[...],"importance":0.X,"unresolved":false,"valence":0.X,"arousal":0.X}}\n\n'
        f"严格只输出 JSON 数组。\n\n"
        f"【对话记录】：\n{messages_text}"
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
    import aiosqlite
    from database import get_db
    from sentinel import _unpack_embedding
    from memory import cosine_similarity

    if not card_embedding or not source_conv:
        return None
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT id, embedding FROM memory_cards WHERE source_conv=? AND embedding IS NOT NULL",
            (source_conv,),
        )
        rows = await cur.fetchall()
    for row in rows:
        mem_vec = _unpack_embedding(row["embedding"])
        sim = cosine_similarity(card_embedding, mem_vec)
        if sim >= threshold:
            return row["id"]
    return None


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
    split_mode = settings.get("digest_agents", {}).get("split_mode", "separate")
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
        return {"ok": True, "message": "没有新消息需要总结", "new_cards_count": 0, "processed_messages": 0}

    if min_messages > 0 and len(new_msgs) < min_messages:
        return {"ok": True, "message": f"消息不足 {min_messages} 条，跳过", "new_cards_count": 0, "processed_messages": 0}

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
            # Single call: split + emotion
            unified_prompt = _build_unified_prompt(messages_text, user_name, ai_name, persona_block)
            try:
                raw_u = await simple_ai_call([{"role": "user", "content": unified_prompt}], model_key)
            except Exception as e:
                print(f"[digest_v2] Unified agent failed: {e}")
                save_digest_anchor(source_end_ts)
                continue
            atomic_cards = _parse_atomic_cards(raw_u)
            if not atomic_cards:
                print(f"[digest_v2] Unified agent returned no valid cards for group {group_start}")
                save_digest_anchor(source_end_ts)
                continue
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

        # ── Phase 2: 并发 dedup ──
        keep_indices = []
        if source_conv_id:
            async def _noop():
                return None
            dedup_tasks = []
            for i, ac in enumerate(atomic_cards):
                if vectors[i]:
                    dedup_tasks.append(_dedup_against_realtime(
                        ac["content"], vectors[i], source_conv_id, auto_threshold
                    ))
                else:
                    dedup_tasks.append(_noop())
            dedup_results = await asyncio.gather(*dedup_tasks, return_exceptions=True)
            for i, result in enumerate(dedup_results):
                if isinstance(result, Exception) or result:
                    if not isinstance(result, Exception):
                        print(f"[digest_v2] Skipping duplicate of {result}: {atomic_cards[i]['content'][:40]}")
                    continue
                keep_indices.append(i)
        else:
            keep_indices = list(range(len(atomic_cards)))

        # ── Phase 3: 建卡 + 并发 lifecycle 判断 ──
        for i in keep_indices:
            ac = atomic_cards[i]
            vec = vectors[i]

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

    # Auto-close low-importance events older than 24h
    cutoff_24h = time.time() - 24 * 3600
    async with get_db() as db:
        cur = await db.execute(
            "UPDATE memory_cards SET status='closed', updated_at=? "
            "WHERE type='event' AND status='open' AND importance <= 0.4 AND created_at < ?",
            (time.time(), cutoff_24h)
        )
        if cur.rowcount > 0:
            await db.commit()
            print(f"[digest_v2] Auto-closed {cur.rowcount} low-importance events (>24h)")

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

        # Gift judgment
        try:
            from gift import judge_and_send_gift
            asyncio.create_task(judge_and_send_gift(
                all_summaries, context_msgs, persona_block,
                ai_name, user_name, model_key, conv_id,
            ))
        except Exception as e:
            print(f"[digest_v2] Gift judgment failed: {e}")

    return {
        "ok": True,
        "message": f"V2总结完成：处理 {len(new_msgs)} 条消息（{len(groups)} 组），生成 {total_new} 张卡片",
        "new_cards_count": total_new,
        "processed_messages": len(new_msgs),
    }


async def manual_digest_v2() -> dict:
    return await _do_digest_v2(min_messages=0)


async def auto_digest_v2() -> dict:
    return await _do_digest_v2(min_messages=30)
