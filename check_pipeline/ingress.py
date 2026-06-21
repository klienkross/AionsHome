import time

from .event_bus import EventBus
from .schemas import Schema


class Ingress:
    def __init__(self, bus: EventBus, schemas: dict[str, Schema]):
        self.bus = bus
        self.schemas = schemas

    async def handle(self, raw: dict) -> dict:
        msg_type = raw.get("type")
        if msg_type not in self.schemas:
            return {"ok": False, "error": "unknown_type"}

        errors = self.schemas[msg_type].validate(raw.get("payload", {}))
        if errors:
            return {"ok": False, "error": "validation", "details": errors}

        now = time.time()
        event = {
            **raw,
            "received_at": now,
            "drift": now - raw.get("ts", now),
        }

        await self.bus.emit(msg_type, event)

        commands = self.bus.drain_commands(raw.get("device_id"))
        return {"ok": True, "server_ts": now, "commands": commands}
