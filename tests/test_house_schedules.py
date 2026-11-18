import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
import pytest
from sqlalchemy.orm import Session
from app.models import HouseORM, AutomationORM, DeviceORM, SceneRunORM
from app.schemas import ScheduleTrigger, HouseUpdate
from app.services.schedule_planner import (
    SchedulePlanner,
    scheduled_at,
    consume_occurrence,
)
from app.services.automation_runs import AutomationRunService
from app.core.errors import ConflictError
from test_command_lifecycle import fixture
from test_scene_runs import request
from test_durable_automations import runs


@pytest.mark.parametrize(
    "change",
    [
        {"time": "1:00"},
        {"time": "24:00"},
        {"time": "10:60"},
        {"time": "10:00:00"},
        {"weekdays": []},
        {"weekdays": [True]},
        {"weekdays": [0, 0]},
        {"weekdays": [-1]},
        {"weekdays": [7]},
        {"weekdays": ["1"]},
        {"timezone": "UTC"},
        {"catch_up": True},
    ],
)
def test_strict_schedule(change):
    with pytest.raises(ValueError):
        ScheduleTrigger.model_validate(
            {"type": "schedule", "time": "10:00", "weekdays": [0], **change}
        )


@pytest.mark.parametrize("zone", ["Mars/Base", "../UTC", "", "UTC+05:00"])
def test_invalid_zone(zone):
    with pytest.raises(ValueError):
        HouseUpdate(timezone=zone, expected_timezone_revision=1)


def test_dst_fold_gap_and_explicit_house_zone():
    trigger = dict(type="schedule", time="01:30", weekdays=[6])
    assert scheduled_at(
        trigger, "America/New_York", datetime(2026, 11, 1, 5, 30, tzinfo=UTC)
    )
    assert not scheduled_at(
        trigger, "America/New_York", datetime(2026, 11, 1, 6, 30, tzinfo=UTC)
    )
    gap = dict(type="schedule", time="02:30", weekdays=[6])
    for minute in range(180):
        assert not scheduled_at(
            gap,
            "America/New_York",
            datetime(2026, 3, 8, 6, tzinfo=UTC) + timedelta(minutes=minute),
        )
    assert scheduled_at(
        dict(type="schedule", time="10:00", weekdays=[0]),
        "Asia/Almaty",
        datetime(2026, 11, 16, 5, tzinfo=UTC),
    )
    assert not scheduled_at(trigger, None, datetime.now(UTC))


async def configure(h, *, enabled=True, condition=None):
    house = (await request(h, "GET", "/houses/fixture_house")).json()
    assert house["timezone"] is None
    assert (
        await request(h, "PATCH", "/houses/fixture_house", json={"timezone": "UTC"})
    ).status_code == 422
    response = await request(
        h,
        "PATCH",
        "/houses/fixture_house",
        json={"timezone": "UTC", "expected_timezone_revision": 1},
    )
    assert response.status_code == 200, response.text
    assert response.json()["timezone_revision"] == 2
    now = datetime.now(UTC).replace(second=0, microsecond=0) + timedelta(minutes=1)
    body = dict(
        id="scheduled",
        name="Schedule",
        house_id="fixture_house",
        enabled=enabled,
        trigger=dict(
            type="schedule", time=now.strftime("%H:%M"), weekdays=list(range(7))
        ),
        conditions=condition or [],
        actions=[
            dict(type="device_state", device_id="software_relay", state={"on": True})
        ],
    )
    response = await request(h, "POST", "/automations", json=body)
    assert response.status_code == 201, response.text
    with Session(h.engine) as db:
        db.get(DeviceORM, "software_relay").state_observed_at = now
        db.commit()
    return now, response.json()


def test_api_timezone_cas_null_and_disabled_draft(tmp_path, monkeypatch):
    async def check():
        async with fixture(tmp_path, monkeypatch) as h:
            _, rule = await configure(h)
            response = await request(
                h,
                "PATCH",
                "/houses/fixture_house",
                json={"timezone": "Asia/Almaty", "expected_timezone_revision": 1},
            )
            assert response.status_code == 409
            assert response.json()["code"] == "definition_revision_conflict"
            assert (
                await request(
                    h, "PATCH", "/houses/fixture_house", json={"name": "Renamed"}
                )
            ).status_code == 200
            assert (
                await request(
                    h,
                    "PATCH",
                    "/houses/fixture_house",
                    json={"timezone": None, "expected_timezone_revision": 2},
                )
            ).json()["timezone"] is None
            assert (
                await request(
                    h,
                    "PATCH",
                    "/automations/scheduled",
                    json={"expected_revision": 1, "enabled": False},
                )
            ).status_code == 200
            assert (
                await request(h, "POST", "/automations/scheduled/enable")
            ).status_code == 422
            response = await request(h, "GET", "/automations/scheduled")
            assert response.json()["trigger"] == rule["trigger"]
            assert not response.json()["enabled"]
            dry = await request(h, "POST", "/automations/scheduled/dry-run")
            assert dry.status_code == 200
            assert {"rule_disabled", "house_timezone_required"} <= set(
                dry.json()["issues"]
            )

    asyncio.run(check())


