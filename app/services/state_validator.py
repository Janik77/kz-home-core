from app.core.errors import InvalidReferenceError
from app.schemas import DeviceRead, DeviceState

FIELD_CAPABILITIES = {
    "on": "on_off",
    "brightness": "brightness",
    "position": "position",
    "temperature": "temperature",
    "target_temperature": "target_temperature",
    "motion": "motion",
    "humidity": "humidity",
    "leak": "leak",
    "illuminance": "illuminance",
}


class StateValidator:
    """Allows commands only for state fields advertised by a device."""

    def validate(self, device: DeviceRead, state: DeviceState) -> None:
        self.validate_capabilities(device.capabilities, state)

    def validate_capabilities(
        self, capabilities: list[str], state: DeviceState
    ) -> None:
        unsupported = [
            field
            for field in state
            if FIELD_CAPABILITIES.get(field) not in capabilities
        ]
        if unsupported:
            fields = ", ".join(sorted(unsupported))
            raise InvalidReferenceError(f"Unsupported state fields: {fields}")
