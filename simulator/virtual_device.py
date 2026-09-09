import asyncio
from contextlib import suppress

from app.core.errors import EntityNotFoundError
from app.services.device_service import DeviceService


class VirtualDeviceSimulator:
    """A deterministic, slow demo cycle for seeded virtual sensors."""

    def __init__(self, devices: DeviceService, interval: float = 5.0) -> None:
        self.devices = devices
        self.interval = interval
        self._steps = (
            ("hall_motion", {"motion": True}),
            ("bedroom_temperature", {"temperature": 23.0}),
            ("hall_motion", {"motion": False}),
            ("main_leak_sensor", {"leak": True}),
            ("main_leak_sensor", {"leak": False}),
            ("bedroom_temperature", {"temperature": 22.5}),
        )

    async def run(self) -> None:
        while True:
            for device_id, state in self._steps:
                await asyncio.sleep(self.interval)
                try:
                    await self.devices.update_state(device_id, state)
                except EntityNotFoundError:
                    # The application also supports an intentionally empty database.
                    continue


async def stop_simulator(task: asyncio.Task[None]) -> None:
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task
