import asyncio
from collections.abc import Awaitable, Callable, Iterable

from app.models import Device, DeviceState, DeviceStateChanged

EventHandler = Callable[[DeviceStateChanged], Awaitable[None]]


class DeviceNotFoundError(KeyError):
    """Raised when a device id is unknown."""


class DeviceService:
    """Concurrency-safe, in-memory device storage and event dispatcher."""

    def __init__(self, devices: Iterable[Device] = ()) -> None:
        self._devices = {device.id: device for device in devices}
        self._handlers: list[EventHandler] = []
        self._lock = asyncio.Lock()

    def subscribe(self, handler: EventHandler) -> None:
        self._handlers.append(handler)

    async def list(self) -> list[Device]:
        async with self._lock:
            return [device.model_copy(deep=True) for device in self._devices.values()]

    async def get(self, device_id: str) -> Device:
        async with self._lock:
            device = self._devices.get(device_id)
            if device is None:
                raise DeviceNotFoundError(device_id)
            return device.model_copy(deep=True)

    async def set_state(self, device_id: str, state: DeviceState) -> Device:
        async with self._lock:
            device = self._devices.get(device_id)
            if device is None:
                raise DeviceNotFoundError(device_id)
            if device.state == state:
                return device.model_copy(deep=True)
            device.state = state
            result = device.model_copy(deep=True)

        event = DeviceStateChanged(device_id=device_id, state=state)
        for handler in tuple(self._handlers):
            await handler(event)
        return result