def test_planner_boundary_startup_gap_restart_dedup(tmp_path, monkeypatch):
    async def check():
        async with fixture(tmp_path, monkeypatch) as h:
            boundary, _ = await configure(h)
            clock = [boundary - timedelta(seconds=1)]
            calls = []

            async def admitted(self, rule_id, event, executor, *, occurrence):
                assert event is None
                calls.append((rule_id, occurrence))

            monkeypatch.setattr(AutomationRunService, "admit", admitted)

            def factory():
                return Session(h.engine)

            planner = SchedulePlanner(factory, None, None, now=lambda: clock[0])
            await planner.tick()
            assert calls == []
            clock[0] = boundary + timedelta(milliseconds=100)
            await planner.tick()
            assert calls == [("scheduled", boundary)]
            await planner.tick()
            assert len(calls) == 1
            restart = SchedulePlanner(factory, None, None, now=lambda: clock[0])
            await restart.tick()
            assert len(calls) == 1
            clock[0] = boundary + timedelta(minutes=1, milliseconds=100)
            await planner.tick()  # stalled event loop cannot catch up
            assert len(calls) == 1
            clock[0] = boundary - timedelta(seconds=1)
            await planner.tick()  # rollback cannot reduce cursor
            with factory() as db:
                assert (
                    db.get(HouseORM, "fixture_house").schedule_cursor.replace(
                        tzinfo=UTC
                    )
                    > boundary
                )

    asyncio.run(check())


def test_consumed_run_and_timezone_change_before_dispatch(tmp_path, monkeypatch):
    async def check():
        async with fixture(tmp_path, monkeypatch) as h:
            boundary, a = await configure(h)
            queued = []
            executor = SimpleNamespace(start=lambda *args: queued.append(args))

            def factory():
                return Session(h.engine)

            with factory() as db:
                service = AutomationRunService(
                    db, h.gateway, now=lambda: boundary + timedelta(seconds=1)
                )
                await service.admit(a["id"], None, executor, occurrence=boundary)
            assert len(queued) == 1
            with factory() as db:
                service = AutomationRunService(
                    db, h.gateway, now=lambda: boundary + timedelta(seconds=2)
                )
                await service.admit(a["id"], None, executor, occurrence=boundary)
                row = db.get(SceneRunORM, queued[0][1])
                assert row.schedule_timezone_revision == 2
                assert row.dispatch_deadline.replace(
                    tzinfo=UTC
                ) == boundary + timedelta(seconds=30)
                service.before_dispatch(row)
            response = await request(
                h,
                "PATCH",
                "/houses/fixture_house",
                json={"timezone": "Asia/Almaty", "expected_timezone_revision": 2},
            )
            assert response.status_code == 200
            with factory() as db:
                with pytest.raises(ConflictError, match="timezone"):
                    AutomationRunService(
                        db, h.gateway, now=lambda: boundary
                    ).before_dispatch(db.get(SceneRunORM, queued[0][1]))
            assert len(queued) == 1

    asyncio.run(check())


def test_condition_refusal_consumes_occurrence(tmp_path, monkeypatch):
    async def check():
        async with fixture(tmp_path, monkeypatch) as h:
            boundary, a = await configure(
                h,
                condition=[
                    dict(
                        type="device_state",
                        device_id="software_relay",
                        field="on",
                        operator="eq",
                        value=True,
                    )
                ],
            )
            queued = []
            executor = SimpleNamespace(start=lambda *args: queued.append(args))
            with Session(h.engine) as db:
                db.get(DeviceORM, "software_relay").state = {"on": False}
                db.commit()
                service = AutomationRunService(
                    db, h.gateway, now=lambda: boundary + timedelta(seconds=1)
                )
                await service.admit(a["id"], None, executor, occurrence=boundary)
                db.get(DeviceORM, "software_relay").state = {"on": True}
                db.commit()
                await service.admit(a["id"], None, executor, occurrence=boundary)
            rows = await runs(h, a)
            assert len(rows) == 1 and rows[0]["execution_error"] == "conditions_not_met"
            assert queued == []

    asyncio.run(check())


def test_effective_after_and_late_claim_never_runs(tmp_path, monkeypatch):
    async def check():
        async with fixture(tmp_path, monkeypatch) as h:
            boundary, _ = await configure(h)
            with Session(h.engine) as db:
                rule = db.get(AutomationORM, "scheduled")
                rule.schedule_effective_after = boundary + timedelta(seconds=1)
                db.commit()
                service = AutomationRunService(
                    db, now=lambda: boundary + timedelta(seconds=2)
                )
                assert consume_occurrence(service, rule, boundary) is None
                rule.schedule_effective_after = boundary - timedelta(minutes=1)
                db.commit()
                service.now = lambda: boundary + timedelta(seconds=6)
                assert consume_occurrence(service, rule, boundary) is None
                assert rule.schedule_state == {}

    asyncio.run(check())
