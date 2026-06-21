import pytest
import time
from check_pipeline.event_bus import EventBus
from check_pipeline.schemas import HeartbeatSchema, SensorSchema, CheckSchema
from check_pipeline.ingress import Ingress


def _make_ingress(bus=None):
    bus = bus or EventBus()
    schemas = {
        "heartbeat": HeartbeatSchema(),
        "sensor": SensorSchema(),
        "check": CheckSchema(),
    }
    return Ingress(bus, schemas), bus


@pytest.mark.asyncio
async def test_valid_heartbeat_returns_ok():
    ingress, bus = _make_ingress()
    result = await ingress.handle({
        "type": "heartbeat",
        "device_id": "phone-1",
        "ts": time.time(),
        "payload": {"battery": 80},
    })
    assert result["ok"] is True
    assert "server_ts" in result
    assert isinstance(result["commands"], list)


@pytest.mark.asyncio
async def test_unknown_type_returns_error():
    ingress, bus = _make_ingress()
    result = await ingress.handle({
        "type": "unknown_garbage",
        "device_id": "phone-1",
        "ts": time.time(),
        "payload": {},
    })
    assert result["ok"] is False
    assert result["error"] == "unknown_type"


@pytest.mark.asyncio
async def test_validation_failure_returns_error():
    ingress, bus = _make_ingress()
    result = await ingress.handle({
        "type": "sensor",
        "device_id": "phone-1",
        "ts": time.time(),
        "payload": {},  # missing "event"
    })
    assert result["ok"] is False
    assert result["error"] == "validation"
    assert len(result["details"]) > 0


@pytest.mark.asyncio
async def test_emits_event_to_bus():
    bus = EventBus()
    received = []
    async def handler(event):
        received.append(event)
    bus.on("heartbeat", handler)

    ingress, _ = _make_ingress(bus)
    now = time.time()
    await ingress.handle({
        "type": "heartbeat",
        "device_id": "phone-1",
        "ts": now,
        "payload": {"battery": 50},
    })
    assert len(received) == 1
    assert received[0]["device_id"] == "phone-1"
    assert "received_at" in received[0]
    assert "drift" in received[0]


@pytest.mark.asyncio
async def test_drift_calculation():
    ingress, bus = _make_ingress()
    past = time.time() - 5.0
    result = await ingress.handle({
        "type": "heartbeat",
        "device_id": "phone-1",
        "ts": past,
        "payload": {},
    })
    assert result["ok"] is True


@pytest.mark.asyncio
async def test_commands_returned_in_response():
    bus = EventBus()
    bus.enqueue_command("phone-1", {"action": "report_location"})
    ingress, _ = _make_ingress(bus)
    result = await ingress.handle({
        "type": "heartbeat",
        "device_id": "phone-1",
        "ts": time.time(),
        "payload": {},
    })
    assert len(result["commands"]) == 1
    assert result["commands"][0]["action"] == "report_location"

    # second call: commands already drained
    result2 = await ingress.handle({
        "type": "heartbeat",
        "device_id": "phone-1",
        "ts": time.time(),
        "payload": {},
    })
    assert result2["commands"] == []
