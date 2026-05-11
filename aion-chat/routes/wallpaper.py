"""
动态壁纸 API 路由
"""

import json, os
from pathlib import Path
from fastapi import APIRouter, UploadFile, File
from config import PUBLIC_DIR, DATA_DIR

router = APIRouter(prefix="/api/wallpaper", tags=["wallpaper"])

WALLPAPER_DIR = PUBLIC_DIR / "wallpaper"
WALLPAPER_DIR.mkdir(parents=True, exist_ok=True)
CONFIG_PATH = DATA_DIR / "wallpaper_config.json"

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
VIDEO_EXTS = {".mp4", ".webm", ".mkv", ".mov"}


def _load_config() -> dict:
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"interval": 30, "files": {}}


def _save_config(cfg: dict):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


@router.get("/files")
async def api_list_files():
    """列出壁纸目录下所有图片/视频"""
    items = []
    for p in sorted(WALLPAPER_DIR.iterdir()):
        ext = p.suffix.lower()
        if ext in IMAGE_EXTS:
            items.append({"name": p.name, "type": "image"})
        elif ext in VIDEO_EXTS:
            items.append({"name": p.name, "type": "video"})
    return {"ok": True, "files": items}


@router.get("/config")
async def api_get_config():
    """读取壁纸配置"""
    return {"ok": True, "config": _load_config()}


@router.post("/config")
async def api_save_config(body: dict):
    """保存壁纸配置"""
    _save_config(body)
    return {"ok": True}


@router.post("/upload")
async def api_upload(file: UploadFile = File(...)):
    """上传壁纸文件"""
    ext = Path(file.filename).suffix.lower()
    if ext not in IMAGE_EXTS and ext not in VIDEO_EXTS:
        return {"ok": False, "message": "不支持的文件格式"}
    dest = WALLPAPER_DIR / file.filename
    content = await file.read()
    with open(dest, "wb") as f:
        f.write(content)
    ftype = "image" if ext in IMAGE_EXTS else "video"
    return {"ok": True, "name": file.filename, "type": ftype}


@router.delete("/file/{filename}")
async def api_delete_file(filename: str):
    """删除壁纸文件"""
    target = WALLPAPER_DIR / filename
    if not target.exists() or not target.is_file():
        return {"ok": False, "message": "文件不存在"}
    # 安全检查：确保在壁纸目录内
    try:
        target.resolve().relative_to(WALLPAPER_DIR.resolve())
    except ValueError:
        return {"ok": False, "message": "非法路径"}
    os.remove(target)
    return {"ok": True}
