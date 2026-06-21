import pytest
from check_pipeline.schemas import Schema, HeartbeatSchema, SensorSchema, CheckSchema


def test_heartbeat_schema_valid():
    s = HeartbeatSchema()
    errors = s.validate({"battery": 85, "network": "wifi", "screen": "off"})
    assert errors == []


def test_heartbeat_schema_empty_payload_is_valid():
    s = HeartbeatSchema()
    errors = s.validate({})
    assert errors == []


def test_heartbeat_schema_battery_out_of_range():
    s = HeartbeatSchema()
    errors = s.validate({"battery": 150})
    assert len(errors) == 1
    assert "battery" in errors[0]


def test_sensor_schema_valid():
    s = SensorSchema()
    errors = s.validate({"event": "steps", "data": {"count": 3200}})
    assert errors == []


def test_sensor_schema_missing_event():
    s = SensorSchema()
    errors = s.validate({"data": {"count": 100}})
    assert len(errors) == 1
    assert "event" in errors[0]


def test_check_schema_valid():
    s = CheckSchema()
    errors = s.validate({"key": "location", "value": {"lat": 31.2}, "ttl": 1800})
    assert errors == []


def test_check_schema_missing_key():
    s = CheckSchema()
    errors = s.validate({"value": {"lat": 31.2}})
    assert len(errors) == 1
    assert "key" in errors[0]


def test_check_schema_negative_ttl():
    s = CheckSchema()
    errors = s.validate({"key": "x", "value": {}, "ttl": -1})
    assert len(errors) == 1
    assert "ttl" in errors[0]


def test_schema_protocol_compliance():
    for cls in [HeartbeatSchema, SensorSchema, CheckSchema]:
        instance = cls()
        assert hasattr(instance, "validate")
        result = instance.validate({})
        assert isinstance(result, list)
