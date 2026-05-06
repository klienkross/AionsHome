"""
书籍解析模块 — EPUB/PDF 导入、章节拆分、段落标注、图片提取
"""

import hashlib, json, os, re, shutil, uuid
from pathlib import Path
from typing import List, Optional, Tuple

import ebooklib
from ebooklib import epub
import fitz  # pymupdf — PDF 解析
from bs4 import BeautifulSoup, NavigableString, Tag, XMLParsedAsHTMLWarning
import warnings
warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

from config import DATA_DIR

# ── 路径 ──────────────────────────────────────────
BOOKS_DIR = DATA_DIR / "books"
BOOKS_DIR.mkdir(exist_ok=True)

# 单段最大用于 AI 批注的字数
SEGMENT_MAX_CHARS = 5000


# ── 工具函数 ──────────────────────────────────────

def _safe_text(s: str) -> str:
    """去除多余空白，保留单个换行"""
    s = re.sub(r'[ \t]+', ' ', s)
    s = re.sub(r'\n{3,}', '\n\n', s)
    return s.strip()


def _hash_id(book_id: str, ch_idx: int, p_idx: int) -> str:
    """生成段落级唯一 ID"""
    raw = f"{book_id}:{ch_idx}:{p_idx}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


# ── EPUB 解析 ─────────────────────────────────────

class ParsedChapter:
    __slots__ = ('index', 'title', 'html_content', 'text_content',
                 'paragraphs', 'char_count', 'segments_meta')

    def __init__(self, index: int, title: str, html_content: str,
                 text_content: str, paragraphs: list, char_count: int,
                 segments_meta: list):
        self.index = index
        self.title = title
        self.html_content = html_content
        self.text_content = text_content
        self.paragraphs = paragraphs
        self.char_count = char_count
        self.segments_meta = segments_meta


class ParsedBook:
    __slots__ = ('book_id', 'title', 'author', 'cover_path', 'chapters')

    def __init__(self):
        self.book_id: str = str(uuid.uuid4())
        self.title: str = ""
        self.author: str = ""
        self.cover_path: Optional[str] = None
        self.chapters: List[ParsedChapter] = []


def parse_epub(epub_path: str) -> ParsedBook:
    """
    解析 EPUB 文件，返回结构化的书籍数据。
    - 图片提取到 data/books/{book_id}/images/
    - HTML 中图片路径重写为 /api/books/{book_id}/images/{filename}
    - 纯文本版本完全剥离图片
    """
    book_obj = epub.read_epub(epub_path, options={"ignore_ncx": True})
    result = ParsedBook()
    book_id = result.book_id

    # ── 书籍元数据 ──
    result.title = _get_metadata(book_obj, 'title') or Path(epub_path).stem
    result.author = _get_metadata(book_obj, 'creator') or "未知作者"

    # ── 创建图片目录 ──
    img_dir = BOOKS_DIR / book_id / "images"
    img_dir.mkdir(parents=True, exist_ok=True)

    # ── 提取所有图片到磁盘，建立 EPUB 内部路径 → 服务端文件名 映射 ──
    img_map = {}  # epub_internal_href -> saved_filename
    for item in book_obj.get_items():
        if item.get_type() == ebooklib.ITEM_IMAGE or \
           (item.media_type and item.media_type.startswith('image/')):
            # 保存图片
            fname = Path(item.get_name()).name  # e.g. image00057.jpeg
            # 避免文件名冲突
            if (img_dir / fname).exists():
                base, ext = os.path.splitext(fname)
                fname = f"{base}_{hashlib.md5(item.get_name().encode()).hexdigest()[:6]}{ext}"
            (img_dir / fname).write_bytes(item.get_content())
            # 记录映射（EPUB 中可能用相对路径引用）
            img_map[item.get_name()] = fname
            # 也映射不带目录前缀的文件名
            img_map[Path(item.get_name()).name] = fname

    # ── 提取封面 ──
    result.cover_path = _extract_cover(book_obj, img_map, book_id)

    # ── 按 spine 顺序提取章节 ──
    spine_ids = [s[0] for s in book_obj.spine]
    id_to_item = {item.get_id(): item for item in book_obj.get_items()}

    ch_idx = 0
    for spine_id in spine_ids:
        item = id_to_item.get(spine_id)
        if not item or item.get_type() != ebooklib.ITEM_DOCUMENT:
            continue

        raw_html = item.get_content().decode('utf-8', errors='replace')
        soup = BeautifulSoup(raw_html, 'lxml')
        body = soup.find('body')
        if not body:
            continue

        # 提取章节标题
        ch_title = _extract_chapter_title(body, ch_idx)

        # 提取段落（纯文本列表）+ 带图片的 HTML
        paragraphs, html_content = _extract_paragraphs(
            body, img_map, book_id, item.get_name()
        )

        # 跳过过短的页面（封面/版权页/空白页等）
        text_content = '\n'.join(paragraphs)
        if len(text_content) < 200:
            continue

        # 计算分段元数据
        segments_meta = _compute_segments(paragraphs)

        chapter = ParsedChapter(
            index=ch_idx,
            title=ch_title,
            html_content=html_content,
            text_content=text_content,
            paragraphs=paragraphs,
            char_count=len(text_content),
            segments_meta=segments_meta,
        )
        result.chapters.append(chapter)
        ch_idx += 1

    return result


