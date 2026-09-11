from typing import Any

from app.core.errors import InvalidReferenceError
from app.events import Event, EventBus
from app.repositories.device_repository import DeviceRepository
from app.repositories.room_repository import RoomRepository
from app.schemas import DeviceCreate, DeviceRead, DeviceState, DeviceUpdate
from app.services.state_validator import StateValidator
from app.transports.base import Transport


class DeviceService:
    def __init__(
        self,
        repository: DeviceRepository,
        rooms: RoomRepository,
        event_bus: EventBus,
        transport: Transport | None = None,
    ) -> None:
        self.repository = repository
        self.rooms = rooms
        self.event_bus = event_bus
        self.transport = transport
        self.state_validator = StateValidator()

    def list(
        self,
        house_id: str | None = None,
        room_id: str | None = None,
        device_type: str | None = None,
        online: bool | None = None,
    ) -> list[DeviceRead]:
        return [
            self._read(item)
            for item in self.repository.filtered(house_id, room_id, device_type, online)
        ]

    def get(self, device_id: str) -> DeviceRead:
        return self._read(self.repository.get(device_id))

    def create(self, data: DeviceCreate) -> DeviceRead:
        self._validate_room(data.room_id)
        self.state_validator.validate_capabilities(data.capabilities, data.state)
        return self._read(self.repository.create(data.model_dump()))

    def update(self, device_id: str, data: DeviceUpdate) -> DeviceRead:
        values = data.model_dump(exclude_unset=True, exclude_none=True)
        if "room_id" in values:
            self._validate_room(values["room_id"])
        if "state" in values or "capabilities" in values:
            current = self.get(device_id)
            self.state_validator.validate_capabilities(
                values.get("capabilities", current.capabilities),
                values.get("state", current.state),
            )
        return self._read(self.repository.update(device_id, values))

    def delete(self, device_id: str) -> None:
        self.repository.delete(device_id)

    async def update_state(
        self,
        device_id: str,
        patch: DeviceState,
        *,
        correlation_id: str = "",
        depth: int = 0,
    ) -> DeviceRead:
        entity = self.repository.get(device_id)
        current = self._read(entity)
        if self.transport is not None:
            self.state_validator.validate_command(current, patch)
            await self.transport.send_command(
                self.repository.house_id(device_id),
                device_id,
                patch,
                correlation_id=correlation_id,
            )
            return current
        self.state_validator.validate(current, patch)
        return await self._persist_state(
            entity, device_id, patch, correlation_id=correlation_id, depth=depth
        )

    async def report_state(
        self,
        house_id: str,
        device_id: str,
        patch: DeviceState,
        *,
        correlation_id: str = "",
    ) -> DeviceRead:
        entity = self.repository.get(device_id)
        self.require_house(house_id, device_id)
        self.state_validator.validate_report(self._read(entity), patch)
        return await self._persist_state(
            entity, device_id, patch, correlation_id=correlation_id
        )

    async def _persist_state(
        self,
        entity: Any,
        device_id: str,
        patch: DeviceState,
        *,
        correlation_id: str,
        depth: int = 0,
    ) -> DeviceRead:
        changed = {
            key: value for key, value in patch.items() if entity.state.get(key) != value
        }
        if not changed:
            return self._read(entity)
        state = {**entity.state, **changed}
        updated = self.repository.update(device_id, {"state": state})
        await self.event_bus.publish(
            Event(
                type="device_state_changed",
                data={
                    "device_id": device_id,
                    "house_id": self.repository.house_id(device_id),
                    "state": changed,
                },
                correlation_id=correlation_id,
                depth=depth,
            )
        )
        return self._read(updated)

    def require_house(self, house_id: str, device_id: str) -> DeviceRead:
        device = self.get(device_id)
        if self.repository.house_id(device_id) != house_id:
            raise InvalidReferenceError("Device does not belong to MQTT topic house")
        return device

    async def update_status(
        self, house_id: str, device_id: str, online: bool, last_seen: Any
    ) -> bool:
        device = self.require_house(house_id, device_id)
        changed = device.online != online
        metadata = {**device.metadata, "last_seen": last_seen.isoformat()}
        self.repository.update(
            device_id, {"online": online, "metadata": metadata}
        )
        return changed

    def _validate_room(self, room_id: str) -> None:
        from app.core.errors import InvalidReferenceError

        if not self.rooms.exists(room_id):
            raise InvalidReferenceError("Room does not exist")

    @staticmethod
    def _read(entity: Any) -> DeviceRead:
        return DeviceRead.model_validate(
            {
                "id": entity.id,
                "name": entity.name,
                "room_id": entity.room_id,
                "type": entity.type,
                "state": entity.state,
                "online": entity.online,
                "capabilities": entity.capabilities,
                "metadata": entity.metadata_,
                "created_at": entity.created_at,
                "updated_at": entity.updated_at,
            }
        )
