"""
陪伴阅读 API 路由
- 书籍上传/列表/详情/删除
- 章节内容获取
- 阅读进度保存
- 图片服务
- AI 批注（邀请共读）
- 用户高亮（框选提问持久化）
"""

import asyncio, hashlib, json, logging, re, time, os
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, UploadFile, File, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from config import DATA_DIR, load_worldbook, MODELS, DEFAULT_MODEL
from database import get_db
from ai_providers import stream_ai, CLI_STATUS_PREFIX
from book import parse_epub, parse_pdf, delete_book_files, build_annotate_text, BOOKS_DIR
from sentinel import get_embedding, _pack_embedding
from context_builder import fetch_merged_timeline, render_merged_timeline
from chatroom import stream_connor_cli, check_connor_online, _read_connor_persona, load_chatroom_config

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
    fname_lower = (file.filename or "").lower()
    if not (fname_lower.endswith('.epub') or fname_lower.endswith('.pdf')):
        raise HTTPException(400, "只支持 EPUB 和 PDF 格式")

    # 保存到临时目录
    tmp_path = _TMP_DIR / file.filename
    try:
        content = await file.read()
        tmp_path.write_bytes(content)

        # 按格式解析
        if fname_lower.endswith('.epub'):
            parsed = parse_epub(str(tmp_path))
        else:
            parsed = parse_pdf(str(tmp_path))

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

        # 获取已有批注（区分 annotator）
        ann_rows = await db.execute("""
            SELECT segment_index, annotations, summary, updated_at, COALESCE(annotator, 'aion') as annotator
            FROM book_annotations
            WHERE book_id = ? AND chapter_index = ?
            ORDER BY segment_index
        """, (book_id, ch_idx))
        annotations_raw = await ann_rows.fetchall()

        # 获取用户高亮（含 annotator 和 connor_answer）
        hl_rows = await db.execute("""
            SELECT id, selected_text, start_p, start_offset, end_p, end_offset,
                   question, answer, created_at,
                   COALESCE(annotator, 'aion') as annotator,
                   COALESCE(connor_answer, '') as connor_answer
            FROM book_highlights
            WHERE book_id = ? AND chapter_index = ?
            ORDER BY created_at
        """, (book_id, ch_idx))
        highlights_raw = await hl_rows.fetchall()

    # 解析批注（按 annotator 分组）
    aion_annotations_map = {}  # p_idx -> annotation
    connor_annotations_map = {}
    aion_summaries = []
    connor_summaries = []
    for ann in annotations_raw:
        annotator = ann.get('annotator', 'aion')
        target_map = aion_annotations_map if annotator == 'aion' else connor_annotations_map
        target_summaries = aion_summaries if annotator == 'aion' else connor_summaries
        try:
            ann_list = json.loads(ann['annotations'])
            for a in ann_list:
                p = a.get('p')
                if p is not None:
                    target_map[p] = a
        except:
            pass
        if ann.get('summary'):
            target_summaries.append({
                "segment_index": ann['segment_index'],
                "summary": ann['summary']
            })

    chapter['paragraphs'] = json.loads(chapter.get('paragraphs', '[]'))
    chapter['segments_meta'] = json.loads(chapter.get('segments_meta', '[]'))

    # 解析高亮
    highlights = []
    for hl in highlights_raw:
        highlights.append({
            "id": hl['id'],
            "selected_text": hl['selected_text'],
            "start_p": hl['start_p'],
            "start_offset": hl['start_offset'],
            "end_p": hl['end_p'],
            "end_offset": hl['end_offset'],
            "question": hl['question'],
            "answer": hl['answer'],
            "created_at": hl['created_at'],
            "annotator": hl.get('annotator', 'aion'),
            "connor_answer": hl.get('connor_answer', ''),
        })

    wb = load_worldbook()
    cfg = load_chatroom_config()
    connor_name = cfg.get("connor_name", "Connor")
    return {
        "chapter": chapter,
        "annotations": aion_annotations_map,
        "connor_annotations": connor_annotations_map,
        "summaries": aion_summaries,
        "connor_summaries": connor_summaries,
        "highlights": highlights,
        "ai_name": wb.get("ai_name", "AI"),
        "user_name": wb.get("user_name", "你"),
        "connor_name": connor_name,
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


# =============================================
#  用户高亮（框选提问持久化）
# =============================================
class HighlightCreate(BaseModel):
    selected_text: str
    start_p: int
    start_offset: int
    end_p: int
    end_offset: int
    question: str
    answer: str
    annotator: Optional[str] = 'aion'  # 'aion', 'connor', 'group'
    connor_answer: Optional[str] = ''

@router.post("/api/books/{book_id}/chapters/{ch_idx}/highlights")
async def create_highlight(book_id: str, ch_idx: int, body: HighlightCreate):
    async with get_db() as db:
        cursor = await db.execute("""
            INSERT INTO book_highlights
                (book_id, chapter_index, selected_text, start_p, start_offset,
                 end_p, end_offset, question, answer, created_at, annotator, connor_answer)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (book_id, ch_idx, body.selected_text, body.start_p, body.start_offset,
              body.end_p, body.end_offset, body.question, body.answer, time.time(),
              body.annotator, body.connor_answer))
        await db.commit()
        return {"ok": True, "id": cursor.lastrowid}

@router.delete("/api/books/{book_id}/highlights/{hl_id}")
async def delete_highlight(book_id: str, hl_id: int):
    async with get_db() as db:
        await db.execute(
            "DELETE FROM book_highlights WHERE id = ? AND book_id = ?",
            (hl_id, book_id))
        await db.commit()
    return {"ok": True}


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
    对指定章节的指定段落段进行 AI 批注（Aion + Connor 并行）。
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

    # 加载上下文
    prev_summaries = await _get_prev_summaries(book_id, ch_idx, limit=3)
    chat_context = await _get_chat_context_merged(limit=15)

    # 构建 Aion prompt
    wb = load_worldbook()
    ai_name = wb.get("ai_name", "AI")
    connor_name = load_chatroom_config().get("connor_name", "Connor")
    model_key = body.model_key or DEFAULT_MODEL
    if model_key not in MODELS:
        model_key = DEFAULT_MODEL
    if model_key not in MODELS:
        model_key = next(iter(MODELS)) if MODELS else None
    if not model_key:
        raise HTTPException(500, "没有可用的 AI 模型")

    aion_messages = _build_annotate_messages(wb, annotate_text, chapter['title'],
                                              prev_summaries, start_p, end_p, chat_context, book_title)

    # 检查 Connor 是否可用
    connor_available = await _is_connor_available()

    # SSE 流式返回
    async def generate():
        async with lock:
            yield f"data: {json.dumps({'type': 'start', 'segment_index': seg_idx, 'connor_available': connor_available})}\n\n"

            # 并行执行 Aion 和 Connor 批注
            aion_task = asyncio.create_task(_run_aion_annotation(aion_messages, model_key))
            connor_task = None
            if connor_available:
                connor_messages = _build_connor_annotate_messages(
                    wb, annotate_text, chapter['title'], prev_summaries, start_p, end_p, chat_context, book_title)
                connor_task = asyncio.create_task(_run_connor_annotation(connor_messages))

            # 等待 Aion 完成
            aion_text, aion_error = await aion_task
            if aion_error:
                yield f"data: {json.dumps({'type': 'aion_error', 'message': aion_error})}\n\n"
            else:
                aion_result = _parse_annotation_json(aion_text, start_p, end_p)
                if aion_result:
                    await _save_annotations(book_id, ch_idx, seg_idx, aion_result, annotator='aion')
                    yield f"data: {json.dumps({'type': 'aion_result', 'annotations': aion_result['annotations'], 'summary': aion_result['summary'], 'segment_index': seg_idx})}\n\n"
                else:
                    yield f"data: {json.dumps({'type': 'aion_error', 'message': f'{ai_name} 返回格式解析失败'})}\n\n"

            # 等待 Connor 完成
            if connor_task:
                connor_text, connor_error = await connor_task
                if connor_error:
                    yield f"data: {json.dumps({'type': 'connor_error', 'message': connor_error})}\n\n"
                else:
                    connor_result = _parse_annotation_json(connor_text, start_p, end_p)
                    if connor_result:
                        await _save_annotations(book_id, ch_idx, seg_idx, connor_result, annotator='connor')
                        yield f"data: {json.dumps({'type': 'connor_result', 'annotations': connor_result['annotations'], 'summary': connor_result['summary'], 'segment_index': seg_idx})}\n\n"
                    else:
                        yield f"data: {json.dumps({'type': 'connor_error', 'message': f'{connor_name} 返回格式解析失败'})}\n\n"

            # 更新 segments_meta 状态
            await _update_segment_status(book_id, ch_idx, seg_idx, 'done')
            yield "data: {\"type\": \"done\"}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")


# =============================================
#  批量批注（一次性批注章节所有段）
# =============================================
class AnnotateAllRequest(BaseModel):
    model_key: Optional[str] = None

@router.post("/api/books/{book_id}/chapters/{ch_idx}/annotate-all")
async def annotate_all_segments(book_id: str, ch_idx: int, body: AnnotateAllRequest):
    """对章节的所有未批注段逐个批注（Aion+Connor并行），SSE 流式返回每段的进度和结果"""
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
    if model_key not in MODELS:
        model_key = next(iter(MODELS)) if MODELS else None
    if not model_key:
        raise HTTPException(500, "没有可用的 AI 模型")

    # 预先检测 Connor 可用性（整个批注流程中只检查一次）
    connor_available = await _is_connor_available()

    async def generate():
        yield f"data: {json.dumps({'type': 'start', 'total_segments': total, 'connor_available': connor_available})}\n\n"

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
                chat_context = await _get_chat_context_merged(limit=15)

                ai_name = wb.get("ai_name", "AI")
                connor_name = load_chatroom_config().get("connor_name", "Connor")
                aion_messages = _build_annotate_messages(wb, annotate_text, chapter['title'],
                                                         prev_summaries, start_p, end_p, chat_context, book_title)

                yield f"data: {json.dumps({'type': 'segment_start', 'segment_index': seg_idx, 'total': total})}\n\n"

                # 并行执行 Aion + Connor
                aion_task = asyncio.create_task(_run_aion_annotation(aion_messages, model_key))
                connor_task = None
                if connor_available:
                    connor_messages = _build_connor_annotate_messages(
                        wb, annotate_text, chapter['title'], prev_summaries, start_p, end_p, chat_context, book_title)
                    connor_task = asyncio.create_task(_run_connor_annotation(connor_messages))

                # Aion 结果
                aion_text, aion_error = await aion_task
                aion_result = None
                if aion_error:
                    logger.error(f"{ai_name} 批注 seg={seg_idx} 失败: {aion_error}")
                    yield f"data: {json.dumps({'type': 'segment_error', 'segment_index': seg_idx, 'message': f'{ai_name}: {aion_error}', 'who': 'aion'})}\n\n"
                else:
                    aion_result = _parse_annotation_json(aion_text, start_p, end_p)
                    if aion_result:
                        await _save_annotations(book_id, ch_idx, seg_idx, aion_result, annotator='aion')
                    else:
                        yield f"data: {json.dumps({'type': 'segment_error', 'segment_index': seg_idx, 'message': f'{ai_name} 格式解析失败', 'who': 'aion'})}\n\n"

                # Connor 结果
                connor_result = None
                if connor_task:
                    connor_text, connor_error = await connor_task
                    if connor_error:
                        logger.error(f"{connor_name} 批注 seg={seg_idx} 失败: {connor_error}")
                        yield f"data: {json.dumps({'type': 'segment_error', 'segment_index': seg_idx, 'message': f'{connor_name}: {connor_error}', 'who': 'connor'})}\n\n"
                    else:
                        connor_result = _parse_annotation_json(connor_text, start_p, end_p)
                        if connor_result:
                            await _save_annotations(book_id, ch_idx, seg_idx, connor_result, annotator='connor')
                        else:
                            yield f"data: {json.dumps({'type': 'segment_error', 'segment_index': seg_idx, 'message': f'{connor_name} 格式解析失败', 'who': 'connor'})}\n\n"

                await _update_segment_status(book_id, ch_idx, seg_idx, 'done')

                yield f"data: {json.dumps({'type': 'segment_done', 'segment_index': seg_idx, 'aion_annotations': aion_result['annotations'] if aion_result else [], 'aion_summary': aion_result['summary'] if aion_result else '', 'connor_annotations': connor_result['annotations'] if connor_result else [], 'connor_summary': connor_result['summary'] if connor_result else ''})}\n\n"

        yield "data: {\"type\": \"done\"}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")


# ── 内部辅助函数 ──────────────────────────────────

async def _is_connor_available() -> bool:
    """检查 Connor 是否可用（persona 文件存在 + CLI 路径存在）"""
    try:
        persona = _read_connor_persona()
        if not persona:
            return False
        cli_path = Path(__file__).parent.parent.parent / "Connor-Codex" / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
        return cli_path.exists()
    except Exception:
        return False


async def _get_chat_context_merged(limit: int = 15) -> list:
    """获取合并的私聊+群聊时间线作为上下文（替代旧的仅查私聊逻辑）"""
    try:
        merged = await fetch_merged_timeline("aion", limit)
        # 简化为 role+content 格式供批注 prompt 使用
        timeline = render_merged_timeline(merged, "aion")
        result = []
        for m in timeline:
            content = m.get("content", "")[:200]
            if content:
                result.append({"role": m["role"], "content": content})
        return result
    except Exception:
        return []


async def _run_aion_annotation(messages: list, model_key: str) -> tuple[str, str]:
    """执行 Aion 批注，返回 (full_text, error_msg)"""
    try:
        full_text = ""
        async for chunk in stream_ai(messages, model_key, meta={}):
            full_text += chunk
        return full_text, ""
    except Exception as e:
        return "", str(e)


async def _run_connor_annotation(messages: list) -> tuple[str, str]:
    """执行 Connor 批注（Codex CLI），返回 (full_text, error_msg)"""
    try:
        full_text = ""
        async for chunk in stream_connor_cli(messages=messages):
            if chunk.startswith(CLI_STATUS_PREFIX):
                continue  # 跳过状态事件
            full_text += chunk
        return full_text, ""
    except Exception as e:
        return "", str(e)


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


_AI_ERROR_PREFIXES = ("[硅基流动错误", "[Gemini错误", "[中转站错误", "[自定义端点错误", "[错误]")


def _extract_ai_error(text: str) -> Optional[str]:
    """检测 AI provider 返回的错误信息（非 JSON，而是内联错误文本）"""
    stripped = text.strip()
    for prefix in _AI_ERROR_PREFIXES:
        if stripped.startswith(prefix):
            return stripped
    return None


def _build_connor_annotate_messages(wb: dict, text: str, ch_title: str,
                                     prev_summaries: list, start_p: int, end_p: int,
                                     chat_context: list = None, book_title: str = '未知') -> list:
    """构建 Connor 的批注 messages（使用 Connor 人设）"""
    cfg = load_chatroom_config()
    connor_name = cfg.get("connor_name", "Connor")
    user_name = wb.get("user_name", "你")
    connor_persona = _read_connor_persona()
    user_persona = wb.get("user_persona", "")

    system_parts = []
    if connor_persona:
        system_parts.append(f"【你的人设】\n{connor_persona}")
    if user_persona:
        system_parts.append(f"【{user_name}的信息】\n{user_persona}")

    now_str = datetime.now().strftime("%Y年%m月%d日 %H:%M")
    system_parts.append(f"【当前时间】{now_str}")

    system_parts.append(ANNOTATE_PROMPT_TEMPLATE.format(user_name=user_name, book_title=book_title))

    if prev_summaries:
        ctx = "\n".join(f"- {s}" for s in prev_summaries)
        system_parts.append(f"【之前章节摘要（供参考）】\n{ctx}")

    if chat_context:
        chat_lines = []
        for m in chat_context:
            role_label = connor_name if m['role'] == 'assistant' else user_name
            chat_lines.append(f"{role_label}: {m['content']}")
        system_parts.append(f"【你和{user_name}最近的聊天（供参考，了解当前状态和心情）】\n" + "\n".join(chat_lines))

    messages = [
        {"role": "user", "content": "\n\n".join(system_parts)},
        {"role": "assistant", "content": f"好的，我{connor_name}来认真读这段内容，然后给出我的批注～"},
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


async def _save_annotations(book_id: str, ch_idx: int, seg_idx: int, result: dict,
                            annotator: str = 'aion', book_title: str = '', ch_title: str = ''):
    """保存批注，覆盖合并策略，区分 annotator；同时将 summary 写入向量记忆库供 RAG 召回"""
    now = time.time()
    async with get_db() as db:
        db.row_factory = _row_dict
        # 检查是否已有旧批注（同 annotator）
        row = await db.execute("""
            SELECT annotations FROM book_annotations
            WHERE book_id = ? AND chapter_index = ? AND segment_index = ? AND COALESCE(annotator, 'aion') = ?
        """, (book_id, ch_idx, seg_idx, annotator))
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
                WHERE book_id = ? AND chapter_index = ? AND segment_index = ? AND COALESCE(annotator, 'aion') = ?
            """, (json.dumps(final_annotations, ensure_ascii=False),
                  result['summary'], now, book_id, ch_idx, seg_idx, annotator))
        else:
            await db.execute("""
                INSERT INTO book_annotations (book_id, chapter_index, segment_index,
                    annotations, summary, created_at, updated_at, annotator)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (book_id, ch_idx, seg_idx,
                  json.dumps(new_annotations, ensure_ascii=False),
                  result['summary'], now, now, annotator))

        await db.commit()

    # 将 summary 写入向量记忆库
    summary = result.get('summary', '').strip()
    if summary:
        await _sync_book_note_to_memory(book_id, ch_idx, seg_idx, summary,
                                        book_title, ch_title, now)


async def _sync_book_note_to_memory(book_id: str, ch_idx: int, seg_idx: int,
                                     summary: str, book_title: str, ch_title: str,
                                     ts: float):
    """将读书笔记 summary 向量化后写入 memories 表，供 RAG 召回"""
    mem_id = "book_" + hashlib.sha256(f"{book_id}:{ch_idx}:{seg_idx}".encode()).hexdigest()[:12]
    content = f"【读书笔记】《{book_title}》{ch_title}\n{summary}"
    keywords = json.dumps([book_title, ch_title, "读书笔记"], ensure_ascii=False)

    vec = await get_embedding(summary)
    blob = _pack_embedding(vec) if vec else None

    async with get_db() as db:
        await db.execute("""
            INSERT INTO memories (id, content, type, created_at, source_conv, embedding, keywords, importance)
            VALUES (?, ?, 'book_note', ?, NULL, ?, ?, 0.6)
            ON CONFLICT(id) DO UPDATE SET content=excluded.content, embedding=excluded.embedding, created_at=excluded.created_at
        """, (mem_id, content, ts, blob, keywords))
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
