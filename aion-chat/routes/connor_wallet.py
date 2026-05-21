"""
Connor 钱包 API：余额查询、转账记录、转账入账
数据与 AIon 钱包完全独立，使用 connor_wallet_user / connor_wallet_ai 类型
"""

import time
from fastapi import APIRouter
from pydantic import BaseModel

from database import get_db

router = APIRouter()


async def _get_connor_balance() -> float:
    """内部工具：获取 Connor 钱包余额"""
    async with get_db() as db:
        cur = await db.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM bookkeeping "
            "WHERE record_type IN ('connor_wallet_user', 'connor_wallet_ai')"
        )
        row = await cur.fetchone()
        return row[0]


class ConnorTransferIn(BaseModel):
    amount: float
    source: str = "user"          # "user" | "connor"
    description: str = ""


@router.get("/api/connor-wallet/balance")
async def get_connor_balance():
    """查询 Connor 钱包余额"""
    async with get_db() as db:
        cur = await db.execute(
            "SELECT COALESCE(SUM(amount), 0) AS balance FROM bookkeeping "
            "WHERE record_type IN ('connor_wallet_user', 'connor_wallet_ai')"
        )
        row = await cur.fetchone()
        return {"balance": row[0]}


@router.get("/api/connor-wallet/transactions")
async def list_connor_transactions(limit: int = 50, offset: int = 0):
    """获取 Connor 钱包转账记录列表，按时间倒序"""
    async with get_db() as db:
        db.row_factory = __import__('aiosqlite').Row
        cur = await db.execute(
            "SELECT * FROM bookkeeping WHERE record_type IN ('connor_wallet_user', 'connor_wallet_ai') "
            "ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (limit, offset)
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


@router.post("/api/connor-wallet/transfer")
async def do_connor_transfer(body: ConnorTransferIn):
    """执行 Connor 钱包转账入账"""
    now = time.time()
    rec_id = f"cwt_{int(now * 1000)}"
    record_type = "connor_wallet_ai" if body.source == "connor" else "connor_wallet_user"

    async with get_db() as db:
        await db.execute(
            "INSERT INTO bookkeeping (id, record_type, amount, description, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (rec_id, record_type, body.amount, body.description, now)
        )
        await db.commit()

        # 返回最新余额
        cur = await db.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM bookkeeping "
            "WHERE record_type IN ('connor_wallet_user', 'connor_wallet_ai')"
        )
        row = await cur.fetchone()

    return {"ok": True, "id": rec_id, "balance": row[0]}
