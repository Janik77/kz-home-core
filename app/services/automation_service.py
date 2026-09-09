from collections.abc import Iterable

from app.events import Event, EventBus
from app.models import Automation
from app.services.device_service import DeviceService


class AutomationService:
    """Evaluates enabled equality-triggered automation rules."""

    def __init__(self, devices: DeviceService, event_bus: EventBus, rules: Iterable[Automation] = ()) -> None:
        self._devices = devices
        self._event_bus = event_bus
        self._rules = {rule.id: rule for rule in rules}

    def list(self) -> list[Automation]:
        return [rule.model_copy(deep=True) for rule in self._rules.values()]

    async def handle_event(self, event: Event) -> None:
        if event.type != "device_state_changed":
            return
        device_id = event.data["device_id"]
        changed_state = event.data["state"]
        for rule in self._rules.values():
            trigger = rule.trigger
            if not rule.enabled or trigger.device_id != device_id:
                continue
            if changed_state.get(trigger.field) != trigger.equals:
                continue
            if not await self._conditions_match(rule):
                continue
            for action in rule.actions:
                await self._devices.update_state(action.device_id, action.state)
            await self._event_bus.publish(
                Event(type="automation_triggered", data={"automation_id": rule.id})
            )

    async def _conditions_match(self, rule: Automation) -> bool:
        for condition in rule.conditions:
            device = await self._devices.get(condition.device_id)
            if device.state.get(condition.field) != condition.equals:
                return False
        return True
