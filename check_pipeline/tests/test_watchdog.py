import pytest
import time
from unittest.mock import AsyncMock
from check_pipeline.event_bus import EventBus
from check_pipeline.recorder import Recorder, MemoryStore
from check_pipeline.notifier import Notifier
from check_pipeline.watchdog import Watchdog


def _make_watchdog():
    bus = EventBus()
    store = MemoryStore()
    recorder = Recorder(store)
    notifier = Notifier(bus, recorder)
    notifier.handle_timeout = AsyncMock()
    watchdog = Watchdog(bus, notifier)
    return watchdog, notifier, store


@pytest.mark.asyncio
async def test_feed_registers_timer():
    watchdog, notifier, _ = _make_watchdog()
    await watchdog.feed({"type": "heartbeat", "device_id": "phone-1", "payload": {}})
    assert "phone-1:heartbeat" in watchdog._timers


@pytest.mark.asyncio
async def test_tick_no_timeout_before_deadline():
    watchdog, notifier, _ = _make_watchdog()
    await watchdog.feed({"type": "heartbeat", "device_id": "phone-1", "payload": {}})
    await watchdog.tick()
    notifier.handle_timeout.assert_not_called()


@pytest.mark.asyncio
async def test_tick_triggers_timeout_after_deadline():
    watchdog, notifier, _ = _make_watchdog()
    await watchdog.feed({"type": "heartbeat", "device_id": "phone-1", "payload": {}})
    # force deadline into the past
    watchdog._timers["phone-1:heartbeat"] = time.time() - 10
    await watchdog.tick()
    notifier.handle_timeout.assert_called_once()
    call_kwargs = notifier.handle_timeout.call_args[1]
    assert call_kwargs["device_id"] == "phone-1"
    assert call_kwargs["check_type"] == "heartbeat"
    assert call_kwargs["overdue_seconds"] > 0


@pytest.mark.asyncio
async def test_custom_ttl_from_payload():
    watchdog, notifier, _ = _make_watchdog()
    await watchdog.feed({
        "type": "check",
        "device_id": "phone-1",
        "payload": {"key": "location", "ttl": 60},
    })
    deadline = watchdog._timers["phone-1:check"]
    expected = time.time() + 60
    assert abs(deadline - expected) < 2


@pytest.mark.asyncio
async def test_default_ttl_heartbeat():
    watchdog, notifier, _ = _make_watchdog()
    await watchdog.feed({"type": "heartbeat", "device_id": "phone-1", "payload": {}})
    deadline = watchdog._timers["phone-1:heartbeat"]
    expected = time.time() + 300  # default heartbeat ttl
    assert abs(deadline - expected) < 2


@pytest.mark.asyncio
async def test_timeout_doubles_interval():
    watchdog, notifier, _ = _make_watchdog()
    await watchdog.feed({"type": "heartbeat", "device_id": "phone-1", "payload": {}})
    watchdog._timers["phone-1:heartbeat"] = time.time() - 10
    await watchdog.tick()

    # after timeout, next deadline should be now + 2*ttl
    new_deadline = watchdog._timers["phone-1:heartbeat"]
    expected = time.time() + 300 * 2
    assert abs(new_deadline - expected) < 2


@pytest.mark.asyncio
async def test_feed_resets_timer():
    watchdog, notifier, _ = _make_watchdog()
    await watchdog.feed({"type": "heartbeat", "device_id": "phone-1", "payload": {}})
    first_deadline = watchdog._timers["phone-1:heartbeat"]

    # simulate time passing, then new heartbeat
    watchdog._timers["phone-1:heartbeat"] = time.time() + 10  # almost expired
    await watchdog.feed({"type": "heartbeat", "device_id": "phone-1", "payload": {}})
    new_deadline = watchdog._timers["phone-1:heartbeat"]

    # should be reset to now + 300, not still at +10
    assert new_deadline > time.time() + 200


@pytest.mark.asyncio
async def test_multiple_devices_independent():
    watchdog, notifier, _ = _make_watchdog()
    await watchdog.feed({"type": "heartbeat", "device_id": "phone-1", "payload": {}})
    await watchdog.feed({"type": "heartbeat", "device_id": "phone-2", "payload": {}})

    # only phone-1 times out
    watchdog._timers["phone-1:heartbeat"] = time.time() - 10
    await watchdog.tick()

    assert notifier.handle_timeout.call_count == 1
    assert notifier.handle_timeout.call_args[1]["device_id"] == "phone-1"
