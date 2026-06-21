import pytest
import time
from check_pipeline.event_bus import EventBus
from check_pipeline.schemas import HeartbeatSchema, SensorSchema, CheckSchema
from check_pipeline.ingress import Ingress
from check_pipeline.recorder import Recorder, MemoryStore
from check_pipeline.notifier import Notifier
from check_pipeline.watchdog import Watchdog


def _wire_pipeline():
    bus = EventBus()
    store = MemoryStore()
    recorder = Recorder(store)
    notifier = Notifier(bus, recorder)
    watchdog = Watchdog(bus, notifier)

    bus.on("*", recorder.log)
    bus.on("heartbeat", watchdog.feed)
    bus.on("check", watchdog.feed)

    schemas = {
        "heartbeat": HeartbeatSchema(),
        "sensor": SensorSchema(),
        "check": CheckSchema(),
    }
    ingress = Ingress(bus, schemas)
    return ingress, bus, store, watchdog, notifier


@pytest.mark.asyncio
async def test_heartbeat_flow():
    ingress, bus, store, watchdog, notifier = _wire_pipeline()

    result = await ingress.handle({
        "type": "heartbeat",
        "device_id": "phone-1",
        "ts": time.time(),
        "payload": {"battery": 72, "network": "4g", "screen": "on"},
    })

    assert result["ok"] is True
    assert len(store.events) == 1
    assert store.events[0]["event_type"] == "heartbeat"
    assert "phone-1:heartbeat" in watchdog._timers


@pytest.mark.asyncio
async def test_sensor_flow_no_watchdog():
    ingress, bus, store, watchdog, notifier = _wire_pipeline()

    result = await ingress.handle({
        "type": "sensor",
        "device_id": "phone-1",
        "ts": time.time(),
        "payload": {"event": "steps", "data": {"count": 5000}},
    })

    assert result["ok"] is True
    assert len(store.events) == 1
    # sensor is not registered with watchdog
    assert "phone-1:sensor" not in watchdog._timers


@pytest.mark.asyncio
async def test_check_with_custom_ttl():
    ingress, bus, store, watchdog, notifier = _wire_pipeline()

    result = await ingress.handle({
        "type": "check",
        "device_id": "phone-1",
        "ts": time.time(),
        "payload": {"key": "location", "value": {"lat": 31.2}, "ttl": 120},
    })

    assert result["ok"] is True
    assert "phone-1:check" in watchdog._timers
    deadline = watchdog._timers["phone-1:check"]
    assert abs(deadline - (time.time() + 120)) < 2


@pytest.mark.asyncio
async def test_timeout_records_to_store():
    ingress, bus, store, watchdog, notifier = _wire_pipeline()

    await ingress.handle({
        "type": "heartbeat",
        "device_id": "phone-1",
        "ts": time.time(),
        "payload": {},
    })

    # force timeout
    watchdog._timers["phone-1:heartbeat"] = time.time() - 10
    await watchdog.tick()

    assert len(store.timeouts) == 1
    assert store.timeouts[0]["device_id"] == "phone-1"
    assert store.timeouts[0]["check_type"] == "heartbeat"


@pytest.mark.asyncio
async def test_commands_round_trip():
    ingress, bus, store, watchdog, notifier = _wire_pipeline()

    bus.enqueue_command("phone-1", {"action": "report_location"})

    result = await ingress.handle({
        "type": "heartbeat",
        "device_id": "phone-1",
        "ts": time.time(),
        "payload": {},
    })

    assert len(result["commands"]) == 1
    assert result["commands"][0]["action"] == "report_location"

    # next request: no commands
    result2 = await ingress.handle({
        "type": "heartbeat",
        "device_id": "phone-1",
        "ts": time.time(),
        "payload": {},
    })
    assert result2["commands"] == []


@pytest.mark.asyncio
async def test_validation_rejection_does_not_record():
    ingress, bus, store, watchdog, notifier = _wire_pipeline()

    result = await ingress.handle({
        "type": "heartbeat",
        "device_id": "phone-1",
        "ts": time.time(),
        "payload": {"battery": 999},
    })

    assert result["ok"] is False
    assert len(store.events) == 0
