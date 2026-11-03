from typing import Protocol
from datetime import datetime

from app.schemas import DeviceState


class Transport(Protocol):
    async def authorize_command(self, house_id: str, device_id: str) -> None: ...

    async def send_command(
        self,
        house_id: str,
        device_id: str,
        state: DeviceState,
        *,
        correlation_id: str = "",
        command_id: str = "",
        timestamp: datetime | None = None,
        expires_at: datetime | None = None,
    ) -> str: ...
