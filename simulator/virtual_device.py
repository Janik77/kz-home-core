import asyncio
from contextlib import suppress

from app.services.device_service import DeviceService


class VirtualMotionSensor:
    """Alternates a virtual motion sensor between false and true."""

    def __init__(self, devices: DeviceService, device_id: str = "motion1", interval: float = 5.0) -> None:
        self.devices = devices
        self.device_id = device_id
        self.interval = interval

    async def run(self) -> None:
        state = False
        while True:
            await asyncio.sleep(self.interval)
            state = not state
            await self.devices.set_state(self.device_id, state)


async def stop_simulator(task: asyncio.Task[None]) -> None:
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task
