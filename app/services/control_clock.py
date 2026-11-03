"""Conditional timing machinery, unqualified by default; no automatic physical trust."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from time import monotonic_ns
from uuid import uuid4

from app.core.errors import UnavailableError

LIFETIME_US = 30_000_000
PERMIT_US = 2_000_000


class ControlClock:
    def __init__(
        self,
        *,
        ticks: Callable[[], int] = lambda: monotonic_ns() // 1000,
        wall: Callable[[], datetime] = lambda: datetime.now(UTC),
        qualified: bool = False,
        utc_health: Callable[[], bool] = lambda: False,
        rate_error_ppm: int = 1000,
        recovery_error_us: int = 1000,
    ):
        if not 0 <= rate_error_ppm < 1_000_000 or recovery_error_us < 0:
            raise ValueError("Invalid conditional clock bounds")
        self.epoch = str(uuid4())
        self.ticks, self.wall = ticks, wall
        self.qualified, self.utc_health = qualified, utc_health
        initial = ticks()
        self.start = self.last = initial if type(initial) is int else 0
        self.fence_start = self.start
        self.anchor = wall()
        self.timer_fault = type(initial) is not int
        self.wall_fault = False
        # Worst-case new-clock fast / old-clock slow, rounded up.
        numerator = (LIFETIME_US + recovery_error_us) * (1_000_000 + rate_error_ppm)
        denominator = 1_000_000 - rate_error_ppm
        self.fence_us = (numerator + denominator - 1) // denominator

    def tick(self) -> int:
        if not self.qualified or self.timer_fault:
            raise UnavailableError("Control timing is unqualified")
        value = self.ticks()
        if type(value) is not int or value < self.last:
            self.timer_fault = True
        if not self.qualified or self.timer_fault:
            raise UnavailableError("Control timing is unqualified")
        self.last = value
        return value

    def recovered(self) -> bool:
        return self.tick() - self.fence_start >= self.fence_us

    def logical_now(self) -> datetime:
        # Existing deadlines can finish despite a latched wall/admission fault.
        tick = self.tick()
        if self.anchor.tzinfo is None or self.anchor.utcoffset() != timedelta(0):
            raise UnavailableError("Control UTC anchor is invalid")
        return self.anchor + timedelta(microseconds=tick - self.start)

    def sample(self) -> tuple[datetime, int]:
        tick = self.tick()
        wall = self.wall()
        if self.anchor.tzinfo is None or self.anchor.utcoffset() != timedelta(0):
            self.wall_fault = True
            raise UnavailableError("Control UTC anchor is invalid")
        value = self.anchor + timedelta(microseconds=tick - self.start)
        if wall.tzinfo is None or wall.utcoffset() != timedelta(0):
            self.wall_fault = True
        elif abs((wall - value).total_seconds()) > 1:
            self.wall_fault = True
        if self.wall_fault or not self.utc_health() or not self.recovered():
            raise UnavailableError("Control authority is recovering or unhealthy")
        return value, tick
