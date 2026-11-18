"""Explicit durable waits; old delay actions never become runnable implicitly."""

MAX_WAIT_SECONDS = 3600
RESUME_GRACE_SECONDS = 30


def wait_plan(actions):
    values = [a.model_dump() if hasattr(a, "model_dump") else a for a in actions]
    if not any(a.get("type") == "durable_wait" for a in values):
        return 0
    if not 3 <= len(values) <= 16:
        raise ValueError("Durable plans require 3 to 16 total steps")
    if (
        values[0].get("type", "device_state") != "device_state"
        or values[-1].get("type", "device_state") != "device_state"
    ):
        raise ValueError("First and last steps must be relay commands")
    total, previous_wait = 0, False
    for action in values:
        waiting = action.get("type") == "durable_wait"
        if waiting:
            seconds = action.get("seconds")
            if (
                set(action) != {"type", "seconds"}
                or type(seconds) is not int
                or not 1 <= seconds <= MAX_WAIT_SECONDS
                or previous_wait
            ):
                raise ValueError(
                    "Wait requires integer seconds 1..3600 and cannot follow another wait"
                )
            total += seconds
        elif (
            action.get("type", "device_state") != "device_state"
            or set(action) - {"type", "device_id", "state"}
            or not isinstance(action.get("state"), dict)
            or set(action.get("state", {})) != {"on"}
            or type(action["state"]["on"]) is not bool
        ):
            raise ValueError(
                "Durable plans support only Boolean relay commands and explicit durable_wait"
            )
        previous_wait = waiting
    if total > MAX_WAIT_SECONDS:
        raise ValueError("Total wait budget exceeds 3600 seconds")
    return total
