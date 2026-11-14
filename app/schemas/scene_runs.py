from datetime import datetime
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, StrictInt
from app.schemas.commands import CommandRead


class SceneRunCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scene_id: str = Field(min_length=1, max_length=64)
    expected_revision: StrictInt = Field(ge=1)


class SceneRunActionRead(BaseModel):
    position: int
    device_id: str
    requested_on: bool
    status: Literal[
        "ready",
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


class SceneRunRead(BaseModel):
    id: str
    house_id: str
    scene_id: str
    scene_revision: int
    created_at: datetime
    dispatch_deadline: datetime
    status: Literal[
        "ready",
        "running",
        "succeeded",
        "stopped",
        "unknown",
        "expired",
        "partial",
        "refused",
    ]
    execution_mode: Literal["manual", "server"] = "manual"
    execution_error: str | None = None
    actions: list[SceneRunActionRead]
