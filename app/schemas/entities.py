from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

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
    "illuminance",
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


Operator = Literal["eq", "neq", "gt", "gte", "lt", "lte"]


class DeviceAction(BaseModel):
    type: Literal["device_state"] = "device_state"
    device_id: str = Field(min_length=1, max_length=64)
    state: DeviceState

    @field_validator("state")
    @classmethod
    def state_size(cls, value: DeviceState) -> DeviceState:
        if len(value) > 32:
            raise ValueError("state may contain at most 32 fields")
        return value


class DelayAction(BaseModel):
    type: Literal["delay"]
    seconds: float = Field(gt=0, le=86400)


AutomationAction = DeviceAction | DelayAction


class SceneCreate(BaseModel):
    id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=255)
    house_id: str
    actions: list[DeviceAction] = Field(default_factory=list, max_length=100)


class SceneUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    house_id: str | None = None
    actions: list[DeviceAction] | None = None


class SceneRead(SceneCreate, ReadSchema):
    pass


class AutomationTrigger(BaseModel):
    type: Literal["device_state"] = "device_state"
    device_id: str = Field(min_length=1, max_length=64)
    field: str = Field(min_length=1, max_length=64)
    operator: Operator = "eq"
    value: Any

    @model_validator(mode="before")
    @classmethod
    def migrate_equals(cls, data: Any) -> Any:
        if isinstance(data, dict) and "equals" in data:
            data = dict(data)
            data["value"] = data.pop("equals")
        return data


class DeviceStateCondition(AutomationTrigger):
    type: Literal["device_state"] = "device_state"


class TimeCondition(BaseModel):
    type: Literal["time"]
    after: str
    before: str

    @field_validator("after", "before")
    @classmethod
    def valid_time(cls, value: str) -> str:
        parts = value.split(":")
        if len(parts) != 2 or not all(part.isdigit() for part in parts):
            raise ValueError("time must use HH:MM format")
        hour, minute = map(int, parts)
        if hour > 23 or minute > 59:
            raise ValueError("time must use HH:MM format")
        return f"{hour:02d}:{minute:02d}"


AutomationCondition = DeviceStateCondition | TimeCondition


class AutomationCreate(BaseModel):
    id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=255)
    house_id: str
    enabled: bool = True
    trigger: AutomationTrigger
    conditions: list[AutomationCondition] = Field(default_factory=list, max_length=50)
    actions: list[AutomationAction] = Field(
        default_factory=list, min_length=1, max_length=100
    )

    @model_validator(mode="before")
    @classmethod
    def migrate_legacy_rule(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        data = dict(data)
        trigger = data.get("trigger")
        if isinstance(trigger, dict) and "equals" in trigger:
            trigger = dict(trigger)
            trigger["value"] = trigger.pop("equals")
            trigger.setdefault("operator", "eq")
            trigger.setdefault("type", "device_state")
            data["trigger"] = trigger
        for key in ("conditions", "actions"):
            values = data.get(key)
            if isinstance(values, list):
                data[key] = [
                    (
                        {"type": "device_state", **value}
                        if isinstance(value, dict) and "type" not in value
                        else value
                    )
                    for value in values
                ]
        return data


class AutomationUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    house_id: str | None = None
    enabled: bool | None = None
    trigger: AutomationTrigger | None = None
    conditions: list[AutomationCondition] | None = None
    actions: list[AutomationAction] | None = Field(
        default=None, min_length=1, max_length=100
    )


class AutomationRead(AutomationCreate, ReadSchema):
    pass


class EventLogRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    house_id: str | None
    event_type: str
    entity_id: str | None
    payload: dict[str, Any]
    correlation_id: str
    created_at: datetime
