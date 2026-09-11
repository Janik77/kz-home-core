from collections.abc import Awaitable, Callable
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel

EventType = Literal[
    "device_state_changed",
    "device_online",
    "device_offline",
    "scene_started",
    "automation_triggered",
    "automation_completed",
    "automation_failed",
]


class Event(BaseModel):
    type: EventType
    data: dict[str, Any]
    correlation_id: str = ""
    depth: int = 0

    def model_post_init(self, __context: Any) -> None:
        if not self.correlation_id:
            self.correlation_id = str(uuid4())

    def message(self) -> dict[str, Any]:
        return {"type": self.type, **self.data, "correlation_id": self.correlation_id}


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
