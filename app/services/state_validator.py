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
READ_ONLY_FIELDS = {
    "temperature",
    "motion",
    "humidity",
    "leak",
    "illuminance",
}
BOOLEAN_FIELDS = {"on", "motion", "leak"}
INTEGER_RANGES = {"brightness": (0, 100), "position": (0, 100)}


class StateValidator:
    """Allows commands only for state fields advertised by a device."""

    def validate(self, device: DeviceRead, state: DeviceState) -> None:
        self.validate_capabilities(device.capabilities, state)

    def validate_command(self, device: DeviceRead, state: DeviceState) -> None:
        self.validate(device, state)
        self._validate_values(state)
        read_only = sorted(set(state) & READ_ONLY_FIELDS)
        if read_only:
            raise InvalidReferenceError(
                f"Read-only state fields: {', '.join(read_only)}"
            )
        if not state:
            raise InvalidReferenceError("Command state must not be empty")

    def validate_report(self, device: DeviceRead, state: DeviceState) -> None:
        self.validate(device, state)
        self._validate_values(state)

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

    @staticmethod
    def _validate_values(state: DeviceState) -> None:
        for field, value in state.items():
            if value is None:
                raise InvalidReferenceError(f"Invalid value for state field: {field}")
            if field in BOOLEAN_FIELDS and type(value) is not bool:
                raise InvalidReferenceError(f"Invalid value for state field: {field}")
            if field in INTEGER_RANGES:
                minimum, maximum = INTEGER_RANGES[field]
                if type(value) is not int or not minimum <= value <= maximum:
                    raise InvalidReferenceError(
                        f"Invalid value for state field: {field}"
                    )
