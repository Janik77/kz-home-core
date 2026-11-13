from app.core.errors import DefinitionRevisionConflict, InvalidReferenceError
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
        self._validate(data)
        return SceneRead.model_validate(self.repository.create(data.model_dump()))

    def update(
        self, scene_id: str, data: SceneUpdate, *, expected_house_id: str | None = None
    ) -> SceneRead:
        values = data.model_dump(
            exclude_unset=True, exclude_none=True, exclude={"expected_revision"}
        )
        current = self.get(scene_id)
        if (
            data.expected_revision is not None
            and data.expected_revision != current.revision
        ):
            raise DefinitionRevisionConflict()
        candidate = SceneCreate.model_validate(
            {**current.model_dump(exclude={"created_at", "updated_at"}), **values}
        )
        self._validate(candidate)
        return SceneRead.model_validate(
            self.repository.update(
                scene_id,
                values,
                expected_revision=current.revision,
                expected_house_id=expected_house_id,
            )
        )

    def delete(self, scene_id: str) -> None:
        self.repository.delete(scene_id)

    async def run(self, scene_id: str) -> SceneRead:
        scene = self.get(scene_id)
        self._validate(scene)
        for action in scene.actions:
            await self.devices.update_state(action.device_id, action.state)
        await self.event_bus.publish(
            Event(
                type="scene_started",
                data={"scene_id": scene.id, "house_id": scene.house_id},
            )
        )
        return scene

    def _validate(self, scene: SceneCreate) -> None:
        if not self.houses.exists(scene.house_id):
            raise InvalidReferenceError("House does not exist")
        for action in scene.actions:
            if self.devices.repository.house_id(action.device_id) != scene.house_id:
                raise InvalidReferenceError(
                    "Scene cannot access a device in another house"
                )
