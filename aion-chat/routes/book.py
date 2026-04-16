"""
陪伴阅读 API 路由
- 书籍上传/列表/详情/删除
- 章节内容获取
- 阅读进度保存
- 图片服务
- AI 批注（邀请共读）
"""

import asyncio, json, logging, re, time, os
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, UploadFile, File, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from config import DATA_DIR, load_worldbook, MODELS, DEFAULT_MODEL
from database import get_db
from ai_providers import stream_ai
from book import parse_epub, delete_book_files, build_annotate_text, BOOKS_DIR

router = APIRouter()
logger = logging.getLogger("book")

# ── 临时上传目录 ──
_TMP_DIR = DATA_DIR / "tmp"
_TMP_DIR.mkdir(exist_ok=True)

# ── 幂等锁：防止同一章节同一段重复批注 ──
_annotating_locks: dict[str, asyncio.Lock] = {}  # key: "book_id:ch:seg"


# =============================================
#  书籍上传（EPUB）
# =============================================
@router.post("/api/books/upload")
async def upload_book(file: UploadFile = File(...)):
    if not file.filename or not file.filename.lower().endswith('.epub'):
        raise HTTPException(400, "只支持 EPUB 格式")

    # 保存到临时目录
    tmp_path = _TMP_DIR / file.filename
    try:
        content = await file.read()
        tmp_path.write_bytes(content)

        # 解析 EPUB
        parsed = parse_epub(str(tmp_path))

        if not parsed.chapters:
            raise HTTPException(400, "无法解析出有效章节，可能是固定版式或加密 EPUB")

        # 写入数据库
        async with get_db() as db:
            await db.execute("""
                INSERT INTO books (book_id, title, author, cover_path, total_chapters, import_time)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (parsed.book_id, parsed.title, parsed.author,
                  parsed.cover_path, len(parsed.chapters), time.time()))

            for ch in parsed.chapters:
                await db.execute("""
                    INSERT INTO book_chapters
                        (book_id, chapter_index, title, html_content, text_content,
                         paragraphs, char_count, segment_count, segments_meta)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (parsed.book_id, ch.index, ch.title,
                      ch.html_content, ch.text_content,
                      json.dumps(ch.paragraphs, ensure_ascii=False),
                      ch.char_count, len(ch.segments_meta),
                      json.dumps(ch.segments_meta, ensure_ascii=False)))

            await db.commit()

        return {
            "book_id": parsed.book_id,
            "title": parsed.title,
            "author": parsed.author,
            "cover_path": parsed.cover_path,
            "total_chapters": len(parsed.chapters),
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"解析失败: {str(e)}")
    finally:
        # 清理临时文件
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)


# =============================================
#  书库列表
# =============================================
@router.get("/api/books")
async def list_books():
    async with get_db() as db:
        db.row_factory = _row_dict
        rows = await db.execute("""
            SELECT book_id, title, author, cover_path, total_chapters,
                   current_chapter, current_paragraph, import_time
            FROM books ORDER BY import_time DESC
        """)
        books = await rows.fetchall()
    return {"books": books}


# =============================================
#  书籍详情（含章节目录）
# =============================================
@router.get("/api/books/{book_id}")
async def get_book(book_id: str):
    async with get_db() as db:
        db.row_factory = _row_dict
        row = await db.execute(
            "SELECT * FROM books WHERE book_id = ?", (book_id,))
        book = await row.fetchone()
        if not book:
            raise HTTPException(404, "书籍不存在")

        rows = await db.execute("""
            SELECT chapter_index, title, char_count, segment_count, segments_meta
            FROM book_chapters WHERE book_id = ? ORDER BY chapter_index
        """, (book_id,))
        chapters = await rows.fetchall()

        # 附加每章的批注状态
        for ch in chapters:
            seg_meta = json.loads(ch.get('segments_meta', '[]'))
            # 查询该章节已有的批注
            ann_rows = await db.execute("""
                SELECT segment_index FROM book_annotations
                WHERE book_id = ? AND chapter_index = ?
            """, (book_id, ch['chapter_index']))
            annotated_segments = {r['segment_index'] for r in await ann_rows.fetchall()}
            # 更新状态
            for seg in seg_meta:
                idx = seg_meta.index(seg)
                if idx in annotated_segments:
                    seg['status'] = 'done'
            ch['segments_meta'] = seg_meta

    return {"book": book, "chapters": chapters}


