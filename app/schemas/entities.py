from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

DeviceType = Literal[
    "light",
    "relay",
    "switch",
    "motion_sensor",
    "temperature_sensor",
    "humidity_sensor",
    "leak_sensor",
    "curtain",
    "thermostat",
    "socket",
]
Capability = Literal[
    "on_off",
    "brightness",
    "open_close",
    "position",
    "temperature",
    "target_temperature",
    "motion",
    "humidity",
    "leak",
]
DeviceState = dict[str, Any]


class ReadSchema(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    created_at: datetime
    updated_at: datetime


class HouseCreate(BaseModel):
    id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=255)


class HouseUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)


class HouseRead(HouseCreate, ReadSchema):
    pass


class FloorCreate(BaseModel):
    id: str = Field(min_length=1, max_length=64)
    house_id: str
    name: str = Field(min_length=1, max_length=255)
    order: int


class FloorUpdate(BaseModel):
    house_id: str | None = None
    name: str | None = Field(default=None, min_length=1, max_length=255)
    order: int | None = None


class FloorRead(FloorCreate, ReadSchema):
    pass


class RoomCreate(BaseModel):
    id: str = Field(min_length=1, max_length=64)
    floor_id: str
    name: str = Field(min_length=1, max_length=255)
    icon: str | None = None


class RoomUpdate(BaseModel):
    floor_id: str | None = None
    name: str | None = Field(default=None, min_length=1, max_length=255)
    icon: str | None = None


class RoomRead(RoomCreate, ReadSchema):
    pass


class DeviceCreate(BaseModel):
    id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=255)
    room_id: str
    type: DeviceType
    state: DeviceState = Field(default_factory=dict)
    online: bool = True
    capabilities: list[Capability] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class DeviceUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    room_id: str | None = None
    type: DeviceType | None = None
    state: DeviceState | None = None
    online: bool | None = None
    capabilities: list[Capability] | None = None
    metadata: dict[str, Any] | None = None


class DeviceRead(DeviceCreate, ReadSchema):
    pass


class DeviceAction(BaseModel):
    device_id: str
    state: DeviceState


class SceneCreate(BaseModel):
    id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=255)
    house_id: str
    actions: list[DeviceAction] = Field(default_factory=list)


class SceneUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    house_id: str | None = None
    actions: list[DeviceAction] | None = None


class SceneRead(SceneCreate, ReadSchema):
    pass


class AutomationTrigger(BaseModel):
    device_id: str
    field: str
    equals: Any


class AutomationCondition(AutomationTrigger):
    pass


class AutomationCreate(BaseModel):
    id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=255)
    house_id: str
    enabled: bool = True
    trigger: AutomationTrigger
    conditions: list[AutomationCondition] = Field(default_factory=list)
    actions: list[DeviceAction] = Field(default_factory=list)


class AutomationUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    house_id: str | None = None
    enabled: bool | None = None
    trigger: AutomationTrigger | None = None
    conditions: list[AutomationCondition] | None = None
    actions: list[DeviceAction] | None = None


class AutomationRead(AutomationCreate, ReadSchema):
    pass
