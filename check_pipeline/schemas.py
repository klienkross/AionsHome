from typing import Protocol


class Schema(Protocol):
    def validate(self, payload: dict) -> list[str]: ...


class HeartbeatSchema:
    def validate(self, payload: dict) -> list[str]:
        errors = []
        if "battery" in payload:
            b = payload["battery"]
            if not isinstance(b, (int, float)) or b < 0 or b > 100:
                errors.append("battery must be 0-100")
        return errors


class SensorSchema:
    def validate(self, payload: dict) -> list[str]:
        errors = []
        if "event" not in payload:
            errors.append("event is required")
        return errors


class CheckSchema:
    def validate(self, payload: dict) -> list[str]:
        errors = []
        if "key" not in payload:
            errors.append("key is required")
        if "ttl" in payload:
            ttl = payload["ttl"]
            if not isinstance(ttl, (int, float)) or ttl <= 0:
                errors.append("ttl must be positive")
        return errors
