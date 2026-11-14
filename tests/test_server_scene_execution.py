"""One HTTP intent drives durable sequential execution; fake MQTT only."""

import asyncio
from datetime import timedelta
from uuid import uuid4
import pytest
from sqlalchemy.orm import Session
from app.models import SceneRunORM, SceneRunActionORM, UserORM
from app.services.command_clock import command_clock
from app.services.scene_executor import SceneExecutor
from test_command_lifecycle import fixture
from test_scene_runs import request, read, dispatch, BASE


async def start(h, actions=None, key=None):
    scene_id = "server_" + uuid4().hex
    r = await request(
        h,
        "POST",
        "/scenes",
        json={
            "id": scene_id,
            "house_id": "fixture_house",
            "name": "One click",
            "actions": actions
            or [
                {"device_id": "software_relay", "state": {"on": True}},
                {"device_id": "software_relay", "state": {"on": False}},
            ],
        },
    )
    assert r.status_code == 201, r.text
    key = key or str(uuid4())
    body = {"scene_id": scene_id, "expected_revision": 1}
    r = await request(
        h, "POST", BASE + "/execute", json=body, headers={"Idempotency-Key": key}
    )
    return r, key, body


async def wait_action(h, run, position):
    for _ in range(100):
        view = await read(h, run)
        if view["actions"][position]["receipt"] is not None:
            return view["actions"][position]["receipt"]
        await asyncio.sleep(0.025)
    raise AssertionError(view)


async def wait_status(h, run, status):
    for _ in range(100):
        view = await read(h, run)
        if view["status"] == status:
            return view
        await asyncio.sleep(0.025)
    raise AssertionError(view)


def test_one_click_advances_only_after_ack_and_state_and_replay_is_readonly(
    tmp_path, monkeypatch
):
    async def check():
        async with fixture(tmp_path, monkeypatch) as h:
            r, key, body = await start(h)
            assert r.status_code == 201, r.text
            run = r.json()
            assert run["execution_mode"] == "server"
            first = await wait_action(h, run, 0)
            assert (await dispatch(h, run, 0)).status_code == 409
            await h.ack(first)
            await asyncio.sleep(0.25)
            assert (await read(h, run))["actions"][1]["receipt"] is None
            await h.state(first, True)
            second = await wait_action(h, run, 1)
            await h.state(second, False)
            await h.ack(second)
            await wait_status(h, run, "succeeded")
            count = len(h.wire.published)
            again = await request(
                h,
                "POST",
                BASE + "/execute",
                json=body,
                headers={"Idempotency-Key": key},
            )
            assert again.json()["id"] == run["id"]
            lookup = await request(h, "GET", BASE + "/by-key/" + key)
            assert lookup.json()["id"] == run["id"]
            assert len(h.wire.published) == count

    asyncio.run(check())


@pytest.mark.parametrize("outcome", ["failed", "timeout", "revoked"])
def test_stop_after_first_failure_unknown_or_revocation(tmp_path, monkeypatch, outcome):
    async def check():
        async with fixture(tmp_path, monkeypatch) as h:
            r, _, _ = await start(h)
            run = r.json()
            first = await wait_action(h, run, 0)
            if outcome == "failed":
                await h.ack(first, "rejected", error_code="test_rejected")
                expected = "refused"
            elif outcome == "timeout":
                h.expire(first)
                expected = "unknown"
            else:
                with Session(h.engine) as s:
                    row = s.get(SceneRunORM, run["id"])
                    user = s.get(UserORM, row.requested_by)
                    user_id = user.id
                    user.is_active = False
                    s.commit()
                # Read under same bearer would also be unauthorized; re-enable after
                # worker has observed revoked authority at next dispatch.
                await h.ack(first)
                await h.state(first, True)
                await asyncio.sleep(0.4)
                with Session(h.engine) as s:
                    user = s.get(UserORM, user_id)
                    user.is_active = True
                    s.commit()
                expected = "partial"
            view = await wait_status(h, run, expected)
            assert view["actions"][1]["receipt"] is None

    asyncio.run(check())


def test_all_action_preflight_rejects_before_publication(tmp_path, monkeypatch):
    async def check():
        async with fixture(tmp_path, monkeypatch) as h:
            count = len(h.wire.published)
            r, _, _ = await start(
                h,
                [
                    {"device_id": "software_relay", "state": {"on": True}},
                    {"device_id": "software_relay", "state": {"on": "bad"}},
                ],
            )
            assert r.status_code in (409, 422), r.text
            assert len(h.wire.published) == count

    asyncio.run(check())


