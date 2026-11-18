# ruff: noqa: F811
"""Dedicated CI database only, no local containers or production profile."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.models import HouseORM, AutomationORM
from app.services.automation_runs import AutomationRunService
from app.services.schedule_planner import consume_occurrence
from app.repositories.house_repository import HouseRepository
from app.core.errors import DefinitionRevisionConflict
from test_definition_postgresql import pg_definitions  # noqa: F401


def test_concurrent_schedule_consumption_one_winner(pg_definitions):
    now = datetime.now(UTC).replace(second=0, microsecond=0)
    with Session(pg_definitions) as db:
        house = db.get(HouseORM, "a")
        house.timezone = "UTC"
        rule = db.get(AutomationORM, "rule")
        rule.trigger = dict(
            type="schedule", time=now.strftime("%H:%M"), weekdays=list(range(7))
        )
        rule.schedule_effective_after = now - timedelta(minutes=1)
        db.commit()
    barrier = Barrier(2)

    def claim(_):
        with Session(pg_definitions) as db:
            barrier.wait(timeout=5)
            db.scalar(select(HouseORM).where(HouseORM.id == "a").with_for_update())
            service = AutomationRunService(db, now=lambda: now + timedelta(seconds=1))
            service.lock_definition("rule")
            return consume_occurrence(service, db.get(AutomationORM, "rule"), now)

    with ThreadPoolExecutor(2) as pool:
        result = list(pool.map(claim, range(2)))
    assert result.count(1) == 1 and result.count(None) == 1
    with Session(pg_definitions) as db:
        assert (
            db.get(AutomationORM, "rule").schedule_state["occurrence"]
            == now.isoformat()
        )


def test_concurrent_timezone_edit_one_cas_winner(pg_definitions):
    barrier = Barrier(2)

    def edit(zone):
        with Session(pg_definitions) as db:
            barrier.wait(timeout=5)
            try:
                HouseRepository(db).update(
                    "a", dict(timezone=zone, expected_timezone_revision=1)
                )
                return "saved"
            except DefinitionRevisionConflict:
                return "conflict"

    with ThreadPoolExecutor(2) as pool:
        assert sorted(pool.map(edit, ["Asia/Almaty", "UTC"])) == ["conflict", "saved"]
    with Session(pg_definitions) as db:
        assert db.get(HouseORM, "a").timezone_revision == 2
