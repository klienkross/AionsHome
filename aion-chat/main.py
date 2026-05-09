"""
Aion Chat — 入口文件
FastAPI app 创建、lifespan、静态文件挂载、路由注册
"""

import asyncio, json, logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles

# 过滤高频轮询路径的 access log，避免淹没有用的日志
class _QuietCamFilter(logging.Filter):
    _noisy = ("/api/cam/frame", "/api/cam/status")
    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        return not any(p in msg for p in self._noisy)

logging.getLogger("uvicorn.access").addFilter(_QuietCamFilter())
from fastapi.responses import FileResponse, HTMLResponse

from config import BASE_DIR, PUBLIC_DIR, UPLOADS_DIR, SCREENSHOTS_DIR, load_cam_config, SETTINGS, save_settings
from database import init_db, get_db
from ws import manager
from reading import ReadingSession, get_session
from camera import cam
from voice import voice
from schedule import schedule_mgr

from routes import chat, cam as cam_routes, files, settings, memories
from routes import voice as voice_routes
from routes import music as music_routes
from routes import schedule as schedule_routes
from routes import location as location_routes
from routes import heart_whispers as heart_whispers_routes
from routes import activity as activity_routes
from routes import book as book_routes
from routes import theater as theater_routes
from routes import ghost_forest as ghost_forest_routes
from routes import gift as gift_routes
from routes import webhooks as webhooks_routes
from activity import pc_tracker
# from memory import auto_digest  # V1
from digest_v2 import auto_digest_v2 as auto_digest


# ── 自动记忆总结定时任务 ──────────────────────────
async def _auto_digest_loop():
    """每 30 分钟检查一次，若用户已 30 分钟未发消息则自动总结"""
    import aiosqlite, time as _time
    while True:
        await asyncio.sleep(30 * 60)  # 30 分钟
        try:
            # 检查最后一条用户消息的时间
            async with get_db() as db:
                db.row_factory = aiosqlite.Row
                cur = await db.execute(
                    "SELECT created_at FROM messages WHERE role='user' ORDER BY created_at DESC LIMIT 1"
                )
                row = await cur.fetchone()
            if not row:
                continue
            elapsed = _time.time() - row["created_at"]
            if elapsed < 30 * 60:
                print(f"[auto_digest] 用户 {elapsed/60:.0f} 分钟前仍在对话，跳过")
                continue
            print(f"[auto_digest] 用户已 {elapsed/60:.0f} 分钟未对话，开始自动总结")
            result = await auto_digest()
            print(f"[auto_digest] {result.get('message', '')}")
        except asyncio.CancelledError:
            break
        except Exception as e:
            print(f"[auto_digest] ❌ 异常: {e}")


def _print_local_ips():
    import psutil, socket
    print("\n╔══ 本机 IP 地址 ══════════════════════════════╗")
    for name, addrs in psutil.net_if_addrs().items():
        for a in addrs:
            if a.family == socket.AF_INET and not a.address.startswith("127."):
                print(f"║  {name} — {a.address}")
    print("╚══════════════════════════════════════════════╝\n")


@asynccontextmanager
async def lifespan(app: FastAPI):
    _print_local_ips()
    await init_db()
    loop = asyncio.get_event_loop()
    cam.set_event_loop(loop)
    cam_cfg = load_cam_config()
    if cam_cfg.get("monitor_enabled"):
        cam.open_camera(cam_cfg["camera_index"])
        cam.start_monitoring()
    # 语音模块初始化
    voice.set_event_loop(loop)
    voice.set_ws_manager(manager)
    # 日程/闹铃模块初始化
    schedule_mgr.set_event_loop(loop)
    schedule_mgr.start()
    # 传感器模块初始化
    import sensor
    sensor.set_event_loop(loop)
    # ntfy.sh 公网中转桥接
    import ntfy_bridge
    ntfy_bridge.start(loop)
    # PC 活动采集
    pc_tracker.set_event_loop(loop)
    try:
        pc_tracker.start()
    except Exception as e:
        print(f"[PCActivity] ❌ 启动异常: {e}")
    # 自动记忆总结定时任务
    digest_task = asyncio.create_task(_auto_digest_loop())
    # WS 心跳清理
    manager.start_heartbeat()
    yield
    digest_task.cancel()
    ntfy_bridge.stop()
    pc_tracker.stop()
    schedule_mgr.stop()
    voice.stop()
    cam.close_camera()


app = FastAPI(lifespan=lifespan)

# 全局禁用静态文件缓存
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

_CACHEABLE_PREFIXES = ("/uploads/", "/public/", "/screenshots/")

class NoCacheStaticMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        path = request.url.path
        if path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        elif path.startswith(_CACHEABLE_PREFIXES):
            response.headers["Cache-Control"] = "public, max-age=604800, immutable"
        return response

app.add_middleware(NoCacheStaticMiddleware)

# 静态文件
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
app.mount("/uploads", StaticFiles(directory=str(UPLOADS_DIR)), name="uploads")
app.mount("/public", StaticFiles(directory=str(PUBLIC_DIR)), name="public")
app.mount("/screenshots", StaticFiles(directory=str(SCREENSHOTS_DIR)), name="screenshots")

# 路由
app.include_router(chat.router)
app.include_router(cam_routes.router)
app.include_router(files.router)
app.include_router(settings.router)
app.include_router(memories.router)
app.include_router(voice_routes.router)
app.include_router(music_routes.router)
app.include_router(schedule_routes.router)
app.include_router(location_routes.router)
app.include_router(heart_whispers_routes.router)
app.include_router(activity_routes.router)
app.include_router(book_routes.router)
app.include_router(theater_routes.router)
app.include_router(ghost_forest_routes.router)
app.include_router(gift_routes.router)
app.include_router(webhooks_routes.router)

