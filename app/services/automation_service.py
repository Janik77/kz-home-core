from app.core.errors import InvalidReferenceError
from app.events import Event, EventBus
from app.repositories.automation_repository import AutomationRepository
from app.repositories.house_repository import HouseRepository
from app.schemas import AutomationCreate, AutomationRead, AutomationUpdate
from app.services.device_service import DeviceService


class AutomationService:
    def __init__(
        self,
        repository: AutomationRepository,
        houses: HouseRepository,
        devices: DeviceService,
        event_bus: EventBus,
    ) -> None:
        self.repository, self.houses, self.devices, self.event_bus = (
            repository,
            houses,
            devices,
            event_bus,
        )

    def list(self) -> list[AutomationRead]:
        return [AutomationRead.model_validate(item) for item in self.repository.list()]

    def get(self, entity_id: str) -> AutomationRead:
        return AutomationRead.model_validate(self.repository.get(entity_id))

    def create(self, data: AutomationCreate) -> AutomationRead:
        self._house(data.house_id)
        return AutomationRead.model_validate(self.repository.create(data.model_dump()))

    def update(self, entity_id: str, data: AutomationUpdate) -> AutomationRead:
        values = data.model_dump(exclude_unset=True, exclude_none=True)
        if "house_id" in values:
            self._house(values["house_id"])
        return AutomationRead.model_validate(self.repository.update(entity_id, values))

    def delete(self, entity_id: str) -> None:
        self.repository.delete(entity_id)

    async def handle_event(self, event: Event) -> None:
        if event.type != "device_state_changed":
            return
        for rule in self.list():
            trigger = rule.trigger
            if not rule.enabled or trigger.device_id != event.data["device_id"]:
                continue
            if event.data["state"].get(trigger.field) != trigger.equals:
                continue
            if not all(
                self.devices.get(c.device_id).state.get(c.field) == c.equals
                for c in rule.conditions
            ):
                continue
            for action in rule.actions:
                await self.devices.update_state(action.device_id, action.state)
            await self.event_bus.publish(
                Event(type="automation_triggered", data={"automation_id": rule.id})
            )

    def _house(self, house_id: str) -> None:
        if not self.houses.exists(house_id):
            raise InvalidReferenceError("House does not exist")
