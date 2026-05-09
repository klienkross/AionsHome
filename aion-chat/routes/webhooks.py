"""
Webhook 接收端点 — MacroDroid / Tasker / 外部脚本推送状态到所有客户端

扩展方式：
  1. 在任意模块写一个 async def handler(payload: dict) -> dict 函数
  2. 在下方 _HANDLERS 字典里注册 channel → handler 映射
  handler 返回 {"triggered": True/False, ...}，需自行处理 AI 回复等逻辑
"""
import json, time, logging
from fastapi import APIRouter, Request, Header, HTTPException, Query
from ws import manager
from config import SETTINGS, save_settings, DATA_DIR
from webhook_ai import handle_night_activity, _is_night, trigger_ai_reply
from sensor import handle_sensor_event

router = APIRouter(prefix="/api/webhooks", tags=["webhooks"])
log = logging.getLogger("webhooks")

WEBHOOK_LOG_PATH = DATA_DIR / "webhook_log.jsonl"

# ── channel → handler 映射 ─────────────────────────
# 每个 handler 签名为: async (payload: dict) -> dict
# 未来新增 channel 只需在这注册一行即可
_HANDLERS = {
    "phone-activity": handle_night_activity,
    "sensor": handle_sensor_event,
}


def _get_token() -> str:
    return SETTINGS.get("webhook_token", "")


@router.post("/{channel}")
async def receive_webhook(
    channel: str,
    request: Request,
    token: str = Query(None),
    x_webhook_token: str = Header(None),
):
    """接收外部 webhook 推送 → 广播 + 按 channel 分发 handler"""
    expected = _get_token()
    provided = token or x_webhook_token
    if expected and provided != expected:
        raise HTTPException(403, "invalid token")

    body = await request.json()
    payload = {
        "type": "webhook",
        "channel": channel,
        "payload": body,
        "received_at": time.time(),
    }

    # 广播原始事件给所有客户端
    await manager.broadcast(payload)

    # 日志
    try:
        with open(WEBHOOK_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception:
        pass

    # 分发到对应 handler
    handler_result = None
    handler = _HANDLERS.get(channel)
    if handler:
        try:
            handler_result = await handler(body)
        except Exception as e:
            log.warning("handler [%s] 异常: %s", channel, e)
            handler_result = {"triggered": False, "error": str(e)}

    return {
        "ok": True,
        "channel": channel,
        "handler": handler_result or {},
    }


@router.get("/ntfy-status")
async def ntfy_status():
    from ntfy_bridge import get_status
    return get_status()


@router.get("/night-status")
async def night_status():
    return {
        "is_night": _is_night(),
        "night_start": SETTINGS.get("night_start_hour", 23),
        "night_end": SETTINGS.get("night_end_hour", 7),
    }


@router.get("/token")
async def get_webhook_token():
    t = _get_token()
    if len(t) > 4:
        return {"token": t[:2] + "*" * (len(t) - 4) + t[-2:], "has_token": True}
    return {"token": t, "has_token": bool(t)}


@router.put("/token")
async def set_webhook_token(request: Request):
    body = await request.json()
    new_token = body.get("token", "").strip()
    SETTINGS["webhook_token"] = new_token
    save_settings(SETTINGS)
    return {"ok": True, "has_token": bool(new_token)}
