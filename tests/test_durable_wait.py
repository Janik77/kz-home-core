import asyncio
from datetime import UTC, datetime, timedelta
import pytest
from sqlalchemy.orm import Session
from app.models import (
    SceneRunORM,
    SceneRunWaitORM,
    SceneRunActionORM,
    SceneORM,
)
from app.schemas import SceneCreate
from app.services.durable_wait import resume_waits
from app.services.scene_executor import SceneExecutor
from test_command_lifecycle import fixture
from test_scene_runs import request, read, BASE
from test_server_scene_execution import start, wait_action, wait_status


def actions(seconds=1):
    return [
        {"type": "device_state", "device_id": "software_relay", "state": {"on": True}},
        {"type": "durable_wait", "seconds": seconds},
        {"type": "device_state", "device_id": "software_relay", "state": {"on": False}},
    ]


@pytest.mark.parametrize(
    "value", [True, False, 0, -1, 3601, 1.5, "1", None, float("nan"), float("inf")]
)
def test_wait_strict_seconds(value):
    with pytest.raises(ValueError):
        SceneCreate(id="s", house_id="h", name="N", actions=actions(value))


@pytest.mark.parametrize(
    "plan",
    [
        actions()[1:],
        actions()[:-1],
        actions()[:2] + actions()[1:],
        actions(3600) + actions(1)[1:],
        actions() * 6,
        [
            {"type": "device_state", "device_id": "d", "state": {"brightness": 1}},
            *actions()[1:],
        ],
        [
            actions()[0],
            {"type": "durable_wait", "seconds": 1, "extra": True},
            actions()[2],
        ],
    ],
)
def test_wait_plan_bounds(plan):
    with pytest.raises(ValueError):
        SceneCreate(id="s", house_id="h", name="N", actions=plan)


async def confirmed_first(h, run):
    first = await wait_action(h, run, 0)
    await h.ack(first)
    assert (await read(h, run))["actions"][1]["due_at"] is None
    await h.state(first, True)
    return await wait_status(h, run, "waiting")


def test_scene_confirm_wait_resume_once_and_immutable_deadlines(tmp_path, monkeypatch):
    async def check():
        async with fixture(tmp_path, monkeypatch) as h:
            response, key, body = await start(h, actions(1))
            assert response.status_code == 201, response.text
            run = response.json()
            assert run["plan_deadline"] == run["dispatch_deadline"]
            assert "type" not in run["actions"][0]
            assert run["actions"][1]["device_id"] is None
            waiting = await confirmed_first(h, run)
            before = len(h.wire.published)
            await asyncio.sleep(0.3)
            assert len(h.wire.published) == before
            assert (await read(h, run))["status"] == "waiting"
            with Session(h.engine) as db:
                row = db.get(SceneRunORM, run["id"])
                assert row.executor_token is None and row.lease_until is None
                assert row.wait_position == 1
            second = await wait_action(h, run, 2)
            assert datetime.fromisoformat(
                second["created_at"]
            ) >= datetime.fromisoformat(waiting["wait_due_at"])
            assert datetime.fromisoformat(
                second["expires_at"]
            ) - datetime.fromisoformat(second["created_at"]) <= timedelta(seconds=30)
            await h.ack(second)
            await h.state(second, False)
            done = await wait_status(h, run, "succeeded")
            assert [a["status"] for a in done["actions"]] == [
                "applied",
                "completed",
                "applied",
            ]
            assert done["dispatch_deadline"] == run["dispatch_deadline"]
            replay = await request(
                h,
                "POST",
                BASE + "/execute",
                json=body,
                headers={"Idempotency-Key": key},
            )
            assert replay.json()["id"] == run["id"]
            assert (
                await request(h, "POST", BASE + f"/{run['id']}/cancel")
            ).status_code == 409

    asyncio.run(check())


