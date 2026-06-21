import pytest
from check_pipeline.recorder import Recorder, MemoryStore


@pytest.mark.asyncio
async def test_log_event_stores_record():
    store = MemoryStore()
    recorder = Recorder(store)
    await recorder.log("heartbeat", {"device_id": "phone-1", "ts": 1000})
    assert len(store.events) == 1
    assert store.events[0]["event_type"] == "heartbeat"
    assert store.events[0]["event"]["device_id"] == "phone-1"


@pytest.mark.asyncio
async def test_log_multiple_events():
    store = MemoryStore()
    recorder = Recorder(store)
    await recorder.log("heartbeat", {"ts": 1})
    await recorder.log("sensor", {"ts": 2})
    await recorder.log("check", {"ts": 3})
    assert len(store.events) == 3


@pytest.mark.asyncio
async def test_log_timeout_stores_timeout_record():
    store = MemoryStore()
    recorder = Recorder(store)
    ctx = {"device_id": "phone-1", "check_type": "heartbeat", "overdue": 120.0, "ts": 1000}
    await recorder.log_timeout(ctx)
    assert len(store.timeouts) == 1
    assert store.timeouts[0]["device_id"] == "phone-1"


@pytest.mark.asyncio
async def test_log_timeout_multiple():
    store = MemoryStore()
    recorder = Recorder(store)
    await recorder.log_timeout({"device_id": "a", "check_type": "heartbeat", "overdue": 60, "ts": 1})
    await recorder.log_timeout({"device_id": "b", "check_type": "check", "overdue": 120, "ts": 2})
    assert len(store.timeouts) == 2
