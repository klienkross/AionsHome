"""
钱包 API：余额查询、转账记录、转账入账
"""

import time
from fastapi import APIRouter
from pydantic import BaseModel
from typing import Optional

from database import get_db

router = APIRouter()


async def _get_balance() -> float:
    """内部工具：获取钱包余额"""
    async with get_db() as db:
        cur = await db.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM bookkeeping "
            "WHERE record_type IN ('wallet_user', 'wallet_ai')"
        )
        row = await cur.fetchone()
        return row[0]


async def record_transfer(amount: float, source: str, description: str) -> dict:
    """公共转账入账，chat.py 和 API 端点共用"""
    now = time.time()
    rec_id = f"wt_{int(now * 1000)}"
    record_type = "wallet_ai" if source == "ai" else "wallet_user"

    async with get_db() as db:
        await db.execute(
            "INSERT INTO bookkeeping (id, record_type, amount, description, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (rec_id, record_type, amount, description, now)
        )
        await db.commit()
        cur = await db.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM bookkeeping "
            "WHERE record_type IN ('wallet_user', 'wallet_ai')"
        )
        row = await cur.fetchone()

    return {"ok": True, "id": rec_id, "balance": row[0]}


class TransferIn(BaseModel):
    amount: float
    source: str = "user"          # "user" | "ai"
    description: str = ""


@router.get("/api/wallet/balance")
async def get_balance():
    """查询钱包余额（所有 wallet_* 类型记录的 amount 求和）"""
    balance = await _get_balance()
    return {"balance": balance}


@router.get("/api/wallet/transactions")
async def list_transactions(limit: int = 50, offset: int = 0):
    """获取转账记录列表，按时间倒序"""
    async with get_db() as db:
        db.row_factory = __import__('aiosqlite').Row
        cur = await db.execute(
            "SELECT * FROM bookkeeping WHERE record_type IN ('wallet_user', 'wallet_ai') "
            "ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (limit, offset)
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


@router.post("/api/wallet/transfer")
async def do_transfer(body: TransferIn):
    """执行转账入账"""
    return await record_transfer(body.amount, body.source, body.description)
