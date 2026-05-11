"""
WebSocket 连接管理器
"""

import asyncio
import json
import logging
import time
from fastapi import WebSocket

log = logging.getLogger("ws")

HEARTBEAT_INTERVAL = 30  # 每 30s ping 一次
HEARTBEAT_TIMEOUT = 10   # ping 后 10s 无 pong 视为死连接


class ConnectionManager:
    def __init__(self):
        self.active: list[WebSocket] = []
        self.tts_clients: dict[WebSocket, dict] = {}  # {ws: {"enabled": bool, "voice": str}}
        self._tts_fallback: dict = {}  # {"enabled": bool, "voice": str} — 来自 HTTP 请求的备用 TTS 状态
        self.client_ids: dict[WebSocket, str] = {}     # {ws: client_id} — 客户端唯一标识
        self._last_sender_client_id: str | None = None  # 最后发消息的客户端 ID
        self.pet_clients: dict[WebSocket, bool] = {}    # {ws: enabled} — 在线桌宠客户端
        self._last_pong: dict[WebSocket, float] = {}
        self._heartbeat_task: asyncio.Task | None = None

    def start_heartbeat(self):
        if self._heartbeat_task is None:
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

    async def _heartbeat_loop(self):
        while True:
            await asyncio.sleep(HEARTBEAT_INTERVAL)
            now = time.time()
            dead = []
            for ws in self.active.copy():
                last = self._last_pong.get(ws, now)
                if now - last > HEARTBEAT_INTERVAL + HEARTBEAT_TIMEOUT:
                    dead.append(ws)
                    continue
                try:
                    await ws.send_text(json.dumps({"type": "ping"}))
                except Exception:
                    dead.append(ws)
            for ws in dead:
                log.info("Heartbeat: removing dead connection")
                self._remove_ws(ws)

    def record_pong(self, ws: WebSocket):
        self._last_pong[ws] = time.time()

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active.append(ws)
        self._last_pong[ws] = time.time()
        log.info("WS connected, total=%d", len(self.active))

    def disconnect(self, ws: WebSocket):
        self._remove_ws(ws)
        log.info("WS disconnected, total=%d", len(self.active))

    def _remove_ws(self, ws: WebSocket):
        if ws in self.active:
            self.active.remove(ws)
        self.tts_clients.pop(ws, None)
        self.client_ids.pop(ws, None)
        self.pet_clients.pop(ws, None)
        self._last_pong.pop(ws, None)

    def register_client_id(self, ws: WebSocket, client_id: str):
        self.client_ids[ws] = client_id

    def set_last_sender(self, client_id: str):
        self._last_sender_client_id = client_id

    def set_pet_state(self, ws: WebSocket, enabled: bool):
        if enabled:
            self.pet_clients[ws] = True
        else:
            self.pet_clients.pop(ws, None)

    def has_active_pet(self) -> bool:
        return any(self.pet_clients.values())

    async def send_to_client(self, client_id: str, data: dict):
        msg = json.dumps(data, ensure_ascii=False)
        for ws, cid in list(self.client_ids.items()):
            if cid == client_id:
                try:
                    await ws.send_text(msg)
                except Exception as e:
                    log.warning("WS send_to_client failed: %s", e)

    async def send_to_last_sender(self, data: dict):
        if self._last_sender_client_id:
            await self.send_to_client(self._last_sender_client_id, data)

    def set_tts_state(self, ws: WebSocket, enabled: bool, voice: str = ""):
        if enabled and voice:
            self.tts_clients[ws] = {"enabled": True, "voice": voice}
        else:
            self.tts_clients.pop(ws, None)

    def set_tts_fallback(self, enabled: bool, voice: str = ""):
        if enabled and voice:
            self._tts_fallback = {"enabled": True, "voice": voice}
        else:
            self._tts_fallback = {}

    def any_tts_enabled(self) -> bool:
        if any(c.get("enabled") for c in self.tts_clients.values()):
            return True
        return bool(self._tts_fallback.get("enabled"))

    def get_tts_voice(self) -> str | None:
        for c in self.tts_clients.values():
            if c.get("enabled"):
                return c.get("voice")
        if self._tts_fallback.get("enabled"):
            return self._tts_fallback.get("voice")
        return None

    async def broadcast(self, data: dict, exclude: WebSocket = None):
        msg = json.dumps(data, ensure_ascii=False)
        targets = [ws for ws in self.active.copy() if ws is not exclude]
        if not targets:
            return

        async def _send(ws):
            try:
                await ws.send_text(msg)
                return True
            except Exception:
                self._remove_ws(ws)
                return False

        results = await asyncio.gather(*[_send(ws) for ws in targets], return_exceptions=True)
        sent = sum(1 for r in results if r is True)
        failed = len(targets) - sent
        if failed:
            log.info("broadcast type=%s sent=%d failed=%d total=%d",
                     data.get("type", "?"), sent, failed, len(self.active))


manager = ConnectionManager()