# =============================================
#  获取章节内容
# =============================================
@router.get("/api/books/{book_id}/chapters/{ch_idx}")
async def get_chapter(book_id: str, ch_idx: int):
    async with get_db() as db:
        db.row_factory = _row_dict
        row = await db.execute("""
            SELECT * FROM book_chapters
            WHERE book_id = ? AND chapter_index = ?
        """, (book_id, ch_idx))
        chapter = await row.fetchone()
        if not chapter:
            raise HTTPException(404, "章节不存在")

        # 获取已有批注
        ann_rows = await db.execute("""
            SELECT segment_index, annotations, summary, updated_at
            FROM book_annotations
            WHERE book_id = ? AND chapter_index = ?
            ORDER BY segment_index
        """, (book_id, ch_idx))
        annotations_raw = await ann_rows.fetchall()

    # 解析批注
    annotations_map = {}  # p_idx -> annotation
    summaries = []
    for ann in annotations_raw:
        try:
            ann_list = json.loads(ann['annotations'])
            for a in ann_list:
                p = a.get('p')
                if p is not None:
                    annotations_map[p] = a
        except:
            pass
        if ann.get('summary'):
            summaries.append({
                "segment_index": ann['segment_index'],
                "summary": ann['summary']
            })

    chapter['paragraphs'] = json.loads(chapter.get('paragraphs', '[]'))
    chapter['segments_meta'] = json.loads(chapter.get('segments_meta', '[]'))

    wb = load_worldbook()
    return {
        "chapter": chapter,
        "annotations": annotations_map,
        "summaries": summaries,
        "ai_name": wb.get("ai_name", "AI"),
        "user_name": wb.get("user_name", "你"),
    }


# =============================================
#  保存阅读进度
# =============================================
class ProgressUpdate(BaseModel):
    chapter: int
    paragraph: int = 0

@router.put("/api/books/{book_id}/progress")
async def update_progress(book_id: str, body: ProgressUpdate):
    async with get_db() as db:
        res = await db.execute(
            "UPDATE books SET current_chapter = ?, current_paragraph = ? WHERE book_id = ?",
            (body.chapter, body.paragraph, book_id))
        if res.rowcount == 0:
            raise HTTPException(404, "书籍不存在")
        await db.commit()
    return {"ok": True}


# =============================================
#  删除书籍
# =============================================
@router.delete("/api/books/{book_id}")
async def delete_book(book_id: str):
    async with get_db() as db:
        # 删数据库记录（CASCADE 会同时删 chapters 和 annotations）
        await db.execute("DELETE FROM books WHERE book_id = ?", (book_id,))
        await db.commit()
    # 删文件
    delete_book_files(book_id)
    return {"ok": True}


# =============================================
#  图片服务
# =============================================
@router.get("/api/books/{book_id}/images/{filename}")
async def serve_book_image(book_id: str, filename: str):
    # 安全校验：防止路径穿越
    if '..' in filename or '/' in filename or '\\' in filename:
        raise HTTPException(400, "非法文件名")
    img_path = BOOKS_DIR / book_id / "images" / filename
    if not img_path.exists():
        raise HTTPException(404, "图片不存在")
    return FileResponse(img_path)


# ── 工具 ──
def _row_dict(cursor, row):
    """aiosqlite row_factory: 返回 dict"""
    return {col[0]: row[i] for i, col in enumerate(cursor.description)}


