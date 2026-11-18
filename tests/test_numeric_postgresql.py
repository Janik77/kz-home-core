# ruff: noqa: F811
"""Opt-in PostgreSQL locking and durable threshold checkpoint checks."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier
from types import SimpleNamespace
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.models import AutomationORM, DeviceORM, HouseORM
from app.services.automation_runs import AutomationRunService
from test_definition_postgresql import pg_definitions  # noqa: F401


def test_concurrent_duplicate_threshold_has_one_durable_winner(pg_definitions):
    now = datetime.now(UTC)
    old = (now - timedelta(seconds=1)).isoformat()
    stamp = now.isoformat()
    with Session(pg_definitions) as db:
        device = db.get(DeviceORM, "fixture")
        device.capabilities = ["illuminance"]
        device.state = {"illuminance": 90}
        device.numeric_observed_at = {"illuminance": stamp}
        rule = db.get(AutomationORM, "rule")
        rule.trigger = {
            "type": "numeric_threshold",
            "device_id": "fixture",
            "field": "illuminance",
            "operator": "lt",
            "value": 100,
            "hysteresis": 20,
        }
        rule.threshold_state = {"revision": 1, "armed": True, "observed_at": old}
        db.commit()
    event = SimpleNamespace(
        data={
            "state": {"illuminance": 90},
            "numeric_observed_at": {"illuminance": stamp},
            "numeric_previous_observed_at": {"illuminance": old},
        }
    )
    barrier = Barrier(2)

    def consume(_):
        with Session(pg_definitions) as db:
            barrier.wait(timeout=5)
            db.scalar(select(HouseORM).where(HouseORM.id == "a").with_for_update())
            service = AutomationRunService(db)
            service.lock_definition("rule")
            return service.consume_threshold(db.get(AutomationORM, "rule"), event)

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(consume, range(2)))
    assert sorted(results) == [False, True]
    pg_definitions.dispose()
    with Session(pg_definitions) as db:
        assert db.get(AutomationORM, "rule").threshold_state == {
            "revision": 1,
            "armed": False,
            "observed_at": stamp,
        }
