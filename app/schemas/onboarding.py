from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr

# The existing database IDs are limited to 64 characters. This is a subset of
# the v1 adapter policy, without accepting topic strings as identity.
TopicID = Annotated[
    str, Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
]
HardwareID = Annotated[
    str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")
]
HardwareModel = Literal["esp32-c6-relay-v1"]
Lifecycle = Literal["provisioning", "active", "inactive", "revoked"]


class InventoryRegistration(BaseModel):
    model_config = ConfigDict(extra="forbid")
    device_id: TopicID
    hardware_id: HardwareID
    hardware_model: HardwareModel
    claim_code: SecretStr = Field(min_length=43, max_length=43)


class DeviceClaim(BaseModel):
    model_config = ConfigDict(extra="forbid")
    hardware_id: HardwareID
    claim_code: SecretStr = Field(min_length=43, max_length=43)
    room_id: TopicID
    name: str = Field(min_length=1, max_length=255)


class DeviceActivation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    broker_access_confirmed: Literal[True]


class PhysicalDeviceRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    device_id: str
    house_id: str
    hardware_model: str
    protocol_version: Literal["v1"]
    status: Lifecycle
    created_at: datetime
    updated_at: datetime
