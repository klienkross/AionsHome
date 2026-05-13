"""
BLE 广播玩具控制 API 路由
"""

import sys, json, logging
from pathlib import Path
from fastapi import APIRouter
from pydantic import BaseModel
from typing import Optional

from config import SETTINGS, save_settings

import toy_adv

logger = logging.getLogger("routes.toy_adv")
router = APIRouter()

_TC_DIR = str(Path(__file__).resolve().parent.parent.parent.parent / "ToyController")
if _TC_DIR not in sys.path:
    sys.path.insert(0, _TC_DIR)


class ToyAdvConfigUpdate(BaseModel):
    toy_adv_enabled: Optional[bool] = None
    toy_adv_model: Optional[str] = None
    toy_adv_channel: Optional[int] = None
    toy_adv_presets: Optional[list] = None


class ToyAdvTestRequest(BaseModel):
    preset: Optional[int] = None
    mode: Optional[int] = None
    speed: Optional[int] = None


@router.get("/api/toy-adv/devices")
async def list_devices(search: str = ""):
    try:
        from mcp_server import _load_data, _devices
        _load_data()
        result = [d for d in _devices if d.get("isBleDevice")]
        if search:
            q = search.lower()
            result = [d for d in result if q in d.get("displayName", "").lower() or q in d.get("localName", "").lower()]
        return {
            "ok": True,
            "devices": [
                {"name": d.get("displayName", ""), "model": d.get("localName", ""), "deviceType": d.get("deviceType", 0)}
                for d in result[:50]
            ],
        }
    except Exception as e:
        logger.error(f"[toy-adv] list_devices 失败: {e}")
        return {"ok": False, "error": str(e)}


@router.get("/api/toy-adv/waveforms")
async def list_waveforms():
    try:
        from mcp_server import _load_data, _waveforms, _pump_waveforms
        _load_data()
        result = [{"id": i, "name": w.get("name", "")} for i, w in enumerate(_waveforms)]
        result += [{"id": 100 + i, "name": w.get("name", ""), "pump": True} for i, w in enumerate(_pump_waveforms)]
        return {"ok": True, "waveforms": result}
    except Exception as e:
        logger.error(f"[toy-adv] list_waveforms 失败: {e}")
        return {"ok": False, "error": str(e)}


@router.get("/api/toy-adv/config")
async def get_config():
    return {
        "ok": True,
        "toy_adv_enabled": SETTINGS.get("toy_adv_enabled", False),
        "toy_adv_model": SETTINGS.get("toy_adv_model", "K134"),
        "toy_adv_channel": SETTINGS.get("toy_adv_channel", 37),
        "toy_adv_presets": toy_adv.get_preset_map(SETTINGS),
    }


@router.post("/api/toy-adv/config")
async def update_config(body: ToyAdvConfigUpdate):
    updated = {}
    if body.toy_adv_enabled is not None:
        SETTINGS["toy_adv_enabled"] = body.toy_adv_enabled
        updated["toy_adv_enabled"] = body.toy_adv_enabled
    if body.toy_adv_model is not None:
        SETTINGS["toy_adv_model"] = body.toy_adv_model
        updated["toy_adv_model"] = body.toy_adv_model
    if body.toy_adv_channel is not None:
        SETTINGS["toy_adv_channel"] = body.toy_adv_channel
        updated["toy_adv_channel"] = body.toy_adv_channel
    if body.toy_adv_presets is not None:
        SETTINGS["toy_adv_presets"] = body.toy_adv_presets
        updated["toy_adv_presets"] = body.toy_adv_presets
    save_settings(SETTINGS)
    return {"ok": True, "updated": updated}


@router.post("/api/toy-adv/test")
async def test_send(body: ToyAdvTestRequest):
    if not toy_adv.is_available():
        return {"ok": False, "error": "ToyController 不可用"}

    if body.preset is not None:
        payload_hex = toy_adv.build_for_preset(body.preset, SETTINGS)
    elif body.mode is not None:
        payload_hex = toy_adv.build_direct(body.mode, body.speed or 0, SETTINGS)
    else:
        return {"ok": False, "error": "需要指定 preset 或 mode"}

    if not payload_hex:
        return {"ok": False, "error": "构建 payload 失败"}

    return {
        "ok": True,
        "payloadHex": payload_hex,
        "payloadLength": len(payload_hex) // 2,
        "note": "payload 已构建，需要前端 APK 发送广播",
    }