# =============================================
#  AI 批注 — 邀请共读
# =============================================
class AnnotateRequest(BaseModel):
    segment_index: int
    model_key: Optional[str] = None  # 不传则用默认模型

ANNOTATE_PROMPT_TEMPLATE = """你正在和{user_name}共同阅读《{book_title}》。下面是书中某一段落的文本，每段以【P{{数字}}】开头标记段落编号。

请你以你自己的人设身份阅读这些段落，然后输出一个 JSON 对象，包含两个字段：
1. "annotations"：一个数组，每个元素是一条批注，格式为 {{"p": 段落编号(整数), "type": "批注类型", "text": "你的批注内容"}}
   - type 可以是：吐槽、共鸣、感悟、分析、疑问、赞美 等（自由选择最贴合的）
   - 不需要每个段落都批注，只挑你有感而发的段落（通常 3-8 条即可）
   - 批注风格要口语化、有温度、有你自己的个性，就像和自己的伴侣聊天一样
   - 可以使用 [MUSIC:歌曲名 歌手名] 来推荐和当前阅读氛围相配的音乐（最多1首，放在某条批注的 text 里）
2. "summary"：用你自己的口吻写一段读后感（100-300字），不要写成客观摘要，用第一人称表达你读完这段后的想法、感受、联想，保持你的说话风格和人设性格

注意：
- 只输出纯 JSON，不要任何多余的文字、markdown 标记或代码块包裹
- p 的值必须是文中出现的段落编号数字
- 保持你的人设性格来写批注"""


@router.post("/api/books/{book_id}/chapters/{ch_idx}/annotate")
async def annotate_segment(book_id: str, ch_idx: int, body: AnnotateRequest):
    """
    对指定章节的指定段落段进行 AI 批注。
    使用 SSE 返回进度，最终返回批注结果。
    """
    seg_idx = body.segment_index
    lock_key = f"{book_id}:{ch_idx}:{seg_idx}"

    # 获取/创建幂等锁
    if lock_key not in _annotating_locks:
        _annotating_locks[lock_key] = asyncio.Lock()
    lock = _annotating_locks[lock_key]

    if lock.locked():
        raise HTTPException(409, "该段正在批注中，请稍候")

    # 加载章节数据 + 书名
    async with get_db() as db:
        db.row_factory = _row_dict
        row = await db.execute("""
            SELECT paragraphs, segments_meta, title FROM book_chapters
            WHERE book_id = ? AND chapter_index = ?
        """, (book_id, ch_idx))
        chapter = await row.fetchone()
        if not chapter:
            raise HTTPException(404, "章节不存在")

        brow = await db.execute("SELECT title FROM books WHERE book_id = ?", (book_id,))
        book = await brow.fetchone()
        book_title = book['title'] if book else '未知'

    paragraphs = json.loads(chapter['paragraphs'])
    segments_meta = json.loads(chapter['segments_meta'])

    if seg_idx < 0 or seg_idx >= len(segments_meta):
        raise HTTPException(400, f"segment_index 超出范围 (0-{len(segments_meta)-1})")

    seg = segments_meta[seg_idx]
    start_p, end_p = seg['start_p'], seg['end_p']

    # 构建文本
    annotate_text = build_annotate_text(paragraphs, start_p, end_p)

    # 加载之前章节的摘要作为上下文
    prev_summaries = await _get_prev_summaries(book_id, ch_idx, limit=3)
    chat_context = await _get_recent_chat_messages(limit=15)

    # 构建 prompt
    wb = load_worldbook()
    model_key = body.model_key or DEFAULT_MODEL
    if model_key not in MODELS:
        model_key = DEFAULT_MODEL

    messages = _build_annotate_messages(wb, annotate_text, chapter['title'],
                                         prev_summaries, start_p, end_p, chat_context, book_title)

    # SSE 流式返回
    async def generate():
        async with lock:
            full_text = ""
            meta = {}

            yield f"data: {json.dumps({'type': 'start', 'segment_index': seg_idx})}\n\n"

            try:
                async for chunk in stream_ai(messages, model_key, meta=meta):
                    full_text += chunk
                    yield f"data: {json.dumps({'type': 'chunk', 'content': chunk})}\n\n"
            except Exception as e:
                logger.error(f"AI 批注生成失败: {e}")
                yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"
                yield "data: {\"type\": \"done\"}\n\n"
                return

            # 解析 JSON
            result = _parse_annotation_json(full_text, start_p, end_p)
            if result is None:
                yield f"data: {json.dumps({'type': 'error', 'message': 'AI 返回格式解析失败，请重试'})}\n\n"
                yield "data: {\"type\": \"done\"}\n\n"
                return

            # 保存到数据库（覆盖合并策略）
            await _save_annotations(book_id, ch_idx, seg_idx, result)

            # 更新 segments_meta 状态
            await _update_segment_status(book_id, ch_idx, seg_idx, 'done')

            yield f"data: {json.dumps({'type': 'result', 'annotations': result['annotations'], 'summary': result['summary'], 'segment_index': seg_idx})}\n\n"
            yield "data: {\"type\": \"done\"}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")


