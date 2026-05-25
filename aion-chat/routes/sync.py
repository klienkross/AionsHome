"""同步 API：push/pull/status 端点"""

from fastapi import APIRouter
from config import is_sync_configured, get_sync_config

router = APIRouter()


@router.post("/api/sync/push")
async def api_sync_push():
    """推送本地增量到云端 GitHub 仓库。"""
    if not is_sync_configured():
        return {"ok": False, "error": "Sync not configured. Set github_sync_token and sync_repo in settings."}
    from sync_engine import sync_push
    return await sync_push()


@router.post("/api/sync/pull")
async def api_sync_pull():
    """从云端拉取增量到本地。"""
    if not is_sync_configured():
        return {"ok": False, "error": "Sync not configured. Set github_sync_token and sync_repo in settings."}
    from sync_engine import sync_pull
    return await sync_pull()


@router.get("/api/sync/status")
async def api_sync_status():
    """返回同步配置状态和设备信息。"""
    configured = is_sync_configured()
    if not configured:
        return {"configured": False, "device_id": None}
    cfg = get_sync_config()
    return {
        "configured": True,
        "device_id": cfg["device_id"],
        "device_name": cfg["device_name"],
        "sync_repo": cfg["sync_repo"],
    }
