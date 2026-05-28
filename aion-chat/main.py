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

# 静默 Windows asyncio ProactorEventLoop 连接重置的噪音日志
logging.getLogger("asyncio").setLevel(logging.CRITICAL)
from fastapi.responses import FileResponse, HTMLResponse

from config import BASE_DIR, PUBLIC_DIR, UPLOADS_DIR, CODEX_UPLOADS_DIR, SCREENSHOTS_DIR, load_cam_config, SETTINGS, save_settings
from database import init_db, get_db
from ws import manager
from reading import ReadingSession, get_session
from camera import cam
from voice import voice
from schedule import schedule_mgr

from plugin_loader import discover_routers, discover_pages
from activity import pc_tracker
# from memory import auto_digest  # V1
from digest_v2 import auto_digest_v2 as auto_digest
from chatroom import _connor_1v1_auto_digest_loop
from fund import fund_scheduler


# ── 自动记忆总结定时任务 ──────────────────────────
async def _auto_digest_loop():
    """每 30 分钟检查一次，若用户已 30 分钟未发消息（私聊+群聊）则自动总结"""
    import aiosqlite, time as _time
    while True:
        await asyncio.sleep(30 * 60)  # 30 分钟
        try:
            # 检查最后一条用户消息的时间（私聊 + 群聊取最新的）
            latest_ts = 0
            async with get_db() as db:
                db.row_factory = aiosqlite.Row
                cur = await db.execute(
                    "SELECT created_at FROM messages WHERE role='user' ORDER BY created_at DESC LIMIT 1"
                )
                row = await cur.fetchone()
                if row:
                    latest_ts = max(latest_ts, row["created_at"])
                cur = await db.execute(
                    "SELECT created_at FROM chatroom_messages WHERE sender='user' ORDER BY created_at DESC LIMIT 1"
                )
                row = await cur.fetchone()
                if row:
                    latest_ts = max(latest_ts, row["created_at"])
            if latest_ts == 0:
                continue
            elapsed = _time.time() - latest_ts
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
    # 初始化 embedding 矩阵缓存
    import embedding_cache
    await embedding_cache.load()
    print(f"[embedding_cache] Loaded {embedding_cache.count()} vectors")
    loop = asyncio.get_event_loop()
    cam.set_event_loop(loop)
    cam_cfg = load_cam_config()
    if cam_cfg.get("monitor_enabled"):
        if cam_cfg.get("active_source") == "esp32":
            cam.open_esp32()
        else:
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
    # 基金监控定时任务
    fund_scheduler.set_event_loop(loop)
    fund_scheduler.start()
    # 自动记忆总结定时任务
    digest_task = asyncio.create_task(_auto_digest_loop())
    # WS 心跳清理
    manager.start_heartbeat()
    cr_digest_task = asyncio.create_task(_connor_1v1_auto_digest_loop())
    # 遗忘曲线后台衰减任务
    from decay_engine import decay_loop
    decay_task = asyncio.create_task(decay_loop())
    yield
    cr_digest_task.cancel()
    digest_task.cancel()
    decay_task.cancel()
    ntfy_bridge.stop()
    fund_scheduler.stop()
    pc_tracker.stop()
    schedule_mgr.stop()
    voice.stop()
    cam.close_camera()


app = FastAPI(lifespan=lifespan)

# 全局禁用静态文件缓存
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

_LOCAL_PREFIXES = ("127.", "192.168.", "::1", "localhost")

_CACHEABLE_PREFIXES = ("/uploads/", "/public/", "/screenshots/")

class NoCacheStaticMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        # 壁纸大文件只允许本地 IP 访问，远程设备不需要也避免占带宽
        if request.url.path.startswith("/public/wallpaper/"):
            client_ip = request.client.host if request.client else ""
            if not any(client_ip.startswith(p) for p in _LOCAL_PREFIXES):
                return Response("wallpaper only available on local network", status_code=403)
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
app.mount("/cr-uploads", StaticFiles(directory=str(CODEX_UPLOADS_DIR)), name="cr-uploads")
app.mount("/public", StaticFiles(directory=str(PUBLIC_DIR)), name="public")
app.mount("/screenshots", StaticFiles(directory=str(SCREENSHOTS_DIR)), name="screenshots")
app.mount("/aion-pet", StaticFiles(directory=str(BASE_DIR.parent / "AionPet")), name="aion-pet")

# 路由 + 页面自动发现
discover_routers(app)
discover_pages(app)

# ── reading 辅助函数 ──────────────────────────────

def _reading_sessions_for_ws(ws):
    """Find active reading sessions initiated by this WS connection."""
    from reading import _sessions
    return [s for s in _sessions.values() if s._ws is ws]

# PWA：Service Worker 必须从根路径提供，作用域才能覆盖所有页面
@app.get("/sw.js")
async def service_worker():
    return FileResponse(BASE_DIR / "static" / "sw.js", media_type="application/javascript",
                        headers={"Cache-Control": "no-cache"})

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
                elif msg_type == "pet_state":
                    manager.set_pet_state(ws, msg.get("enabled", False))
                elif msg.get("type") == "step_diag":
                    # 手机回传的步数传感器诊断 → 转发给所有浏览器客户端
                    await manager.broadcast(msg, exclude=ws)
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
