from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.transports.mqtt_topics import PROTOCOL_VERSION

MAX_CONTROL_PAYLOAD = 16 * 1024
MAX_TELEMETRY_PAYLOAD = 32 * 1024
MAX_STATE_FIELDS = 64
MAX_TELEMETRY_FIELDS = 128
MAX_JSON_DEPTH = 8


class ProtocolEnvelope(BaseModel):
    model_config = ConfigDict(extra="ignore")
    protocol_version: Literal["v1"] = PROTOCOL_VERSION


class StateEnvelope(ProtocolEnvelope):
    timestamp: datetime
    correlation_id: str = Field(default="", max_length=128)
    state: dict[str, Any]

    @field_validator("state")
    @classmethod
    def state_limits(cls, value: dict[str, Any]) -> dict[str, Any]:
        if len(value) > MAX_STATE_FIELDS:
            raise ValueError("state may contain at most 64 fields")
        return value


class CommandEnvelope(ProtocolEnvelope):
    command_id: str = Field(min_length=1, max_length=128)
    correlation_id: str = Field(min_length=1, max_length=128)
    timestamp: datetime
    state: dict[str, Any]

    @field_validator("state")
    @classmethod
    def state_limits(cls, value: dict[str, Any]) -> dict[str, Any]:
        if not value or len(value) > MAX_STATE_FIELDS:
            raise ValueError("command state must contain between 1 and 64 fields")
        return value


class AckEnvelope(ProtocolEnvelope):
    command_id: str = Field(min_length=1, max_length=128)
    correlation_id: str = Field(min_length=1, max_length=128)
    timestamp: datetime | None = None
    status: Literal["accepted", "applied", "rejected", "failed"]
    error_code: str | None = Field(default=None, max_length=128)
    message: str | None = Field(default=None, max_length=512)


class StatusEnvelope(ProtocolEnvelope):
    timestamp: datetime | None = None
    status: Literal["online", "offline"]
    last_seen: datetime
    heartbeat_interval_seconds: int | None = Field(default=None, ge=30, le=300)


class TelemetryEnvelope(ProtocolEnvelope):
    timestamp: datetime
    correlation_id: str = Field(default="", max_length=128)
    metrics: dict[str, Any]

    @field_validator("metrics")
    @classmethod
    def metric_limits(cls, value: dict[str, Any]) -> dict[str, Any]:
        if len(value) > MAX_TELEMETRY_FIELDS:
            raise ValueError("telemetry may contain at most 128 metrics")
        return value


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def json_depth(value: Any, current: int = 1) -> int:
    if not isinstance(value, (dict, list)) or not value:
        return current
    children = value.values() if isinstance(value, dict) else value
    return max(json_depth(child, current + 1) for child in children)


def validate_json_bounds(value: Any) -> None:
    if json_depth(value) > MAX_JSON_DEPTH:
        raise ValueError("MQTT JSON exceeds maximum nesting depth")
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, str) and len(item.encode("utf-8")) > 1024:
            raise ValueError("MQTT JSON string exceeds maximum length")
        if isinstance(item, dict):
            stack.extend(item.keys())
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
