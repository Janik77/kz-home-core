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
        and NUMERIC_RANGES[field][0] <= value <= NUMERIC_RANGES[field][1]
        and math.isfinite(value)
    )


def validate_numeric_definition(data, *, threshold=False):
    field, value = data.field, data.value
    if not valid_number(field, value):
        raise ValueError("Numeric capability requires a finite, bounded number")
    if threshold:
        h = data.hysteresis
        span = NUMERIC_RANGES[field][1] - NUMERIC_RANGES[field][0]
        if type(h) not in (int, float) or not 0 < h <= span or not math.isfinite(h):
            raise ValueError("Positive finite hysteresis required")
        if data.operator not in {"lt", "gt"}:
            raise ValueError("Numeric threshold supports strict lt/gt only")
        reset = value + h if data.operator == "lt" else value - h
        if not valid_number(field, reset):
            raise ValueError("Hysteresis reset must be within sensor bounds")
    return data
