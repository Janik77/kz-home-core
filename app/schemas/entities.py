from datetime import datetime
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
    model_serializer,
)

DeviceType = Literal[
    "light",
    "relay",
    "switch",
    "motion_sensor",
    "temperature_sensor",
    "humidity_sensor",
    "illuminance_sensor",
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


def valid_timezone(value):
    if value is not None:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            ZoneInfo(value)
        except (ValueError, ZoneInfoNotFoundError, TypeError) as error:
            raise ValueError("timezone must be an IANA timezone") from error
    return value


class HouseCreate(BaseModel):
    timezone: str | None = Field(default=None, max_length=128)
    _timezone = field_validator("timezone")(valid_timezone)
    id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=255)


class HouseUpdate(BaseModel):
    timezone: str | None = Field(default=None, max_length=128)
    expected_timezone_revision: int | None = Field(default=None, ge=1, strict=True)
    _timezone = field_validator("timezone")(valid_timezone)

    @model_validator(mode="after")
    def timezone_cas(self):
        if (
            "timezone" in self.model_fields_set
            and self.expected_timezone_revision is None
        ):
            raise ValueError("timezone edit requires expected_timezone_revision")
        return self

    name: str | None = Field(default=None, min_length=1, max_length=255)


class HouseRead(HouseCreate, ReadSchema):
    timezone_revision: int = Field(ge=1)


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
    numeric_observed_at: dict[str, str | None] = Field(default_factory=dict)


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


class DurableWaitAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["durable_wait"]
    seconds: int = Field(ge=1, le=3600, strict=True)


AutomationAction = DeviceAction | DelayAction | DurableWaitAction


class SceneCreate(BaseModel):
    id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=255)
    house_id: str
    actions: list[DeviceAction | DurableWaitAction] = Field(
        default_factory=list, max_length=100
    )

    @model_validator(mode="after")
    def durable_plan(self):
        from app.services.wait_contract import wait_plan

        wait_plan(self.actions)
        return self


class SceneUpdate(BaseModel):
    expected_revision: int | None = Field(default=None, ge=1, strict=True)
    name: str | None = Field(default=None, min_length=1, max_length=255)
    house_id: str | None = None
    actions: list[DeviceAction | DurableWaitAction] | None = None

    @field_validator("actions")
    @classmethod
    def validate_wait_update(cls, actions):
        if actions is not None:
            from app.services.wait_contract import wait_plan

            wait_plan(actions)
        return actions


class SceneRead(SceneCreate, ReadSchema):
    revision: int = Field(ge=1)


class AutomationTrigger(BaseModel):
    type: Literal["device_state", "numeric_threshold"] = "device_state"
    device_id: str = Field(min_length=1, max_length=64)
    field: str = Field(min_length=1, max_length=64)
    operator: Operator = "eq"
    value: Any
    hysteresis: Any = None

    @model_serializer(mode="wrap")
    def canonical_definition(self, handler):
        data = handler(self)
        if self.hysteresis is None:
            data.pop("hysteresis", None)
        return data

    @model_validator(mode="after")
    def numeric_contract(self):
        from app.services.numeric_state import validate_numeric_definition

        if self.type == "numeric_threshold":
            validate_numeric_definition(self, threshold=True)
        elif self.type == "numeric_state":
            validate_numeric_definition(self)
            if self.hysteresis is not None:
                raise ValueError("Hysteresis requires numeric_threshold")
        elif self.hysteresis is not None:
            raise ValueError("Hysteresis requires numeric_threshold")
        return self

    @model_validator(mode="before")
    @classmethod
    def migrate_equals(cls, data: Any) -> Any:
        if isinstance(data, dict) and data.get("type") in {
            "numeric_threshold",
            "numeric_state",
        }:
            allowed = {"type", "device_id", "field", "operator", "value", "hysteresis"}
            if set(data) - allowed:
                raise ValueError("Unknown numeric definition fields are not supported")
        if isinstance(data, dict) and "equals" in data:
            data = dict(data)
            data["value"] = data.pop("equals")
        return data


class DeviceStateCondition(AutomationTrigger):
    type: Literal["device_state", "numeric_state"] = "device_state"


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


class ScheduleTrigger(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["schedule"]
    time: str = Field(pattern=r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$")
    weekdays: list[int] = Field(min_length=1, max_length=7)

    @field_validator("weekdays", mode="before")
    @classmethod
    def valid_days(cls, value):
        if not isinstance(value, list) or any(
            type(day) is not int or day not in range(7) for day in value
        ):
            raise ValueError("weekdays must be integers 0 (Monday) through 6 (Sunday)")
        if len(set(value)) != len(value):
            raise ValueError("weekdays must be unique")
        return sorted(value)


class AutomationCreate(BaseModel):
    id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=255)
    house_id: str
    enabled: bool = True
    trigger: AutomationTrigger | ScheduleTrigger
    conditions: list[AutomationCondition] = Field(default_factory=list, max_length=50)
    actions: list[AutomationAction] = Field(
        default_factory=list, min_length=1, max_length=100
    )

    @model_validator(mode="after")
    def durable_plan(self):
        from app.services.wait_contract import wait_plan

        wait_plan(self.actions)
        return self

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
    expected_revision: int | None = Field(default=None, ge=1, strict=True)
    name: str | None = Field(default=None, min_length=1, max_length=255)
    house_id: str | None = None
    enabled: bool | None = None
    trigger: AutomationTrigger | ScheduleTrigger | None = None
    conditions: list[AutomationCondition] | None = None
    actions: list[AutomationAction] | None = Field(
        default=None, min_length=1, max_length=100
    )

    @field_validator("actions")
    @classmethod
    def validate_wait_update(cls, actions):
        if actions is not None:
            from app.services.wait_contract import wait_plan

            wait_plan(actions)
        return actions


class AutomationRead(AutomationCreate, ReadSchema):
    revision: int = Field(ge=1)


class EventLogRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    house_id: str | None
    event_type: str
    entity_id: str | None
    payload: dict[str, Any]
    correlation_id: str
    created_at: datetime