def test_wait_cancel_idempotent_and_no_undo(tmp_path, monkeypatch):
    async def check():
        async with fixture(tmp_path, monkeypatch) as h:
            response, _, _ = await start(h, actions(5))
            run = response.json()
            assert (
                await request(h, "POST", BASE + f"/{run['id']}/cancel")
            ).status_code == 409
            await confirmed_first(h, run)
            count = len(h.wire.published)
            response = await request(h, "POST", BASE + f"/{run['id']}/cancel")
            assert response.status_code == 200, response.text
            result = response.json()
            assert (
                result["status"] == "cancelled"
                and result["actions"][0]["status"] == "applied"
            )
            assert result["actions"][1]["status"] == "cancelled"
            again = await request(h, "POST", BASE + f"/{run['id']}/cancel")
            assert again.json() == result
            await asyncio.sleep(1.1)
            assert len(h.wire.published) == count
            assert (await request(h, "GET", "/devices/software_relay")).json()["state"][
                "on"
            ] is True

    asyncio.run(check())


@pytest.mark.parametrize("change", ["revision", "moved", "rights", "overdue", "clock"])
def test_wait_recovery_refuses_invalid_or_expired_continuation(
    tmp_path, monkeypatch, change
):
    async def check():
        async with fixture(tmp_path, monkeypatch) as h:
            response, _, body = await start(h, actions(10))
            run = response.json()
            await confirmed_first(h, run)
            count = len(h.wire.published)
            with Session(h.engine) as db:
                row = db.get(SceneRunORM, run["id"])
                wait = db.get(SceneRunWaitORM, (row.id, 1))
                due = wait.due_at.replace(tzinfo=UTC)
                if change == "revision":
                    db.get(SceneORM, body["scene_id"]).revision += 1
                elif change == "moved":
                    # The existing inventory authorization contract is checked by changing snapshot house.
                    row.house_id = "other_house"
                    db.rollback()
                    monkeypatch.setattr(
                        "app.services.scene_run_service.DeviceRepository.house_id",
                        lambda *args: "moved",
                    )
                elif change == "rights":
                    from app.models import UserORM

                    db.get(UserORM, row.requested_by).is_active = False
                db.commit()
                current = (
                    due + timedelta(seconds=31)
                    if change == "overdue"
                    else due - timedelta(seconds=20)
                    if change == "clock"
                    else due
                )
            executor = SceneExecutor(lambda: Session(h.engine), h.gateway)
            await resume_waits(
                lambda: Session(h.engine), h.gateway, executor, now=lambda: current
            )
            assert not executor.tasks
            with Session(h.engine) as db:
                row = db.get(SceneRunORM, run["id"])
                assert row.execution_finished
                assert row.execution_error in {
                    "continuation_invalid",
                    "wait_expired",
                    "wait_clock_reversed",
                }
                assert not db.get(SceneRunActionORM, (row.id, 2)).claimed
            assert len(h.wire.published) == count

    asyncio.run(check())


def test_manual_scene_wait_refused_without_effect(tmp_path, monkeypatch):
    async def check():
        async with fixture(tmp_path, monkeypatch) as h:
            scene = await request(
                h,
                "POST",
                "/scenes",
                json=dict(
                    id="manual-wait",
                    name="Wait",
                    house_id="fixture_house",
                    actions=actions(),
                ),
            )
            assert scene.status_code == 201
            result = await request(
                h,
                "POST",
                BASE,
                json={"scene_id": "manual-wait", "expected_revision": 1},
                headers={"Idempotency-Key": "manual-wait-key-123"},
            )
            assert result.status_code == 422

    asyncio.run(check())


