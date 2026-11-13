"""Actual auth/API/ledger/SQLite; only MQTT network is a recording test double."""

import asyncio
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session
from app.models import SceneRunORM, SceneRunReceiptORM
from app.services.scene_run_service import SceneRunService
from app.services.command_service import CommandService
from test_command_lifecycle import fixture

BASE = "/v1/houses/fixture_house/scene-runs"


async def request(h, method, path, **kwargs):
    return await h.api.client.request(
        method,
        path,
        headers={"Authorization": "Bearer " + h.api.token, **kwargs.pop("headers", {})},
        **kwargs,
    )


async def create(h, actions=None, key=None):
    scene_id = "run_scene_" + uuid4().hex
    response = await request(
        h,
        "POST",
        "/scenes",
        json={
            "id": scene_id,
            "house_id": "fixture_house",
            "name": "Snapshot",
            "actions": actions
            or [
                {"device_id": "software_relay", "state": {"on": True}},
                {"device_id": "software_relay", "state": {"on": False}},
            ],
        },
    )
    assert response.status_code == 201, response.text
    key = key or str(uuid4())
    response = await request(
        h,
        "POST",
        BASE,
        json={"scene_id": scene_id, "expected_revision": 1},
        headers={"Idempotency-Key": key},
    )
    return response, key


async def read(h, run):
    response = await request(h, "GET", BASE + "/" + run["id"])
    assert response.status_code == 200, response.text
    return response.json()


async def dispatch(h, run, position):
    return await request(h, "POST", f"{BASE}/{run['id']}/actions/{position}/dispatch")


def test_sequential_receipts_snapshot_idempotency_and_fresh_session_recovery(
    tmp_path, monkeypatch
):
    async def check():
        async with fixture(tmp_path, monkeypatch) as h:
            response, key = await create(h)
            assert response.status_code == 201, response.text
            run = response.json()
            assert [a["status"] for a in run["actions"]] == ["ready", "waiting"]
            before = len(h.wire.published)
            same = await request(
                h,
                "POST",
                BASE,
                json={"scene_id": run["scene_id"], "expected_revision": 1},
                headers={"Idempotency-Key": key},
            )
            assert same.json()["id"] == run["id"]
            assert len(h.wire.published) == before
            assert (await dispatch(h, run, 1)).status_code == 409
            assert (
                await request(
                    h,
                    "PATCH",
                    "/scenes/" + run["scene_id"],
                    json={
                        "actions": [
                            {"device_id": "software_relay", "state": {"on": False}}
                        ]
                    },
                )
            ).status_code == 200
            response = await dispatch(h, run, 0)
            assert response.status_code == 202, response.text
            receipt = response.json()["actions"][0]["receipt"]
            assert receipt["requested_on"] is True
            assert response.json()["status"] == "running"
            assert (await dispatch(h, run, 0)).json()["actions"][0]["receipt"][
                "command_id"
            ] == receipt["command_id"]
            assert len(h.wire.published) == before + 1
            await h.ack(receipt, "accepted")
            await h.state(receipt, True)
            assert (await read(h, run))["actions"][0]["status"] == "dispatched"
            await h.ack(receipt)
            assert (await read(h, run))["actions"][1]["status"] == "ready"
            assert (
                await request(h, "DELETE", "/scenes/" + run["scene_id"])
            ).status_code == 204
            response = await dispatch(h, run, 1)
            second = response.json()["actions"][1]["receipt"]
            await h.ack(second)
            await h.state(second, False)
            result = await read(h, run)
            assert result["status"] == "succeeded"
            with Session(h.engine) as session:
                recovered = SceneRunService(session).get("fixture_house", run["id"])
                assert recovered.model_dump(mode="json") == result
                assert session.query(SceneRunReceiptORM).count() == 2

    asyncio.run(check())


@pytest.mark.parametrize("after_persist", [False, True])
def test_crash_claim_never_replays_and_recovers_atomic_receipt(
    tmp_path, monkeypatch, after_persist
):
    async def check():
        async with fixture(tmp_path, monkeypatch) as h:
            response, _ = await create(h)
            run = response.json()
            original = CommandService.submit

            async def crash(self, *args, **kwargs):
                if after_persist:
                    await original(self, *args, **kwargs)
                raise asyncio.CancelledError()

            monkeypatch.setattr(CommandService, "submit", crash)
            before = len(h.wire.published)
            with Session(h.engine) as session:
                user = session.get(SceneRunORM, run["id"]).requested_by
                with pytest.raises(asyncio.CancelledError):
                    await SceneRunService(session, h.gateway).dispatch(
                        "fixture_house", run["id"], 0, user
                    )
            monkeypatch.setattr(CommandService, "submit", original)
            recovered = await read(h, run)
            assert recovered["status"] == ("running" if after_persist else "unknown")
            replay = await dispatch(h, run, 0)
            assert replay.json() == recovered
            assert len(h.wire.published) == before + int(after_persist)
            assert (await dispatch(h, run, 1)).status_code == 409

    asyncio.run(check())


@pytest.mark.parametrize("negative", ["failed", "timeout"])
def test_failed_action_stops_later_actions_without_compensation(
    tmp_path, monkeypatch, negative
):
    async def check():
        async with fixture(tmp_path, monkeypatch) as h:
            response, _ = await create(h)
            run = response.json()
            receipt = (await dispatch(h, run, 0)).json()["actions"][0]["receipt"]
            if negative == "failed":
                await h.ack(receipt, "failed")
            else:
                h.expire(receipt)
            result = await read(h, run)
            assert result["status"] == "stopped"
            assert result["actions"][1]["status"] == "skipped"
            before = len(h.wire.published)
            assert (await dispatch(h, run, 1)).status_code == 409
            assert len(h.wire.published) == before

    asyncio.run(check())


