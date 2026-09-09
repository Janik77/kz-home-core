from typing import Any, Literal

from pydantic import BaseModel, Field

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
DeviceState = dict[str, Any]


class House(BaseModel):
    id: str
    name: str


class Floor(BaseModel):
    id: str
    house_id: str
    name: str
    order: int


class Room(BaseModel):
    id: str
    floor_id: str
    name: str
    icon: str | None = None


class Device(BaseModel):
    id: str
    name: str
    room_id: str
    type: DeviceType
    state: DeviceState = Field(default_factory=dict)
    online: bool = True
    capabilities: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class DeviceAction(BaseModel):
    device_id: str
    state: DeviceState


class Scene(BaseModel):
    id: str
    name: str
    house_id: str
    actions: list[DeviceAction] = Field(default_factory=list)


class AutomationTrigger(BaseModel):
    device_id: str
    field: str
    equals: Any


class AutomationCondition(BaseModel):
    device_id: str
    field: str
    equals: Any


class Automation(BaseModel):
    id: str
    name: str
    enabled: bool = True
    trigger: AutomationTrigger
    conditions: list[AutomationCondition] = Field(default_factory=list)
    actions: list[DeviceAction] = Field(default_factory=list)
