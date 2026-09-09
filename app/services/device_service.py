import asyncio
from collections.abc import Iterable

from app.events import Event, EventBus
from app.models import Device, DeviceState


class DeviceNotFoundError(KeyError):
    pass


class DeviceService:
    """Protocol-independent in-memory device registry."""

    def __init__(self, event_bus: EventBus, devices: Iterable[Device] = ()) -> None:
        self._event_bus = event_bus
        self._devices = {device.id: device for device in devices}
        self._lock = asyncio.Lock()

    async def list(self) -> list[Device]:
        async with self._lock:
            return [device.model_copy(deep=True) for device in self._devices.values()]

    async def get(self, device_id: str) -> Device:
        async with self._lock:
            return self._copy_device(device_id)

    async def update_state(self, device_id: str, patch: DeviceState) -> Device:
        async with self._lock:
            device = self._find(device_id)
            changed = {key: value for key, value in patch.items() if device.state.get(key) != value}
            if not changed:
                return device.model_copy(deep=True)
            device.state.update(changed)
            result = device.model_copy(deep=True)

        await self._event_bus.publish(
            Event(type="device_state_changed", data={"device_id": device_id, "state": changed})
        )
        return result

    async def set_online(self, device_id: str, online: bool) -> Device:
        async with self._lock:
            device = self._find(device_id)
            if device.online == online:
                return device.model_copy(deep=True)
            device.online = online
            result = device.model_copy(deep=True)
        event_type = "device_online" if online else "device_offline"
        await self._event_bus.publish(Event(type=event_type, data={"device_id": device_id}))
        return result

    def _find(self, device_id: str) -> Device:
        device = self._devices.get(device_id)
        if device is None:
            raise DeviceNotFoundError(device_id)
        return device

    def _copy_device(self, device_id: str) -> Device:
        return self._find(device_id).model_copy(deep=True)
