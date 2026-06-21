import time

from .event_bus import EventBus
from .notifier import Notifier


class Watchdog:
    def __init__(self, bus: EventBus, notifier: Notifier):
        self.bus = bus
        self.notifier = notifier
        self._timers: dict[str, float] = {}
        self._ttls: dict[str, int] = {}
        self._defaults = {"heartbeat": 300, "check": 1800, "sensor": 900}

    async def feed(self, event: dict):
        device = event.get("device_id", "default")
        etype = event.get("type", "check")
        key = f"{device}:{etype}"
        ttl = event.get("payload", {}).get("ttl") or self._defaults.get(etype, 1800)
        self._ttls[key] = ttl
        self._timers[key] = time.time() + ttl

    async def tick(self):
        now = time.time()
        expired = [k for k, deadline in self._timers.items() if now > deadline]
        for key in expired:
            device, etype = key.split(":", 1)
            overdue = now - self._timers[key]
            await self.notifier.handle_timeout(
                device_id=device,
                check_type=etype,
                overdue_seconds=overdue,
            )
            self._timers[key] = now + self._ttls[key] * 2
