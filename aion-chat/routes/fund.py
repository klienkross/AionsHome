"""
奥罗斯财团 — API 路由
持仓 CRUD / 数据拉取 / 手动触发分析 / 配置开关
"""

import time
from fastapi import APIRouter
from pydantic import BaseModel
from typing import Optional

import aiosqlite
from database import get_db
from ws import manager
from fund import (
    load_fund_config, save_fund_config,
    run_fund_analysis, fetch_only,
    is_trading_day, fetch_fund_history,
    load_fund_cache,
)

router = APIRouter()


# ── 数据模型 ─────────────────────────────────────
class HoldingCreate(BaseModel):
    fund_code: str
    fund_name: str = ""
    shares: float = 0
    avg_cost: float = 0
    total_cost: float = 0
    warn_down: float = -3.0
    warn_up: float = 15.0


class HoldingUpdate(BaseModel):
    fund_name: Optional[str] = None
    shares: Optional[float] = None
    avg_cost: Optional[float] = None
    total_cost: Optional[float] = None
    warn_down: Optional[float] = None
    warn_up: Optional[float] = None


class ConfigUpdate(BaseModel):
    enabled: Optional[bool] = None
    tendency: Optional[str] = None


# ── 配置 ─────────────────────────────────────────
@router.get("/api/fund/config")
async def get_config():
    cfg = load_fund_config()
    cfg["is_trading_day"] = is_trading_day()
    return cfg


@router.post("/api/fund/config")
async def update_config(body: ConfigUpdate):
    cfg = load_fund_config()
    if body.enabled is not None:
        cfg["enabled"] = body.enabled
    if body.tendency is not None:
        cfg["tendency"] = body.tendency
    save_fund_config(cfg)
    return cfg


# ── 持仓 CRUD ────────────────────────────────────
@router.get("/api/fund/holdings")
async def list_holdings():
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM fund_holdings ORDER BY fund_code")
        return [dict(r) for r in await cur.fetchall()]


@router.post("/api/fund/holdings")
async def add_holding(body: HoldingCreate):
    hid = f"fh_{int(time.time()*1000)}"
    now = time.time()
    async with get_db() as db:
        await db.execute(
            "INSERT INTO fund_holdings (id, fund_code, fund_name, shares, avg_cost, total_cost, warn_down, warn_up, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (hid, body.fund_code, body.fund_name, body.shares, body.avg_cost,
             body.total_cost, body.warn_down, body.warn_up, now),
        )
        await db.commit()
    return {"id": hid, "fund_code": body.fund_code, "fund_name": body.fund_name,
            "shares": body.shares, "avg_cost": body.avg_cost, "total_cost": body.total_cost,
            "warn_down": body.warn_down, "warn_up": body.warn_up, "created_at": now}


@router.put("/api/fund/holdings/{holding_id}")
async def update_holding(holding_id: str, body: HoldingUpdate):
    updates = []
    params = []
    for field in ("fund_name", "shares", "avg_cost", "total_cost", "warn_down", "warn_up"):
        val = getattr(body, field)
        if val is not None:
            updates.append(f"{field}=?")
            params.append(val)
    if not updates:
        return {"ok": False, "reason": "no fields"}
    params.append(holding_id)
    async with get_db() as db:
        await db.execute(
            f"UPDATE fund_holdings SET {','.join(updates)} WHERE id=?", params
        )
        await db.commit()
    return {"ok": True}


@router.delete("/api/fund/holdings/{holding_id}")
async def delete_holding(holding_id: str):
    async with get_db() as db:
        await db.execute("DELETE FROM fund_holdings WHERE id=?", (holding_id,))
        await db.commit()
    return {"ok": True}


# ── 数据拉取（不调 AI） ─────────────────────────
@router.post("/api/fund/fetch")
async def fetch_data():
    data = await fetch_only()
    return data

# ── 读取缓存数据（不拉取） ─────────────────
@router.get("/api/fund/cache")
async def get_cache():
    data = load_fund_cache()
    if data:
        return data
    return {"funds": [], "index": None, "fetch_time": None}

# ── 手动触发分析（调 AI） ───────────────────────
@router.post("/api/fund/analyze")
async def trigger_analysis():
    result = await run_fund_analysis(manual=True)
    return result


# ── 单只基金历史走势 ─────────────────────────────
@router.get("/api/fund/history/{fund_code}")
async def get_history(fund_code: str, days: int = 30):
    import asyncio
    loop = asyncio.get_event_loop()
    hist = await loop.run_in_executor(None, fetch_fund_history, fund_code, days)
    return hist