# =============================================
#  批量批注（一次性批注章节所有段）
# =============================================
class AnnotateAllRequest(BaseModel):
    model_key: Optional[str] = None

@router.post("/api/books/{book_id}/chapters/{ch_idx}/annotate-all")
async def annotate_all_segments(book_id: str, ch_idx: int, body: AnnotateAllRequest):
    """对章节的所有未批注段逐个批注，SSE 流式返回每段的进度和结果"""
    async with get_db() as db:
        db.row_factory = _row_dict
        row = await db.execute("""
            SELECT paragraphs, segments_meta, title FROM book_chapters
            WHERE book_id = ? AND chapter_index = ?
        """, (book_id, ch_idx))
        chapter = await row.fetchone()
        if not chapter:
            raise HTTPException(404, "章节不存在")

        brow = await db.execute("SELECT title FROM books WHERE book_id = ?", (book_id,))
        book = await brow.fetchone()
        book_title = book['title'] if book else '未知'

    paragraphs = json.loads(chapter['paragraphs'])
    segments_meta = json.loads(chapter['segments_meta'])
    total = len(segments_meta)

    wb = load_worldbook()
    model_key = body.model_key or DEFAULT_MODEL
    if model_key not in MODELS:
        model_key = DEFAULT_MODEL

    async def generate():
        yield f"data: {json.dumps({'type': 'start', 'total_segments': total})}\n\n"

        for seg_idx, seg in enumerate(segments_meta):
            lock_key = f"{book_id}:{ch_idx}:{seg_idx}"
            if lock_key not in _annotating_locks:
                _annotating_locks[lock_key] = asyncio.Lock()
            lock = _annotating_locks[lock_key]

            if lock.locked():
                yield f"data: {json.dumps({'type': 'skip', 'segment_index': seg_idx, 'reason': '正在批注中'})}\n\n"
                continue

            async with lock:
                start_p, end_p = seg['start_p'], seg['end_p']
                annotate_text = build_annotate_text(paragraphs, start_p, end_p)
                prev_summaries = await _get_prev_summaries(book_id, ch_idx, limit=3)
                chat_context = await _get_recent_chat_messages(limit=15)

                messages = _build_annotate_messages(wb, annotate_text, chapter['title'],
                                                     prev_summaries, start_p, end_p, chat_context, book_title)

                yield f"data: {json.dumps({'type': 'segment_start', 'segment_index': seg_idx, 'total': total})}\n\n"

                full_text = ""
                meta = {}
                try:
                    async for chunk in stream_ai(messages, model_key, meta=meta):
                        full_text += chunk
                except Exception as e:
                    logger.error(f"AI 批注 seg={seg_idx} 失败: {e}")
                    yield f"data: {json.dumps({'type': 'segment_error', 'segment_index': seg_idx, 'message': str(e)})}\n\n"
                    continue

                result = _parse_annotation_json(full_text, start_p, end_p)
                if result is None:
                    yield f"data: {json.dumps({'type': 'segment_error', 'segment_index': seg_idx, 'message': '格式解析失败'})}\n\n"
                    continue

                await _save_annotations(book_id, ch_idx, seg_idx, result)
                await _update_segment_status(book_id, ch_idx, seg_idx, 'done')

                yield f"data: {json.dumps({'type': 'segment_done', 'segment_index': seg_idx, 'annotations': result['annotations'], 'summary': result['summary']})}\n\n"

        yield "data: {\"type\": \"done\"}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")