def parse_pdf(pdf_path: str) -> ParsedBook:
    """
    解析 PDF 文件，返回与 parse_epub 相同结构的 ParsedBook。
    - 优先使用 PDF 内置目录（TOC）划分章节
    - 无 TOC 时按页数分组（~15 页/章）
    - 图片提取到 data/books/{book_id}/images/
    """
    doc = fitz.open(pdf_path)
    result = ParsedBook()
    book_id = result.book_id

    # ── 元数据 ──
    meta = doc.metadata
    result.title = (meta.get("title") or "").strip() or Path(pdf_path).stem
    result.author = (meta.get("author") or "").strip() or "未知作者"

    # ── 图片目录 ──
    img_dir = BOOKS_DIR / book_id / "images"
    img_dir.mkdir(parents=True, exist_ok=True)

    # ── 提取所有嵌入图片 ──
    img_map = {}  # xref -> saved_filename
    for page in doc:
        for img_info in page.get_images(full=True):
            xref = img_info[0]
            if xref in img_map:
                continue
            try:
                base = doc.extract_image(xref)
                if not base:
                    continue
                ext = base["ext"]
                fname = f"img_{xref}.{ext}"
                (img_dir / fname).write_bytes(base["image"])
                img_map[xref] = fname
            except Exception:
                pass

    # ── 提取封面 ──
    result.cover_path = _extract_pdf_cover(doc, img_map, book_id)

    # ── 章节边界 ──
    toc = doc.get_toc(simple=False)
    chapter_entries = _resolve_pdf_chapters(doc, toc)

    # ── 逐章提取 ──
    for ch_idx, ch in enumerate(chapter_entries):
        paragraphs, html_content = _extract_pdf_chapter(
            doc, ch["start_page"], ch["end_page"], img_map, book_id
        )

        text_content = '\n'.join(paragraphs)
        if len(text_content) < 200:
            continue

        segments_meta = _compute_segments(paragraphs)

        chapter = ParsedChapter(
            index=ch_idx,
            title=ch["title"],
            html_content=html_content,
            text_content=text_content,
            paragraphs=paragraphs,
            char_count=len(text_content),
            segments_meta=segments_meta,
        )
        result.chapters.append(chapter)

    doc.close()
    return result


def _resolve_pdf_chapters(doc: fitz.Document, toc: list) -> list:
    """
    解析 PDF 章节边界，返回 [{"title": ..., "start_page": 0-based, "end_page": 0-based}, ...]
    """
    total_pages = doc.page_count

    if toc and len(toc) >= 2:
        chapters = []
        for entry in toc:
            # entry is (level, title, page, dest_dict)
            level = entry[0]
            title = entry[1].strip() if entry[1] else ""
            page_num = max(0, int(entry[2]) - 1)  # convert to 0-based
            if not title:
                continue
            chapters.append({"title": title, "start_page": page_num})

        if not chapters:
            return _fallback_page_chapters(total_pages)

        # dedup by page & compute end_page
        seen_pages = set()
        deduped = []
        for ch in chapters:
            sp = ch["start_page"]
            if sp in seen_pages:
                continue
            seen_pages.add(sp)
            deduped.append(ch)
        deduped.sort(key=lambda x: x["start_page"])

        for i, ch in enumerate(deduped):
            if i + 1 < len(deduped):
                ch["end_page"] = max(ch["start_page"], deduped[i + 1]["start_page"] - 1)
            else:
                ch["end_page"] = total_pages - 1

        return deduped

    return _fallback_page_chapters(total_pages)


