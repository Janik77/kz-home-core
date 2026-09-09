from typing import Protocol

from app.schemas import DeviceRead, DeviceState


class Transport(Protocol):
    async def publish_state(self, device: DeviceRead) -> None: ...

    async def send_command(self, device_id: str, state: DeviceState) -> None: ...
