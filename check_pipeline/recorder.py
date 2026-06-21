from typing import Protocol


class Store(Protocol):
    async def save_event(self, record: dict): ...
    async def save_timeout(self, record: dict): ...


class MemoryStore:
    def __init__(self):
        self.events: list[dict] = []
        self.timeouts: list[dict] = []

    async def save_event(self, record: dict):
        self.events.append(record)

    async def save_timeout(self, record: dict):
        self.timeouts.append(record)


class Recorder:
    def __init__(self, store: Store):
        self._store = store

    async def log(self, event_type: str, event: dict):
        record = {"event_type": event_type, "event": event}
        await self._store.save_event(record)

    async def log_timeout(self, context: dict):
        await self._store.save_timeout(context)
