from app.core.errors import InvalidReferenceError
from app.events import Event, EventBus
from app.repositories.house_repository import HouseRepository
from app.repositories.scene_repository import SceneRepository
from app.schemas import SceneCreate, SceneRead, SceneUpdate
from app.services.device_service import DeviceService


class SceneService:
    def __init__(
        self,
        repository: SceneRepository,
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

    def list(self) -> list[SceneRead]:
        return [SceneRead.model_validate(item) for item in self.repository.list()]

    def get(self, scene_id: str) -> SceneRead:
        return SceneRead.model_validate(self.repository.get(scene_id))

    def create(self, data: SceneCreate) -> SceneRead:
        self._house(data.house_id)
        return SceneRead.model_validate(self.repository.create(data.model_dump()))

    def update(self, scene_id: str, data: SceneUpdate) -> SceneRead:
        values = data.model_dump(exclude_unset=True, exclude_none=True)
        if "house_id" in values:
            self._house(values["house_id"])
        return SceneRead.model_validate(self.repository.update(scene_id, values))

    def delete(self, scene_id: str) -> None:
        self.repository.delete(scene_id)

    async def run(self, scene_id: str) -> SceneRead:
        scene = self.get(scene_id)
        for action in scene.actions:
            await self.devices.update_state(action.device_id, action.state)
        await self.event_bus.publish(
            Event(type="scene_started", data={"scene_id": scene.id})
        )
        return scene

    def _house(self, house_id: str) -> None:
        if not self.houses.exists(house_id):
            raise InvalidReferenceError("House does not exist")
