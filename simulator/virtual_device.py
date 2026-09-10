import asyncio
import logging
from contextlib import suppress

from sqlalchemy.orm import Session, sessionmaker

from app.core.errors import EntityNotFoundError
from app.events import EventBus
from app.repositories import DeviceRepository, RoomRepository
from app.services.device_service import DeviceService

logger = logging.getLogger(__name__)


class VirtualDeviceSimulator:
    """A deterministic, slow demo cycle for seeded virtual sensors."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        event_bus: EventBus,
        interval: float = 5.0,
    ) -> None:
        self.session_factory = session_factory
        self.event_bus = event_bus
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
                    with self.session_factory() as session:
                        devices = DeviceService(
                            DeviceRepository(session),
                            RoomRepository(session),
                            self.event_bus,
                        )
                        await devices.update_state(device_id, state)
                except EntityNotFoundError:
                    # The application also supports an intentionally empty database.
                    logger.warning(
                        "Virtual device %s is not seeded", device_id, exc_info=True
                    )
                except Exception:
                    logger.exception("Virtual device step failed for %s", device_id)


async def stop_simulator(task: asyncio.Task[None]) -> None:
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task