def _fallback_page_chapters(total_pages: int, pages_per_chapter: int = 15) -> list:
    chapters = []
    for start in range(0, total_pages, pages_per_chapter):
        end = min(start + pages_per_chapter, total_pages) - 1
        chapters.append({
            "title": f"第 {len(chapters) + 1} 章",
            "start_page": start,
            "end_page": end,
        })
    return chapters


def _extract_pdf_chapter(doc: fitz.Document, start_page: int, end_page: int,
                         img_map: dict, book_id: str) -> Tuple[list, str]:
    """提取指定页面范围的段落和 HTML"""
    paragraphs = []
    html_parts = []

    for page_num in range(start_page, end_page + 1):
        page = doc[page_num]
        blocks = page.get_text("blocks")  # list of (x0, y0, x1, y1, text, block_no, block_type)

        # 按 y 坐标排序（从上到下）
        text_blocks = [b for b in blocks if b[6] == 0 and b[4].strip()]
        text_blocks.sort(key=lambda b: (b[1], b[0]))  # y 优先，x 次之

        for block in text_blocks:
            text = _safe_text(block[4])
            if text and len(text) > 5:
                p_idx = len(paragraphs)
                paragraphs.append(text)
                html_parts.append(f'<p data-p="{p_idx}">{_html_escape(text)}</p>')

        # 页面图片
        for img_info in page.get_images(full=True):
            xref = img_info[0]
            fname = img_map.get(xref)
            if fname:
                html_parts.append(
                    f'<div class="book-img"><img src="/api/books/{book_id}/images/{fname}" '
                    f'alt="page {page_num+1}" loading="lazy"></div>'
                )

    html_content = '\n'.join(html_parts)
    return paragraphs, html_content


def _extract_pdf_cover(doc: fitz.Document, img_map: dict, book_id: str) -> Optional[str]:
    """提取 PDF 封面：优先第一页渲染图，其次第一页嵌入图"""
    # 方法1：渲染首页为图片
    try:
        cover_dir = BOOKS_DIR / book_id / "images"
        pix = doc[0].get_pixmap(dpi=150)
        cover_path = cover_dir / "cover.jpg"
        pix.save(cover_path)
        return f"/api/books/{book_id}/images/cover.jpg"
    except Exception:
        pass

    # 方法2：首页第一张嵌入图
    try:
        for img_info in doc[0].get_images(full=True):
            fname = img_map.get(img_info[0])
            if fname:
                return f"/api/books/{book_id}/images/{fname}"
    except Exception:
        pass

    return None


def _get_metadata(book_obj, field: str) -> Optional[str]:
    """安全提取 EPUB 元数据"""
    try:
        values = book_obj.get_metadata('DC', field)
        if values:
            return values[0][0]
    except:
        pass
    return None


def _extract_cover(book_obj, img_map: dict, book_id: str) -> Optional[str]:
    """尝试提取封面图片路径"""
    # 方法1: OPF metadata 中的 cover
    cover_id = None
    try:
        meta = book_obj.get_metadata('OPF', 'cover')
        if meta:
            cover_id = meta[0][1].get('content', '')
    except:
        pass

    if cover_id:
        for item in book_obj.get_items():
            if item.get_id() == cover_id:
                fname = img_map.get(item.get_name()) or img_map.get(Path(item.get_name()).name)
                if fname:
                    return f"/api/books/{book_id}/images/{fname}"

    # 方法2: 找名字含 cover 的图片
    for epub_path_key, fname in img_map.items():
        if 'cover' in epub_path_key.lower():
            return f"/api/books/{book_id}/images/{fname}"

    return None


def _extract_chapter_title(body: Tag, ch_idx: int) -> str:
    """从 body 中提取章节标题"""
    for tag_name in ['h1', 'h2', 'h3']:
        h = body.find(tag_name)
        if h and h.get_text(strip=True):
            return h.get_text(strip=True)
    # fallback: 第一个有文字的 p 或 div
    for p in body.find_all(['p', 'div'], limit=3):
        txt = p.get_text(strip=True)
        if txt and len(txt) < 50:
            return txt
    return f"第 {ch_idx + 1} 章"


def _resolve_img_src(src: str, img_map: dict, book_id: str, item_name: str) -> Optional[str]:
    """将 EPUB 内部的 img src 解析为服务端 URL"""
    if not src:
        return None
    # 尝试直接匹配文件名
    basename = Path(src.split('?')[0].split('#')[0]).name
    fname = img_map.get(basename)
    if fname:
        return f"/api/books/{book_id}/images/{fname}"

    # 尝试相对路径解析
    try:
        item_dir = str(Path(item_name).parent)
        if item_dir == '.':
            resolved = src
        else:
            resolved = str(Path(item_dir) / src)
        # 标准化路径
        resolved = resolved.replace('\\', '/')
        if resolved.startswith('/'):
            resolved = resolved[1:]
        fname = img_map.get(resolved) or img_map.get(Path(resolved).name)
        if fname:
            return f"/api/books/{book_id}/images/{fname}"
    except:
        pass

    return None