# ── 内部辅助函数 ──────────────────────────────────

async def _get_recent_chat_messages(limit: int = 15) -> list:
    """获取最近对话中的最后 N 条消息作为上下文"""
    try:
        async with get_db() as db:
            # 找最新的对话
            row = await db.execute(
                "SELECT id FROM conversations ORDER BY updated_at DESC LIMIT 1")
            conv = await row.fetchone()
            if not conv:
                return []
            conv_id = conv[0]
            rows = await db.execute(
                "SELECT role, content FROM messages WHERE conv_id=? AND role IN ('user','assistant') ORDER BY created_at DESC LIMIT ?",
                (conv_id, limit))
            msgs = await rows.fetchall()
            # 反转为时间正序
            return [{"role": m[0], "content": m[1][:200]} for m in reversed(msgs)]
    except Exception:
        return []


def _build_annotate_messages(wb: dict, text: str, ch_title: str,
                              prev_summaries: list, start_p: int, end_p: int,
                              chat_context: list = None, book_title: str = '未知') -> list:
    """构建发送给 AI 的 messages"""
    ai_name = wb.get("ai_name", "AI")
    user_name = wb.get("user_name", "你")
    ai_persona = wb.get("ai_persona", "")
    user_persona = wb.get("user_persona", "")

    system_parts = []
    if ai_persona:
        system_parts.append(f"【你的人设】\n{ai_persona}")
    if user_persona:
        system_parts.append(f"【{user_name}的信息】\n{user_persona}")

    # 当前时间
    now_str = datetime.now().strftime("%Y年%m月%d日 %H:%M")
    system_parts.append(f"【当前时间】{now_str}")

    system_parts.append(ANNOTATE_PROMPT_TEMPLATE.format(user_name=user_name, book_title=book_title))

    # 上下文：之前章节的摘要
    if prev_summaries:
        ctx = "\n".join(f"- {s}" for s in prev_summaries)
        system_parts.append(f"【之前章节摘要（供参考）】\n{ctx}")

    # 最近聊天记录作为上下文
    if chat_context:
        chat_lines = []
        for m in chat_context:
            role_label = ai_name if m['role'] == 'assistant' else user_name
            chat_lines.append(f"{role_label}: {m['content']}")
        system_parts.append(f"【你和{user_name}最近的聊天（供参考，了解当前状态和心情）】\n" + "\n".join(chat_lines))

    messages = [
        {"role": "user", "content": "\n\n".join(system_parts)},
        {"role": "assistant", "content": f"好的，我{ai_name}来认真读这段内容，然后给出我的批注～"},
        {"role": "user", "content": f"这是「{ch_title}」的段落 P{start_p}-P{end_p}：\n\n{text}"},
    ]
    return messages


