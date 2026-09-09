from collections.abc import Iterable

from app.models import Scene
from app.services.device_service import DeviceService


class SceneNotFoundError(KeyError):
    """Raised when a scene id is unknown."""


class SceneService:
    def __init__(self, device_service: DeviceService, scenes: Iterable[Scene] = ()) -> None:
        self._device_service = device_service
        self._scenes = {scene.id: scene for scene in scenes}

    def list(self) -> list[Scene]:
        return [scene.model_copy(deep=True) for scene in self._scenes.values()]

    async def run(self, scene_id: str) -> Scene:
        scene = self._scenes.get(scene_id)
        if scene is None:
            raise SceneNotFoundError(scene_id)
        for action in scene.actions:
            await self._device_service.set_state(action.device_id, action.state)
        return scene.model_copy(deep=True)