def test_expired_owner_and_claim_are_never_resumed(tmp_path, monkeypatch):
    async def check():
        async with fixture(tmp_path, monkeypatch) as h:
            # Prevent scheduling to model crash after durable create before start.
            monkeypatch.setattr(SceneExecutor, "start", lambda *args: None)
            r, key, body = await start(h)
            run = r.json()
            with Session(h.engine) as s:
                row = s.get(SceneRunORM, run["id"])
                row.lease_until = command_clock.now() - timedelta(seconds=1)
                a = s.get(SceneRunActionORM, (run["id"], 0))
                a.claimed = True
                s.commit()
            count = len(h.wire.published)
            view = await read(h, run)
            assert view["status"] == "unknown"
            assert view["actions"][0]["status"] == "dispatch_unknown"
            assert view["actions"][1]["status"] == "skipped"
            replay = await request(
                h,
                "POST",
                BASE + "/execute",
                json=body,
                headers={"Idempotency-Key": key},
            )
            assert replay.json()["status"] == "unknown"
            assert len(h.wire.published) == count

    asyncio.run(check())


def test_duplicate_different_keys_and_conflicting_same_key(tmp_path, monkeypatch):
    async def check():
        async with fixture(tmp_path, monkeypatch) as h:
            monkeypatch.setattr(SceneExecutor, "start", lambda *args: None)
            r, key, body = await start(h)
            assert r.status_code == 201
            other = await request(
                h,
                "POST",
                BASE + "/execute",
                json=body,
                headers={"Idempotency-Key": str(uuid4())},
            )
            assert other.status_code == 409
            same = await request(
                h,
                "POST",
                BASE + "/execute",
                json=body,
                headers={"Idempotency-Key": key},
            )
            assert same.json()["id"] == r.json()["id"]
            wrong = await request(
                h,
                "POST",
                BASE + "/execute",
                json={**body, "expected_revision": 2},
                headers={"Idempotency-Key": key},
            )
            assert wrong.status_code == 409

    asyncio.run(check())


def test_revision_changed_during_async_preflight_is_rejected(tmp_path, monkeypatch):
    from app.models import SceneORM
    from sqlalchemy import select

    async def check():
        async with fixture(tmp_path, monkeypatch) as h:
            original = h.gateway.authorize_command
            changed = False

            async def mutate(*args):
                nonlocal changed
                await original(*args)
                if not changed:
                    changed = True
                    with Session(h.engine) as s:
                        scene = s.scalar(
                            select(SceneORM).where(SceneORM.name == "One click")
                        )
                        scene.revision += 1
                        s.commit()

            monkeypatch.setattr(h.gateway, "authorize_command", mutate)
            count = len(h.wire.published)
            response, _, _ = await start(h)
            assert response.status_code == 409, response.text
            assert len(h.wire.published) == count

    asyncio.run(check())


def test_claim_in_flight_is_running_then_crash_is_unknown(tmp_path, monkeypatch):
    async def check():
        async with fixture(tmp_path, monkeypatch) as h:
            monkeypatch.setattr(SceneExecutor, "start", lambda *args: None)
            r, _, _ = await start(h)
            run = r.json()
            with Session(h.engine) as s:
                s.get(SceneRunActionORM, (run["id"], 0)).claimed = True
                s.commit()
            live = await read(h, run)
            assert live["status"] == "running"
            assert live["actions"][0]["status"] == "waiting"
            with Session(h.engine) as s:
                row = s.get(SceneRunORM, run["id"])
                row.execution_finished = True
                row.execution_error = "executor_interrupted"
                s.commit()
            dead = await read(h, run)
            assert dead["status"] == "unknown"
            assert dead["actions"][0]["status"] == "dispatch_unknown"

    asyncio.run(check())


def test_partial_explicit_failure_preserves_prior_applied_result(tmp_path, monkeypatch):
    async def check():
        async with fixture(tmp_path, monkeypatch) as h:
            r, _, _ = await start(h)
            run = r.json()
            first = await wait_action(h, run, 0)
            await h.ack(first)
            await h.state(first, True)
            second = await wait_action(h, run, 1)
            await h.ack(second, "rejected", error_code="synthetic_rejection")
            view = await wait_status(h, run, "partial")
            assert [a["status"] for a in view["actions"]] == ["applied", "failed"]

    asyncio.run(check())