def _extract_paragraphs(body: Tag, img_map: dict, book_id: str,
                         item_name: str) -> Tuple[List[str], str]:
    """
    从 body 提取：
    1. paragraphs: 纯文字段落列表（发给 AI 用）
    2. html_content: 保留图片的 HTML（阅读器用）

    策略：遍历 body 下所有 block 级元素，文字提取为段落，图片保留在 HTML 中。
    """
    paragraphs = []     # 纯文字段落
    html_parts = []     # 阅读器 HTML 片段

    # 收集所有顶级 block 元素
    blocks = body.find_all(['p', 'div', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
                            'blockquote', 'pre', 'figure', 'section'], recursive=False)
    if not blocks:
        # fallback: body 下所有 p
        blocks = body.find_all('p')
    if not blocks:
        # 最终 fallback: 把 body 当作一整块
        blocks = [body]

    for block in blocks:
        # 检查是否含有图片
        imgs = block.find_all('img')
        svgs = block.find_all('svg')

        # 提取纯文字
        text = block.get_text(separator=' ', strip=True)
        text = _safe_text(text)

        if text and len(text) > 5:
            p_idx = len(paragraphs)
            paragraphs.append(text)
            # HTML 版本：给段落打上 data-p 标记
            html_parts.append(f'<p data-p="{p_idx}">{_html_escape(text)}</p>')

        # 处理图片（保留在 HTML 中，不加入纯文本）
        for img in imgs:
            src = img.get('src', '') or img.get('data-src', '')
            new_src = _resolve_img_src(src, img_map, book_id, item_name)
            if new_src:
                alt = img.get('alt', '')
                html_parts.append(
                    f'<div class="book-img"><img src="{new_src}" alt="{_html_escape(alt)}" loading="lazy"></div>'
                )

    # 如果段落为零但有文字，做最终尝试
    if not paragraphs:
        full_text = body.get_text(separator='\n', strip=True)
        for line in full_text.split('\n'):
            line = _safe_text(line)
            if line and len(line) > 5:
                p_idx = len(paragraphs)
                paragraphs.append(line)
                html_parts.append(f'<p data-p="{p_idx}">{_html_escape(line)}</p>')

    html_content = '\n'.join(html_parts)
    return paragraphs, html_content


def _html_escape(s: str) -> str:
    """基础 HTML 转义"""
    return s.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('"', '&quot;')


# ── 分段计算（5000 字/段）──────────────────────────

def _compute_segments(paragraphs: List[str]) -> list:
    """
    按 ≤5000 字切分段落为多个 segment，保证在段落边界切分。
    返回: [{"start_p": 0, "end_p": 12, "char_count": 4800, "status": "not_sent"}, ...]
    """
    segments = []
    total = len(paragraphs)
    if total == 0:
        return segments

    start_p = 0
    current_chars = 0

    for i, p in enumerate(paragraphs):
        current_chars += len(p)

        # 到达最后一段 或 累计超过阈值
        is_last = (i == total - 1)
        if current_chars >= SEGMENT_MAX_CHARS or is_last:
            segments.append({
                "start_p": start_p,
                "end_p": i,
                "char_count": current_chars,
                "status": "not_sent"
            })
            start_p = i + 1
            current_chars = 0

    # 如果最后一段太短（< 500 字），合并到前一段
    if len(segments) >= 2 and segments[-1]["char_count"] < 500:
        last = segments.pop()
        segments[-1]["end_p"] = last["end_p"]
        segments[-1]["char_count"] += last["char_count"]

    return segments


def build_annotate_text(paragraphs: List[str], start_p: int, end_p: int) -> str:
    """
    构建发送给 AI 批注的段落文本，每段带 【P{n}】 编号标记。
    """
    lines = []
    for i in range(start_p, end_p + 1):
        if i < len(paragraphs):
            lines.append(f"【P{i}】{paragraphs[i]}")
    return '\n\n'.join(lines)


def delete_book_files(book_id: str):
    """删除书籍的所有本地文件"""
    book_dir = BOOKS_DIR / book_id
    if book_dir.exists():
        shutil.rmtree(book_dir, ignore_errors=True)
