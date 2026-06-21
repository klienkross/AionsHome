import pytest
from check_pipeline.recorder import Recorder, MemoryStore
from check_pipeline.event_bus import EventBus
from check_pipeline.notifier import Notifier, MentionInChat, PushNotify, WebhookCallback


class FakeStrategy:
    def __init__(self, name, threshold=0):
        self.name = name
        self.threshold = threshold
        self.fired = []

    def should_fire(self, ctx):
        return ctx["overdue"] > self.threshold

    async def execute(self, ctx):
        self.fired.append(ctx)


class FailingStrategy:
    name = "failing"
    def should_fire(self, ctx):
        return True
    async def execute(self, ctx):
        raise RuntimeError("strategy exploded")


@pytest.mark.asyncio
async def test_handle_timeout_always_records():
    store = MemoryStore()
    recorder = Recorder(store)
    bus = EventBus()
    notifier = Notifier(bus, recorder)
    await notifier.handle_timeout(device_id="phone-1", check_type="heartbeat", overdue_seconds=60.0)
    assert len(store.timeouts) == 1
    assert store.timeouts[0]["device_id"] == "phone-1"


@pytest.mark.asyncio
async def test_strategy_fires_when_should_fire_true():
    store = MemoryStore()
    recorder = Recorder(store)
    bus = EventBus()
    notifier = Notifier(bus, recorder)
    strategy = FakeStrategy("test", threshold=0)
    notifier.register(strategy)
    await notifier.handle_timeout(device_id="phone-1", check_type="heartbeat", overdue_seconds=60.0)
    assert len(strategy.fired) == 1


@pytest.mark.asyncio
async def test_strategy_skipped_when_should_fire_false():
    store = MemoryStore()
    recorder = Recorder(store)
    bus = EventBus()
    notifier = Notifier(bus, recorder)
    strategy = FakeStrategy("test", threshold=9999)
    notifier.register(strategy)
    await notifier.handle_timeout(device_id="phone-1", check_type="heartbeat", overdue_seconds=60.0)
    assert len(strategy.fired) == 0


@pytest.mark.asyncio
async def test_failing_strategy_does_not_block_others():
    store = MemoryStore()
    recorder = Recorder(store)
    bus = EventBus()
    notifier = Notifier(bus, recorder)

    failing = FailingStrategy()
    good = FakeStrategy("good", threshold=0)

    notifier.register(failing)
    notifier.register(good)

    await notifier.handle_timeout(device_id="phone-1", check_type="heartbeat", overdue_seconds=60.0)
    assert len(good.fired) == 1
    assert len(store.timeouts) == 1


@pytest.mark.asyncio
async def test_multiple_strategies_ordered():
    store = MemoryStore()
    recorder = Recorder(store)
    bus = EventBus()
    notifier = Notifier(bus, recorder)

    s1 = FakeStrategy("low", threshold=0)
    s2 = FakeStrategy("high", threshold=1800)

    notifier.register(s1)
    notifier.register(s2)

    await notifier.handle_timeout(device_id="phone-1", check_type="heartbeat", overdue_seconds=60.0)
    assert len(s1.fired) == 1
    assert len(s2.fired) == 0  # 60 < 1800

    await notifier.handle_timeout(device_id="phone-1", check_type="heartbeat", overdue_seconds=3600.0)
    assert len(s2.fired) == 1  # 3600 > 1800


def test_mention_in_chat_always_fires():
    s = MentionInChat()
    assert s.should_fire({"overdue": 1}) is True
    assert s.should_fire({"overdue": 99999}) is True


def test_push_notify_fires_after_30_min():
    s = PushNotify()
    assert s.should_fire({"overdue": 1800}) is False
    assert s.should_fire({"overdue": 1801}) is True


def test_webhook_callback_fires_after_1_hour():
    s = WebhookCallback()
    assert s.should_fire({"overdue": 3600}) is False
    assert s.should_fire({"overdue": 3601}) is True
