import logging
import time
from typing import Protocol

from .event_bus import EventBus
from .recorder import Recorder

log = logging.getLogger(__name__)


class NotifyStrategy(Protocol):
    name: str
    def should_fire(self, ctx: dict) -> bool: ...
    async def execute(self, ctx: dict): ...


class Notifier:
    def __init__(self, bus: EventBus, recorder: Recorder):
        self.bus = bus
        self.recorder = recorder
        self._strategies: list[NotifyStrategy] = []

    def register(self, strategy: NotifyStrategy):
        self._strategies.append(strategy)

    async def handle_timeout(self, device_id: str, check_type: str, overdue_seconds: float):
        context = {
            "device_id": device_id,
            "check_type": check_type,
            "overdue": overdue_seconds,
            "ts": time.time(),
        }
        await self.recorder.log_timeout(context)
        for strategy in self._strategies:
            if strategy.should_fire(context):
                try:
                    await strategy.execute(context)
                except Exception as e:
                    log.error("notify strategy %s failed: %s", strategy.name, e)


class MentionInChat:
    name = "mention"

    def should_fire(self, ctx: dict) -> bool:
        return True

    async def execute(self, ctx: dict):
        log.info(
            "设备 %s 的 %s 已超时 %ds — 下次对话时提及",
            ctx["device_id"], ctx["check_type"], int(ctx["overdue"]),
        )


class PushNotify:
    name = "push"

    def should_fire(self, ctx: dict) -> bool:
        return ctx["overdue"] > 1800

    async def execute(self, ctx: dict):
        log.info(
            "PUSH: %s 超时 %d 分钟",
            ctx["check_type"], int(ctx["overdue"] / 60),
        )


class WebhookCallback:
    name = "webhook"

    def should_fire(self, ctx: dict) -> bool:
        return ctx["overdue"] > 3600

    async def execute(self, ctx: dict):
        log.info(
            "WEBHOOK: %s 超时 %d 分钟 — 触发回调",
            ctx["check_type"], int(ctx["overdue"] / 60),
        )