def test_run_expiry_and_unrelated_receipt_cannot_confirm_actions(tmp_path, monkeypatch):
    async def check():
        async with fixture(tmp_path, monkeypatch) as h:
            response, _ = await create(h)
            run = response.json()
            # Even deliberately guessing the private stable key does not link an
            # unrelated direct API command to a scene action.
            forged = await h.post(True, key=SceneRunService.action_key(run["id"], 0))
            await h.ack(forged.json())
            await h.state(forged.json(), True)
            assert (await read(h, run))["actions"][0]["receipt"] is None
            assert (await dispatch(h, run, 0)).json()["actions"][0][
                "status"
            ] == "blocked"
            other, _ = await create(h)
            other = other.json()
            with Session(h.engine) as session:
                row = session.get(SceneRunORM, other["id"])
                row.dispatch_deadline = row.created_at - timedelta(seconds=1)
                session.commit()
            assert (await read(h, other))["status"] == "expired"
            assert (await dispatch(h, other, 0)).status_code == 409

    asyncio.run(check())


def test_physical_unknown_capability_and_stale_revision_fail_before_commands(
    tmp_path, monkeypatch
):
    async def check():
        async with fixture(tmp_path, monkeypatch) as h:
            before = len(h.wire.published)
            response, _ = await create(
                h, [{"device_id": "fixture_relay", "state": {"on": True}}]
            )
            assert response.status_code == 409
            response, _ = await create(
                h, [{"device_id": "software_relay", "state": {"on": "yes"}}]
            )
            assert response.status_code == 422
            response, key = await create(h)
            run = response.json()
            wrong_intent = await request(
                h,
                "POST",
                BASE,
                json={"scene_id": run["scene_id"], "expected_revision": 2},
                headers={"Idempotency-Key": key},
            )
            assert wrong_intent.status_code == 409
            stale = await request(
                h,
                "POST",
                BASE,
                json={"scene_id": run["scene_id"], "expected_revision": 2},
                headers={"Idempotency-Key": str(uuid4())},
            )
            assert stale.status_code == 409
            assert len(h.wire.published) == before

    asyncio.run(check())


def test_read_and_dispatch_require_current_house_permissions_and_creator(
    tmp_path, monkeypatch
):
    async def check():
        async with fixture(tmp_path, monkeypatch) as h:
            response, _ = await create(h)
            run = response.json()
            assert (await h.api.client.get(BASE + "/" + run["id"])).status_code == 401
            assert (
                await request(
                    h, "GET", BASE.replace("fixture_house", "foreign") + "/" + run["id"]
                )
            ).status_code == 404
            from app.models import HouseMembershipORM

            with Session(h.engine) as session:
                row = session.get(SceneRunORM, run["id"])
                membership = (
                    session.query(HouseMembershipORM)
                    .filter_by(user_id=row.requested_by, house_id="fixture_house")
                    .one()
                )
                membership.role = "technician"
                session.commit()
            before = len(h.wire.published)
            assert (await dispatch(h, run, 0)).status_code == 403
            assert len(h.wire.published) == before

    asyncio.run(check())


def test_scene_does_not_accept_retained_wrong_or_single_sided_evidence(
    tmp_path, monkeypatch
):
    async def check():
        async with fixture(tmp_path, monkeypatch) as h:
            response, _ = await create(h)
            run = response.json()
            receipt = (await dispatch(h, run, 0)).json()["actions"][0]["receipt"]
            await h.ack(receipt, retained=True)
            await h.state(receipt, True, retained=True)
            await h.ack(receipt, correlation_id="wrong-correlation")
            assert (await read(h, run))["actions"][0]["status"] == "dispatched"
            await h.ack(receipt)
            await h.state(receipt, False)
            assert (await read(h, run))["actions"][1]["status"] == "waiting"
            await h.state(receipt, True)
            assert (await read(h, run))["actions"][1]["status"] == "ready"

    asyncio.run(check())


def test_command_and_scene_link_roll_back_together_before_commit(tmp_path, monkeypatch):
    async def check():
        async with fixture(tmp_path, monkeypatch) as h:
            response, _ = await create(h)
            run = response.json()
            from app.repositories.command_repository import CommandRepository
            from app.models import DeviceCommandORM

            original = CommandRepository.save

            def crash(self):
                if any(isinstance(row, SceneRunReceiptORM) for row in self.session.new):
                    raise RuntimeError("injected crash before atomic commit")
                original(self)

            monkeypatch.setattr(CommandRepository, "save", crash)
            before = len(h.wire.published)
            with Session(h.engine) as session:
                user = session.get(SceneRunORM, run["id"]).requested_by
                with pytest.raises(RuntimeError, match="injected crash"):
                    await SceneRunService(session, h.gateway).dispatch(
                        "fixture_house", run["id"], 0, user
                    )
            with Session(h.engine) as session:
                assert session.query(SceneRunReceiptORM).count() == 0
                assert session.query(DeviceCommandORM).count() == 0
            assert (await read(h, run))["status"] == "unknown"
            assert len(h.wire.published) == before

    asyncio.run(check())