@pytest.mark.parametrize("outcome", ["success", "disable", "cancel"])
def test_automation_wait_lifecycle(tmp_path, monkeypatch, outcome):
    from uuid import uuid4
    from test_durable_automations import rule, wait, runs

    async def check():
        async with fixture(tmp_path, monkeypatch) as h:
            plan = actions(1 if outcome == "success" else 5)
            plan[0]["state"]["on"] = False
            plan[2]["state"]["on"] = True
            a = await rule(h, actions=plan)
            await h.state({"correlation_id": str(uuid4())}, True)
            run = await wait(
                h, a, lambda r: r["actions"] and r["actions"][0]["receipt"]
            )
            receipt = run["actions"][0]["receipt"]
            await h.ack(receipt)
            await h.state(receipt, False)
            waiting = await wait(h, a, lambda r: r["status"] == "waiting")
            count = len(h.wire.published)
            if outcome == "success":
                ready = await wait(h, a, lambda r: r["actions"][2]["receipt"])
                second = ready["actions"][2]["receipt"]
                await h.ack(second)
                await h.state(second, True)
                done = await wait(h, a, lambda r: r["status"] == "succeeded")
                assert done["dispatch_deadline"] == waiting["dispatch_deadline"]
                assert done["actions"][1]["status"] == "completed"
            elif outcome == "disable":
                changed = await request(
                    h,
                    "PATCH",
                    "/automations/" + a["id"],
                    json={"expected_revision": 1, "enabled": False},
                )
                assert changed.status_code == 200, changed.text
                done = await wait(h, a, lambda r: r["status"] == "cancelled")
                assert done["execution_error"] == "continuation_invalid"
                assert len(h.wire.published) == count
            else:
                path = "/automations/" + a["id"] + "/runs/" + run["id"] + "/cancel"
                cancelled = await request(h, "POST", path)
                assert cancelled.status_code == 200, cancelled.text
                assert cancelled.json()["status"] == "cancelled"
                assert (await request(h, "POST", path)).json() == cancelled.json()
                assert (await runs(h, a))[0]["actions"][2]["receipt"] is None
                assert len(h.wire.published) == count

    asyncio.run(check())


def test_invalid_wait_patch_is_422_and_preserves_revision(tmp_path, monkeypatch):
    async def check():
        async with fixture(tmp_path, monkeypatch) as h:
            response, _, body = await start(h, actions(10))
            run = response.json()
            await confirmed_first(h, run)
            invalid = await request(
                h,
                "PATCH",
                "/scenes/" + body["scene_id"],
                json={"expected_revision": 1, "actions": actions()[1:]},
            )
            assert invalid.status_code == 422, invalid.text
            assert (await request(h, "GET", "/scenes/" + body["scene_id"])).json()[
                "revision"
            ] == 1

    asyncio.run(check())


def test_wait_expiry_never_hides_unknown_sent_command(tmp_path, monkeypatch):
    from app.services.scene_run_service import SceneRunService

    async def check():
        async with fixture(tmp_path, monkeypatch) as h:
            response, _, _ = await start(h, actions())
            run = response.json()
            await wait_action(h, run, 0)
            with Session(h.engine) as db:
                row = db.get(SceneRunORM, run["id"])
                future = datetime.fromisoformat(run["dispatch_deadline"]) + timedelta(
                    seconds=1
                )
                row.execution_finished = True
                row.execution_error = "executor_interrupted"
                db.commit()
                result = SceneRunService(db, now=lambda: future).get(
                    "fixture_house", run["id"]
                )
                assert result.status == "unknown"
                assert result.actions[2].receipt is None

    asyncio.run(check())


def test_wait_history_blocks_downgrade_even_after_definition_changed(
    tmp_path, monkeypatch
):
    from alembic import command
    from test_schema import migration_config

    async def check():
        async with fixture(tmp_path, monkeypatch) as h:
            response, _, body = await start(h, actions(10))
            run = response.json()
            await confirmed_first(h, run)
            assert (
                await request(h, "POST", BASE + "/" + run["id"] + "/cancel")
            ).status_code == 200
            with Session(h.engine) as db:
                db.get(SceneORM, body["scene_id"]).actions = [actions()[0]]
                db.commit()
            with h.engine.begin() as conn:
                with pytest.raises(RuntimeError, match="Durable wait history exists"):
                    command.downgrade(migration_config(conn), "0015_house_schedules")

    asyncio.run(check())


def test_explicit_wait_plan_rejects_unknown_relay_constraints():
    plan = actions()
    plan[0]["future_constraint"] = True
    with pytest.raises(ValueError):
        SceneCreate(id="strict", house_id="h", name="Strict", actions=plan)
