from typing import Any
from uuid import uuid4

from app.events import Event
from app.repositories.event_log_repository import EventLogRepository
from app.schemas import EventLogRead

IMPORTANT_EVENTS = {
    "device_state_changed",
    "device_ack_received",
    "device_telemetry_received",
    "device_status_changed",
    "scene_started",
    "automation_triggered",
    "automation_completed",
    "automation_failed",
}
SENSITIVE_KEYS = {"password", "token", "secret", "database_url", "authorization"}


def sanitized(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: sanitized(item)
            for key, item in value.items()
            if key.lower() not in SENSITIVE_KEYS
        }
    if isinstance(value, list):
        return [sanitized(item) for item in value]
    return value


class EventLogService:
    def __init__(self, repository: EventLogRepository) -> None:
        self.repository = repository

    def list(
        self, house_id: str | None, event_type: str | None, limit: int
    ) -> list[EventLogRead]:
        return [
            EventLogRead.model_validate(item)
            for item in self.repository.filtered(house_id, event_type, limit)
        ]

    async def handle_event(self, event: Event) -> None:
        if event.type not in IMPORTANT_EVENTS:
            return
        entity_id = event.data.get("device_id") or event.data.get("scene_id")
        entity_id = entity_id or event.data.get("automation_id")
        self.repository.create(
            {
                "id": str(uuid4()),
                "house_id": event.data.get("house_id"),
                "event_type": event.type,
                "entity_id": entity_id,
                "payload": sanitized(event.data),
                "correlation_id": event.correlation_id,
            }
        )
