from typing import Literal, TypeAlias

from pydantic import BaseModel, Field


DeviceState: TypeAlias = str | bool


class Device(BaseModel):
    id: str
    name: str
    room_id: str
    type: Literal["light", "relay", "motion_sensor"]
    state: DeviceState
    online: bool = True


class Room(BaseModel):
    id: str
    name: str


class SceneAction(BaseModel):
    device_id: str
    state: DeviceState


class Scene(BaseModel):
    id: str
    name: str
    actions: list[SceneAction] = Field(default_factory=list)


class DeviceStateChanged(BaseModel):
    type: str = "device_state_changed"
    device_id: str
    state: DeviceState
