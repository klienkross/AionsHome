import pytest
from check_pipeline.event_bus import EventBus


@pytest.mark.asyncio
async def test_emit_calls_registered_handler():
    bus = EventBus()
    received = []
    async def handler(event):
        received.append(event)
    bus.on("heartbeat", handler)
    await bus.emit("heartbeat", {"device_id": "phone-1", "ts": 1000})
    assert len(received) == 1
    assert received[0]["device_id"] == "phone-1"


@pytest.mark.asyncio
async def test_emit_does_not_call_unrelated_handler():
    bus = EventBus()
    received = []
    async def handler(event):
        received.append(event)
    bus.on("sensor", handler)
    await bus.emit("heartbeat", {"device_id": "phone-1"})
    assert len(received) == 0


@pytest.mark.asyncio
async def test_wildcard_handler_receives_all_events():
    bus = EventBus()
    received = []
    async def handler(event_type, event):
        received.append((event_type, event))
    bus.on("*", handler)
    await bus.emit("heartbeat", {"a": 1})
    await bus.emit("sensor", {"b": 2})
    assert len(received) == 2
    assert received[0][0] == "heartbeat"
    assert received[1][0] == "sensor"


@pytest.mark.asyncio
async def test_handler_exception_does_not_block_others():
    bus = EventBus()
    results = []

    async def bad_handler(event):
        raise ValueError("boom")

    async def good_handler(event):
        results.append("ok")

    bus.on("heartbeat", bad_handler)
    bus.on("heartbeat", good_handler)
    await bus.emit("heartbeat", {})
    assert results == ["ok"]


@pytest.mark.asyncio
async def test_enqueue_and_drain_commands():
    bus = EventBus()
    bus.enqueue_command("phone-1", {"action": "report_location"})
    bus.enqueue_command("phone-1", {"action": "adjust_interval", "value": 60})
    bus.enqueue_command("phone-2", {"action": "noop"})

    cmds = bus.drain_commands("phone-1")
    assert len(cmds) == 2
    assert cmds[0]["action"] == "report_location"

    # drain clears the queue
    assert bus.drain_commands("phone-1") == []

    # phone-2 is independent
    assert len(bus.drain_commands("phone-2")) == 1


@pytest.mark.asyncio
async def test_drain_commands_unknown_device_returns_empty():
    bus = EventBus()
    assert bus.drain_commands("nonexistent") == []
