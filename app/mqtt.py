from collections.abc import Awaitable, Callable
from typing import Protocol

MessageHandler = Callable[[str, str], Awaitable[None]]


def state_topic(home_id: str, room_id: str, device_id: str) -> str:
    return f"kzhome/{home_id}/{room_id}/{device_id}/state"


def command_topic(home_id: str, room_id: str, device_id: str) -> str:
    return f"kzhome/{home_id}/{room_id}/{device_id}/set"


class MQTTClient(Protocol):
    """Transport contract for a future real MQTT implementation."""

    async def connect(self) -> None: ...

    async def disconnect(self) -> None: ...

    async def publish(self, topic: str, payload: str) -> None: ...

    async def subscribe(self, topic: str, handler: MessageHandler) -> None: ...


class NullMQTTClient:
    """No-op transport which keeps MQTT optional for local development."""

    async def connect(self) -> None:
        pass

    async def disconnect(self) -> None:
        pass

    async def publish(self, topic: str, payload: str) -> None:
        pass

    async def subscribe(self, topic: str, handler: MessageHandler) -> None:
        pass
