from collections.abc import Awaitable, Callable
from typing import Any, Literal

from pydantic import BaseModel

EventType = Literal[
    "device_state_changed",
    "device_online",
    "device_offline",
    "scene_started",
    "automation_triggered",
]


class Event(BaseModel):
    type: EventType
    data: dict[str, Any]

    def message(self) -> dict[str, Any]:
        return {"type": self.type, **self.data}


EventHandler = Callable[[Event], Awaitable[None]]


class EventBus:
    """A small sequential event bus for core services and adapters."""

    def __init__(self) -> None:
        self._subscribers: list[EventHandler] = []

    def subscribe(self, handler: EventHandler) -> None:
        self._subscribers.append(handler)

    async def publish(self, event: Event) -> None:
        for handler in tuple(self._subscribers):
            await handler(event)
