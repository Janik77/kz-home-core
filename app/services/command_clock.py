"""Dispatch clock checks, not a proof of synchronized actuator time."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from time import monotonic
from math import isfinite

from app.core.errors import UnavailableError

COMMAND_LIFETIME = timedelta(seconds=30)


class CommandClock:
    def __init__(
        self,
        wall: Callable[[], datetime] = lambda: datetime.now(UTC),
        ticks: Callable[[], float] = monotonic,
    ):
        self.wall, self.ticks = wall, ticks
        self.anchor: tuple[datetime, float] | None = None
        self.fault = False
        self.last_tick: float | None = None

    def now(self) -> datetime:
        wall, tick = self.wall(), self.ticks()
        if (
            wall.tzinfo is None
            or wall.utcoffset() != timedelta(0)
            or not isfinite(tick)
            or (self.last_tick is not None and tick < self.last_tick)
        ):
            self.fault = True
        if self.fault:
            raise UnavailableError("Command clock is unavailable")
        self.last_tick = tick
        if self.anchor is None:
            self.anchor = wall, tick
        base, start = self.anchor
        elapsed = tick - start
        value = base + timedelta(seconds=elapsed)
        if elapsed < 0 or abs((wall - value).total_seconds()) > 1:
            self.fault = True
        if self.fault:
            raise UnavailableError("Command clock is unavailable")
        return value


# One provider for service creation, evidence deadlines and gateway dispatch.
# Restart establishes a new anchor; physical dispatch remains prohibited.
command_clock = CommandClock()
