from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, StrictBool, field_validator


class RelayCommandCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    on: StrictBool


class CommandRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    command_id: str
    correlation_id: str
    house_id: str
    device_id: str
    requested_on: bool
    status: Literal["queued", "dispatched", "applied", "failed", "timed_out"]
    ack_status: Literal["accepted", "applied", "rejected", "failed"] | None
    reported_on: bool | None
    failure_code: (
        Literal[
            "device_rejected",
            "device_failed",
            "publish_unconfirmed",
            "deadline_exceeded",
        ]
        | None
    )
    created_at: datetime
    updated_at: datetime
    expires_at: datetime
    dispatch_started_at: datetime | None
    dispatched_at: datetime | None
    ack_received_at: datetime | None
    state_received_at: datetime | None
    completed_at: datetime | None

    @field_validator(
        "created_at",
        "updated_at",
        "expires_at",
        "dispatch_started_at",
        "dispatched_at",
        "ack_received_at",
        "state_received_at",
        "completed_at",
    )
    @classmethod
    def utc(cls, value: datetime | None) -> datetime | None:
        # SQLite test storage drops tzinfo; PostgreSQL uses timestamptz.
        return (
            value.replace(tzinfo=UTC)
            if value is not None and value.tzinfo is None
            else value
        )