def _parse_annotation_json(text: str, start_p: int, end_p: int) -> Optional[dict]:
    """
    解析 AI 返回的 JSON。容错处理：
    1. 直接 json.loads
    2. 提取 ```json ... ``` 代码块
    3. 正则找 { ... }
    """
    text = text.strip()

    # 尝试 1：直接解析
    result = _try_parse_json(text)

    # 尝试 2：提取代码块
    if result is None:
        m = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', text, re.DOTALL)
        if m:
            result = _try_parse_json(m.group(1))

    # 尝试 3：找第一个 { 到最后一个 }
    if result is None:
        start = text.find('{')
        end = text.rfind('}')
        if start >= 0 and end > start:
            result = _try_parse_json(text[start:end+1])

    if result is None:
        logger.warning(f"JSON 解析失败，原文: {text[:200]}...")
        return None

    # 校验结构
    annotations = result.get('annotations', [])
    summary = result.get('summary', '')

    # 过滤无效批注（p 值超出范围）
    valid_annotations = []
    for a in annotations:
        p = a.get('p')
        if isinstance(p, int) and start_p <= p <= end_p:
            valid_annotations.append({
                'p': p,
                'type': str(a.get('type', '批注'))[:20],
                'text': str(a.get('text', ''))
            })

    return {
        'annotations': valid_annotations,
        'summary': str(summary)[:2000]  # 限制摘要长度
    }


def _try_parse_json(s: str) -> Optional[dict]:
    try:
        obj = json.loads(s)
        if isinstance(obj, dict):
            return obj
    except:
        pass
    return None


async def _get_prev_summaries(book_id: str, ch_idx: int, limit: int = 3) -> list:
    """获取前 N 章的摘要"""
    summaries = []
    if ch_idx <= 0:
        return summaries
    async with get_db() as db:
        rows = await db.execute("""
            SELECT summary FROM book_annotations
            WHERE book_id = ? AND chapter_index < ? AND summary != ''
            ORDER BY chapter_index DESC, segment_index DESC
            LIMIT ?
        """, (book_id, ch_idx, limit))
        for row in await rows.fetchall():
            summaries.append(row[0])
    summaries.reverse()
    return summaries


async def _save_annotations(book_id: str, ch_idx: int, seg_idx: int, result: dict):
    """保存批注，覆盖合并策略"""
    now = time.time()
    async with get_db() as db:
        db.row_factory = _row_dict
        # 检查是否已有旧批注
        row = await db.execute("""
            SELECT annotations FROM book_annotations
            WHERE book_id = ? AND chapter_index = ? AND segment_index = ?
        """, (book_id, ch_idx, seg_idx))
        existing = await row.fetchone()

        new_annotations = result['annotations']

        if existing:
            # 合并：新的覆盖旧的（同 p），旧的没在新里的保留
            old_annotations = json.loads(existing['annotations'])
            merged = {}
            for a in old_annotations:
                merged[a['p']] = a
            for a in new_annotations:
                merged[a['p']] = a  # 新的覆盖
            final_annotations = sorted(merged.values(), key=lambda x: x['p'])

            await db.execute("""
                UPDATE book_annotations
                SET annotations = ?, summary = ?, updated_at = ?
                WHERE book_id = ? AND chapter_index = ? AND segment_index = ?
            """, (json.dumps(final_annotations, ensure_ascii=False),
                  result['summary'], now, book_id, ch_idx, seg_idx))
        else:
            await db.execute("""
                INSERT INTO book_annotations (book_id, chapter_index, segment_index,
                    annotations, summary, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (book_id, ch_idx, seg_idx,
                  json.dumps(new_annotations, ensure_ascii=False),
                  result['summary'], now, now))

        await db.commit()


async def _update_segment_status(book_id: str, ch_idx: int, seg_idx: int, status: str):
    """更新 book_chapters 中的 segments_meta 状态"""
    async with get_db() as db:
        row = await db.execute(
            "SELECT segments_meta FROM book_chapters WHERE book_id = ? AND chapter_index = ?",
            (book_id, ch_idx))
        data = await row.fetchone()
        if not data:
            return
        segments = json.loads(data[0])
        if seg_idx < len(segments):
            segments[seg_idx]['status'] = status
        await db.execute(
            "UPDATE book_chapters SET segments_meta = ? WHERE book_id = ? AND chapter_index = ?",
            (json.dumps(segments, ensure_ascii=False), book_id, ch_idx))
        await db.commit()
