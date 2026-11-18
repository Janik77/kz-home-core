"""Shared read-only numeric capability contract (software sensor pilot)."""

import math

NUMERIC_RANGES = {
    "temperature": (-100, 150),
    "humidity": (0, 100),
    "illuminance": (0, 200000),
}


def valid_number(field, value):
    return (
        field in NUMERIC_RANGES
        and type(value) in (int, float)
        and math.isfinite(value)
        and NUMERIC_RANGES[field][0] <= value <= NUMERIC_RANGES[field][1]
    )


def validate_numeric_definition(data, *, threshold=False):
    field, value = data.field, data.value
    if not valid_number(field, value):
        raise ValueError("Numeric capability requires a finite, bounded number")
    if threshold:
        h = data.hysteresis
        if type(h) not in (int, float) or not math.isfinite(h) or h <= 0:
            raise ValueError("Positive finite hysteresis required")
        if data.operator not in {"lt", "gt"}:
            raise ValueError("Numeric threshold supports strict lt/gt only")
        reset = value + h if data.operator == "lt" else value - h
        if not valid_number(field, reset):
            raise ValueError("Hysteresis reset must be within sensor bounds")
    return data
