import logging
from typing import Callable

log = logging.getLogger(__name__)


class EventBus:
    def __init__(self):
        self._handlers: dict[str, list[Callable]] = {}
        self._command_queue: dict[str, list[dict]] = {}

    def on(self, event_type: str, handler: Callable):
        self._handlers.setdefault(event_type, []).append(handler)

    async def emit(self, event_type: str, event: dict):
        for handler in self._handlers.get(event_type, []):
            try:
                await handler(event)
            except Exception as e:
                log.error("handler %s failed: %s", handler.__name__, e)
        for handler in self._handlers.get("*", []):
            try:
                await handler(event_type, event)
            except Exception as e:
                log.error("wildcard handler %s failed: %s", handler.__name__, e)

    def enqueue_command(self, device_id: str, cmd: dict):
        self._command_queue.setdefault(device_id, []).append(cmd)

    def drain_commands(self, device_id: str) -> list[dict]:
        return self._command_queue.pop(device_id, [])
