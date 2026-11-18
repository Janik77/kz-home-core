from datetime import datetime
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_serializer
from app.schemas.commands import CommandRead


class SceneRunCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scene_id: str = Field(min_length=1, max_length=64)
    expected_revision: StrictInt = Field(ge=1)


class SceneRunActionRead(BaseModel):
    position: int
    device_id: str | None
    requested_on: bool | None
    type: Literal["durable_wait"] | None = None
    seconds: int | None = None
    due_at: datetime | None = None
    resume_deadline: datetime | None = None
    status: Literal[
        "ready",
        "completed",
        "cancelled",
        "expired",
        "waiting",
        "dispatch_unknown",
        "blocked",
        "skipped",
        "queued",
        "dispatched",
        "applied",
        "failed",
        "timed_out",
    ]
    failure_code: str | None = None
    receipt: CommandRead | None = None

    @model_serializer(mode="wrap")
    def keep_relay_wire(self, handler):
        data = handler(self)
        if self.type is None:
            for key in ("type", "seconds", "due_at", "resume_deadline"):
                data.pop(key, None)
        return data


class SceneRunRead(BaseModel):
    id: str
    house_id: str
    scene_id: str
    scene_revision: int
    created_at: datetime
    dispatch_deadline: datetime
    plan_deadline: datetime | None = None
    wait_due_at: datetime | None = None
    resume_deadline: datetime | None = None
    cancelled_at: datetime | None = None
    status: Literal[
        "ready",
        "running",
        "succeeded",
        "stopped",
        "unknown",
        "expired",
        "partial",
        "refused",
        "waiting",
        "cancelled",
    ]
    execution_mode: Literal["manual", "server"] = "manual"
    execution_error: str | None = None
    actions: list[SceneRunActionRead]

    @model_serializer(mode="wrap")
    def keep_legacy_wire(self, handler):
        data = handler(self)
        for key in ("plan_deadline", "wait_due_at", "resume_deadline", "cancelled_at"):
            if data.get(key) is None:
                data.pop(key, None)
        return data