# ── reading 辅助函数 ──────────────────────────────

def _reading_sessions_for_ws(ws):
    """Find active reading sessions initiated by this WS connection."""
    from reading import _sessions
    return [s for s in _sessions.values() if s._ws is ws]


# 页面
@app.get("/")
async def home():
    return FileResponse(BASE_DIR / "static" / "home.html", headers={"Cache-Control": "no-cache, no-store, must-revalidate"})

@app.get("/chat")
async def chat_page():
    return FileResponse(BASE_DIR / "static" / "chat.html", headers={"Cache-Control": "no-cache, no-store, must-revalidate"})

@app.get("/settings")
async def settings_page():
    return FileResponse(BASE_DIR / "static" / "settings.html")

@app.get("/worldbook")
async def worldbook_page():
    return FileResponse(BASE_DIR / "static" / "worldbook.html")

@app.get("/memory")
async def memory_page():
    return FileResponse(BASE_DIR / "static" / "memory.html")

@app.get("/schedule")
async def schedule_page():
    return FileResponse(BASE_DIR / "static" / "schedule.html")

@app.get("/camera")
async def camera_page():
    return FileResponse(BASE_DIR / "static" / "camera.html")

@app.get("/monitor-logs")
async def monitor_logs_page():
    return FileResponse(BASE_DIR / "static" / "monitor-logs.html")

@app.get("/location")
async def location_page():
    return FileResponse(BASE_DIR / "static" / "location.html")

@app.get("/heart-whispers")
async def heart_whispers_page():
    return FileResponse(BASE_DIR / "static" / "heart-whispers.html")

@app.get("/activity-logs")
async def activity_logs_page():
    return FileResponse(BASE_DIR / "static" / "activity-logs.html")

@app.get("/reading")
async def reading_page():
    return FileResponse(BASE_DIR / "static" / "reading.html")

@app.get("/theater")
async def theater_page():
    return FileResponse(BASE_DIR / "static" / "theater.html", headers={"Cache-Control": "no-cache, no-store, must-revalidate"})

@app.get("/ghost-forest")
async def ghost_forest_page():
    return FileResponse(BASE_DIR / "static" / "ghost-forest.html", headers={"Cache-Control": "no-cache, no-store, must-revalidate"})

@app.get("/gift")
async def gift_page():
    return FileResponse(BASE_DIR / "static" / "gift.html", headers={"Cache-Control": "no-cache, no-store, must-revalidate"})

# PWA：Service Worker 必须从根路径提供，作用域才能覆盖所有页面
@app.get("/sw.js")
async def service_worker():
    return FileResponse(BASE_DIR / "static" / "sw.js", media_type="application/javascript")

@app.get("/manifest.json")
async def manifest():
    return FileResponse(BASE_DIR / "static" / "manifest.json", media_type="application/manifest+json")

# WebSocket
@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await manager.connect(ws)
    try:
        while True:
            text = await ws.receive_text()
            try:
                msg = json.loads(text)
                msg_type = msg.get("type")
                if msg_type == "ping":
                    await ws.send_text(json.dumps({"type": "pong"}))
                elif msg_type == "pong":
                    manager.record_pong(ws)
                elif msg_type == "tts_state":
                    manager.set_tts_state(ws, msg.get("enabled", False), msg.get("voice", ""))
                    voice = msg.get("voice", "")
                    if voice and SETTINGS.get("tts_voice") != voice:
                        SETTINGS["tts_voice"] = voice
                        save_settings(SETTINGS)
                elif msg_type == "register_client":
                    manager.register_client_id(ws, msg.get("client_id", ""))
                elif msg_type == "reading_start":
                    book_id = msg.get("book_id", "")
                    logging.getLogger("ws").info("reading_start: book=%s ch=%d", book_id, msg.get("chapter_index", 0))
                    if not book_id:
                        await ws.send_text(json.dumps({"type": "reading_error", "message": "缺少 book_id"}))
                    elif get_session(book_id):
                        await ws.send_text(json.dumps({"type": "reading_error", "message": "该书已有活跃朗读会话"}))
                    else:
                        session = ReadingSession(
                            book_id=book_id,
                            chapter_index=msg.get("chapter_index", 0),
                            conv_id=msg.get("conv_id", ""),
                            ws=ws,
                        )
                        asyncio.create_task(session.run())
                elif msg_type == "reading_audio_ended":
                    for s in list(_reading_sessions_for_ws(ws)):
                        s.on_audio_ended()
                elif msg_type == "reading_pause":
                    for s in list(_reading_sessions_for_ws(ws)):
                        s.on_pause()
                elif msg_type == "reading_resume":
                    for s in list(_reading_sessions_for_ws(ws)):
                        s.on_resume()
                elif msg_type == "reading_stop":
                    for s in list(_reading_sessions_for_ws(ws)):
                        s.on_stop()
            except (json.JSONDecodeError, Exception):
                pass
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logging.getLogger("ws").warning("WS endpoint error: %s", e)
    finally:
        for s in list(_reading_sessions_for_ws(ws)):
            s.on_disconnect()
        manager.disconnect(ws)


if __name__ == "__main__":
    import uvicorn
    import sys
    if "--reload" in sys.argv:
        uvicorn.run("main:app", host="0.0.0.0", port=8080, reload=True)
    else:
        uvicorn.run("main:app", host="0.0.0.0", port=8080, reload=False)
