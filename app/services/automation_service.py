import asyncio
from collections.abc import Callable
from datetime import datetime, time
from typing import Any

from app.core.errors import DefinitionRevisionConflict, InvalidReferenceError
from app.events import Event, EventBus
from app.repositories.automation_repository import AutomationRepository
from app.repositories.house_repository import HouseRepository
from app.schemas import (
    AutomationCreate,
    AutomationRead,
    AutomationUpdate,
    DeviceStateCondition,
    TimeCondition,
)
from app.services.device_service import DeviceService
from app.services.state_validator import StateValidator

MAX_AUTOMATION_DEPTH = 8


def compare(actual: Any, operator: str, expected: Any) -> bool:
    try:
        if operator == "eq":
            return actual == expected
        if operator == "neq":
            return actual != expected
        if operator == "gt":
            return actual > expected
        if operator == "gte":
            return actual >= expected
        if operator == "lt":
            return actual < expected
        if operator == "lte":
            return actual <= expected
    except TypeError:
        return False
    return False


def time_matches(condition: TimeCondition, current: time) -> bool:
    after = time.fromisoformat(condition.after)
    before = time.fromisoformat(condition.before)
    if after <= before:
        return after <= current <= before
    return current >= after or current <= before


class AutomationService:
    def __init__(
        self,
        repository: AutomationRepository,
        houses: HouseRepository,
        devices: DeviceService,
        event_bus: EventBus,
        now: Callable[[], datetime] = datetime.now,
    ) -> None:
        self.repository, self.houses = repository, houses
        self.devices, self.event_bus, self.now = devices, event_bus, now
        self.state_validator = StateValidator()

    def list(self) -> list[AutomationRead]:
        return [AutomationRead.model_validate(item) for item in self.repository.list()]

    def get(self, entity_id: str) -> AutomationRead:
        return AutomationRead.model_validate(self.repository.get(entity_id))

    def create(
        self, data: AutomationCreate, *, authorized_by: str | None = None
    ) -> AutomationRead:
        self._validate(data)
        return AutomationRead.model_validate(
            self.repository.create(
                {**data.model_dump(), "authorized_by": authorized_by}
            )
        )

    def update(
        self,
        entity_id: str,
        data: AutomationUpdate,
        *,
        expected_house_id: str | None = None,
        authorized_by: str | None = None,
    ) -> AutomationRead:
        values = data.model_dump(
            exclude_unset=True, exclude_none=True, exclude={"expected_revision"}
        )
        current = self.get(entity_id)
        if authorized_by is not None and (values.get("enabled") is True or current.enabled):
            values["authorized_by"] = authorized_by
        if (
            data.expected_revision is not None
            and data.expected_revision != current.revision
        ):
            raise DefinitionRevisionConflict()
        candidate = AutomationCreate.model_validate(
            {**current.model_dump(exclude={"created_at", "updated_at"}), **values}
        )
        self._validate(candidate)
        return AutomationRead.model_validate(
            self.repository.update(
                entity_id,
                values,
                expected_revision=current.revision,
                expected_house_id=expected_house_id,
            )
        )

    def delete(self, entity_id: str) -> None:
        self.repository.delete(entity_id)

    def set_enabled(
        self,
        entity_id: str,
        enabled: bool,
        *,
        expected_house_id: str | None = None,
        authorized_by: str | None = None,
    ) -> AutomationRead:
        current = self.get(entity_id)
        if enabled and current.trigger.type == "schedule":
            self._validate(
                AutomationCreate.model_validate(
                    {**current.model_dump(), "enabled": True}
                )
            )
        return AutomationRead.model_validate(
            self.repository.update(
                entity_id,
                {
                    "enabled": enabled,
                    **({"authorized_by": authorized_by} if authorized_by else {}),
                },
                expected_house_id=expected_house_id,
            )
        )

    async def manual_run(self, entity_id: str) -> AutomationRead:
        rule = self.get(entity_id)
        self._validate_read_rule(rule)
        await self._execute(rule, Event(type="automation_triggered", data={}), True)
        return rule

    async def handle_event(self, event: Event) -> None:
        if event.type != "device_state_changed" or event.depth >= MAX_AUTOMATION_DEPTH:
            return
        house_id = event.data.get("house_id")
        if not house_id:
            return
        rules = [
            AutomationRead.model_validate(item)
            for item in self.repository.enabled_for_house(house_id)
        ]
        # Rules are detached Pydantic data; do not retain a read transaction
        # while conditions or delayed actions are evaluated.
        self.repository.session.close()
        for rule in rules:
            trigger = rule.trigger
            if (
                trigger.type == "schedule"
                or trigger.device_id != event.data["device_id"]
            ):
                continue
            if not compare(
                event.data["state"].get(trigger.field), trigger.operator, trigger.value
            ):
                continue
            try:
                self._validate_read_rule(rule)
                if await self._conditions_match(rule):
                    self.repository.session.close()
                    await self._execute(rule, event, True)
            except Exception as error:
                await self._publish_status(
                    "automation_failed", rule, event, {"error": type(error).__name__}
                )

    async def _conditions_match(self, rule: AutomationRead) -> bool:
        for condition in rule.conditions:
            if isinstance(condition, DeviceStateCondition):
                state = self.devices.get(condition.device_id).state
                if not compare(
                    state.get(condition.field), condition.operator, condition.value
                ):
                    return False
            elif isinstance(condition, TimeCondition) and not time_matches(
                condition, self.now().time()
            ):
                return False
        return True

    async def _execute(
        self, rule: AutomationRead, event: Event, publish_trigger: bool
    ) -> None:
        try:
            if any(a.type == "durable_wait" for a in rule.actions):
                raise InvalidReferenceError("Use durable automation executor")
            for action in rule.actions:
                if action.type == "delay":
                    await asyncio.sleep(action.seconds)
                else:
                    await self.devices.update_state(
                        action.device_id,
                        action.state,
                        correlation_id=event.correlation_id,
                        depth=event.depth + 1,
                    )
            if publish_trigger:
                await self._publish_status("automation_triggered", rule, event)
            await self._publish_status("automation_completed", rule, event)
        except Exception as error:
            await self._publish_status(
                "automation_failed", rule, event, {"error": type(error).__name__}
            )

    async def _publish_status(
        self,
        event_type: str,
        rule: AutomationRead,
        source: Event,
        extra: dict[str, Any] | None = None,
    ) -> None:
        await self.event_bus.publish(
            Event(
                type=event_type,
                data={
                    "automation_id": rule.id,
                    "house_id": rule.house_id,
                    **(extra or {}),
                },
                correlation_id=source.correlation_id,
                depth=source.depth,
            )
        )

    def _validate(self, rule: AutomationCreate) -> None:
        if not self.houses.exists(rule.house_id):
            raise InvalidReferenceError("House does not exist")
        scheduled = rule.trigger.type == "schedule"
        if scheduled and rule.enabled and not self.houses.get(rule.house_id).timezone:
            raise InvalidReferenceError("House timezone must be explicitly configured")
        device_ids = [] if scheduled else [rule.trigger.device_id]
        device_ids.extend(
            c.device_id for c in rule.conditions if isinstance(c, DeviceStateCondition)
        )
        device_ids.extend(a.device_id for a in rule.actions if a.type == "device_state")
        for device_id in device_ids:
            self.devices.get(device_id)
            if self.devices.repository.house_id(device_id) != rule.house_id:
                raise InvalidReferenceError(
                    "Automation cannot access a device in another house"
                )
        if not scheduled:
            trigger_device = self.devices.get(rule.trigger.device_id)
            if rule.trigger.type == "numeric_threshold":
                self.state_validator.validate_report(
                    trigger_device, {rule.trigger.field: rule.trigger.value}
                )
            self.state_validator.validate(
                trigger_device, {rule.trigger.field: rule.trigger.value}
            )
        for condition in rule.conditions:
            if isinstance(condition, DeviceStateCondition):
                self.state_validator.validate(
                    self.devices.get(condition.device_id),
                    {condition.field: condition.value},
                )
        for action in rule.actions:
            if action.type == "device_state":
                self.state_validator.validate(
                    self.devices.get(action.device_id), action.state
                )

    def _validate_read_rule(self, rule: AutomationRead) -> None:
        self._validate(
            AutomationCreate.model_validate(
                rule.model_dump(exclude={"created_at", "updated_at"})
            )
        )
