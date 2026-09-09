from typing import Protocol

from app.models import Device, DeviceState


class Transport(Protocol):
    async def publish_state(self, device: Device) -> None: ...

    async def send_command(self, device_id: str, state: DeviceState) -> None: ...
