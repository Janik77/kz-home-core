from typing import Protocol

from app.schemas import DeviceState


class Transport(Protocol):
    async def send_command(
        self,
        house_id: str,
        device_id: str,
        state: DeviceState,
        *,
        correlation_id: str = "",
    ) -> str: ...
