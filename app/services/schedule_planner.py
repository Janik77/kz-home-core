"""One lifecycle tick source feeding existing AutomationRunService, never replaying.

A healthy tick straddles a minute boundary with at most five seconds of gap.
Startup/recovery advances the durable high-water cursor but cannot claim work.
House row locking serializes workers; committing the cursor before admission
makes a crash lose an occurrence rather than create an overdue command.
"""

import asyncio
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from sqlalchemy import select
from app.models import HouseORM, AutomationORM
from app.schemas import ScheduleTrigger
from app.services.command_clock import command_clock
from app.services.command_service import utc

log = logging.getLogger(__name__)
MAX_TICK_GAP = timedelta(seconds=5)


def scheduled_at(trigger, zone, instant):
    """Only fold=0 is eligible; fold=1 cannot rescue a missed first occurrence."""
    try:
        value = ScheduleTrigger.model_validate(trigger)
        local = instant.astimezone(ZoneInfo(zone))
    except (ValueError, ZoneInfoNotFoundError, TypeError):
        return False
    return (
        local.fold == 0
        and local.weekday() in value.weekdays
        and local.strftime("%H:%M") == value.time
    )


def consume_occurrence(service, rule, occurrence):
    session = service.session
    house = session.get(HouseORM, rule.house_id, populate_existing=True)
    now = service.now()
    if (
        house is None
        or not house.timezone
        or rule.trigger.get("type") != "schedule"
        or not scheduled_at(rule.trigger, house.timezone, occurrence)
        or occurrence > now
        or now - occurrence > MAX_TICK_GAP
        or rule.schedule_effective_after is None
        or utc(rule.schedule_effective_after) >= occurrence
        or (
            house.timezone_effective_after
            and utc(house.timezone_effective_after) >= occurrence
        )
    ):
        session.rollback()
        return None
    previous = (rule.schedule_state or {}).get("occurrence")
    # High water survives edits and zone changes, so clock reversal never repeats.
    if previous and utc(datetime.fromisoformat(previous)) >= occurrence:
        session.rollback()
        return None
    rule.schedule_state = {
        "occurrence": occurrence.isoformat(),
        "revision": rule.revision,
        "timezone_revision": house.timezone_revision,
    }
    revision = house.timezone_revision
    session.commit()  # Consumption precedes rights/conditions/network checks.
    return revision


class SchedulePlanner:
    def __init__(self, session_factory, transport, executor, *, now=command_clock.now):
        self.session_factory, self.transport, self.executor = (
            session_factory,
            transport,
            executor,
        )
        self.now = now
        self.last_tick = None

    async def tick(self):
        now = self.now()
        boundary = now.replace(second=0, microsecond=0)
        previous_local, self.last_tick = self.last_tick, now
        locally_healthy = (
            previous_local is not None
            and timedelta(0) < now - previous_local <= MAX_TICK_GAP
        )
        with self.session_factory() as session:
            house_ids = list(
                session.scalars(
                    select(HouseORM.id).where(HouseORM.timezone.is_not(None))
                )
            )
        for house_id in house_ids:
            with self.session_factory() as session:
                house = session.scalar(
                    select(HouseORM).where(HouseORM.id == house_id).with_for_update()
                )
                if house is None:
                    continue
                old = utc(house.schedule_cursor) if house.schedule_cursor else None
                healthy = (
                    locally_healthy
                    and old is not None
                    and old < boundary <= now
                    and timedelta(0) < now - old <= MAX_TICK_GAP
                    and now - boundary <= MAX_TICK_GAP
                )
                # Never decrease the persisted cursor, including clock reversal.
                house.schedule_cursor = max(old, now) if old else now
                candidates = (
                    list(
                        session.scalars(
                            select(AutomationORM.id).where(
                                AutomationORM.house_id == house_id
                            )
                        )
                    )
                    if healthy
                    else []
                )
                session.commit()
            for rule_id in candidates:
                with self.session_factory() as session:
                    from app.services.automation_runs import AutomationRunService

                    try:
                        await AutomationRunService(
                            session, self.transport, now=self.now
                        ).admit(rule_id, None, self.executor, occurrence=boundary)
                    except Exception:
                        session.rollback()
                        log.exception("Scheduled occurrence refused")

    async def run(self):
        while True:
            try:
                await self.tick()
            except Exception:
                self.last_tick = None
                log.exception(
                    "Schedule clock/storage unavailable; skipping occurrences"
                )
            await asyncio.sleep(1)
