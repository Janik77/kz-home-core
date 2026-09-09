from collections.abc import Iterable

from app.events import Event, EventBus
from app.models import Scene
from app.services.device_service import DeviceService


class SceneNotFoundError(KeyError):
    pass


class SceneService:
    def __init__(self, devices: DeviceService, event_bus: EventBus, scenes: Iterable[Scene] = ()) -> None:
        self._devices = devices
        self._event_bus = event_bus
        self._scenes = {scene.id: scene for scene in scenes}

    def list(self) -> list[Scene]:
        return [scene.model_copy(deep=True) for scene in self._scenes.values()]

    async def run(self, scene_id: str) -> Scene:
        scene = self._scenes.get(scene_id)
        if scene is None:
            raise SceneNotFoundError(scene_id)
        for action in scene.actions:
            await self._devices.update_state(action.device_id, action.state)
        await self._event_bus.publish(Event(type="scene_started", data={"scene_id": scene.id}))
        return scene.model_copy(deep=True)
