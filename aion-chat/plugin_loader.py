"""
插件自动发现：扫描 routes/ 注册 API 路由，扫描 static/*.html 注册页面路由
通过 settings.disabled_modules 控制功能开关
"""

import importlib, logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse

from config import BASE_DIR, SETTINGS

logger = logging.getLogger("plugin_loader")

# ── 映射表 ───────────────────────────────────────────
# 页面文件名(stem) → 路由模块名（仅列名称不一致的）
_PAGE_MODULE_MAP = {
    "camera": "cam",
    "memory": "memories",
    "activity-logs": "activity",
    "monitor-logs": "cam",
    "reading": "book",
    "heart-whispers": "heart_whispers",
}

# 页面文件名 → URL 路径（仅列不一致的）
_PAGE_PATH_OVERRIDE = {
    "home": "/",
}

# 核心模块，不可禁用
CORE_MODULES = {
    "chat", "chatroom", "settings", "files",
    "memories", "sync", "webhooks", "heart_whispers",
}

# 无对应路由模块的独立页面，不受模块开关影响
_STANDALONE_PAGES = {"home", "worldbook", "pet"}


# ── 内部工具 ─────────────────────────────────────────
def _disabled() -> set:
    return set(SETTINGS.get("disabled_modules", []))


def _is_enabled(module_name: str) -> bool:
    if module_name in CORE_MODULES:
        return True
    return module_name not in _disabled()


# ── 路由自动发现 ─────────────────────────────────────
def discover_routers(app: FastAPI):
    """扫描 routes/*.py，自动 include_router（跳过已禁用模块）"""
    routes_dir = BASE_DIR / "routes"
    registered = []
    for py in sorted(routes_dir.glob("*.py")):
        name = py.stem
        if name.startswith("_"):
            continue
        if not _is_enabled(name):
            logger.info("跳过已禁用路由: %s", name)
            continue
        try:
            mod = importlib.import_module(f"routes.{name}")
            router = getattr(mod, "router", None)
            if router:
                app.include_router(router)
                registered.append(name)
        except Exception as e:
            logger.warning("加载 routes.%s 失败: %s", name, e)
    logger.info("已注册 %d 个路由模块", len(registered))


# ── 页面自动发现 ─────────────────────────────────────
def discover_pages(app: FastAPI):
    """扫描 static/*.html，自动注册页面 GET 路由（跳过已禁用模块对应的页面）"""
    static_dir = BASE_DIR / "static"
    registered = []
    for html in sorted(static_dir.glob("*.html")):
        page = html.stem
        module_name = _PAGE_MODULE_MAP.get(page, page.replace("-", "_"))
        if page not in _STANDALONE_PAGES and not _is_enabled(module_name):
            logger.info("跳过已禁用页面: /%s", page)
            continue
        url = _PAGE_PATH_OVERRIDE.get(page, f"/{page}")
        fp = html

        def _make_handler(file_path=fp):
            async def handler():
                return FileResponse(file_path, headers={
                    "Cache-Control": "no-cache, no-store, must-revalidate",
                })
            return handler

        app.get(url)(_make_handler())
        registered.append(url)
    logger.info("已注册 %d 个页面路由", len(registered))
