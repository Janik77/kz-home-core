"""Independent automation safety QA; real API/ledger, fake MQTT network only."""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4
from sqlalchemy.orm import Session
from app.models import DeviceORM, SceneRunORM, UserORM
from app.services.scene_executor import SceneExecutor
from test_command_lifecycle import fixture
from test_durable_automations import rule, runs, wait
from test_scene_runs import request


def actions():
    return [
        dict(type="device_state", device_id="software_relay", state={"on": on})
        for on in (False, True)
    ]


def test_heartbeat_cannot_refresh_stale_state(tmp_path, monkeypatch):
    async def check():
        async with fixture(tmp_path, monkeypatch) as h:
            a = await rule(h)
            with Session(h.engine) as session:
                session.get(DeviceORM, "software_relay").state_observed_at = (
                    datetime.now(UTC) - timedelta(minutes=3)
                )
                session.commit()
            await h.message(
                "status",
                {"status": "online", "last_seen": datetime.now(UTC).isoformat()},
            )
            baseline = len(h.wire.published)
            response = await request(h, "POST", f"/automations/{a['id']}/dry-run")
            assert response.status_code == 200, response.text
            assert not response.json()["eligible"]
            assert "state_unavailable" in response.json()["issues"]
            assert len(h.wire.published) == baseline
            assert await runs(h, a) == []

    asyncio.run(check())


def test_revoked_user_stops_unclaimed_second_action(tmp_path, monkeypatch):
    async def check():
        async with fixture(tmp_path, monkeypatch) as h:
            a = await rule(h, actions=actions())
            before = len(h.wire.published)
            await h.state({"correlation_id": str(uuid4())}, True)
            run = await wait(
                h, a, lambda r: r["actions"] and r["actions"][0]["receipt"]
            )
            receipt = run["actions"][0]["receipt"]
            with Session(h.engine) as session:
                actor = session.get(SceneRunORM, run["id"]).requested_by
                session.get(UserORM, actor).is_active = False
                session.commit()
            await h.ack(receipt)
            await h.state(receipt, False)
            await asyncio.sleep(0.5)
            with Session(h.engine) as session:
                row = session.get(SceneRunORM, run["id"])
                assert row.execution_finished
                assert row.execution_error
                session.get(UserORM, actor).is_active = True
                session.commit()
            result = (await runs(h, a))[0]
            assert result["status"] == "partial"
            assert result["actions"][1]["receipt"] is None
            assert len(h.wire.published) == before + 1

    asyncio.run(check())


def test_executor_cancellation_and_new_owner_cannot_replay(tmp_path, monkeypatch):
    captured = []
    original = SceneExecutor.start

    def capture(self, *args):
        captured.append((self, args))
        return original(self, *args)

    monkeypatch.setattr(SceneExecutor, "start", capture)

    async def check():
        async with fixture(tmp_path, monkeypatch) as h:
            a = await rule(h, actions=actions())
            before = len(h.wire.published)
            await h.state({"correlation_id": str(uuid4())}, True)
            await wait(h, a, lambda r: r["actions"] and r["actions"][0]["receipt"])
            executor, args = captured[0]
            await executor.stop()
            # Simulated new worker ownership, not a claim of a process restart test.
            await executor.run(args[0], args[1], str(uuid4()))
            result = (await runs(h, a))[0]
            assert result["status"] == "unknown"
            assert result["actions"][1]["receipt"] is None
            assert len(h.wire.published) == before + 1

    asyncio.run(check())


def test_automation_chain_suppressed_but_new_external_event_allowed(
    tmp_path, monkeypatch
):
    async def check():
        async with fixture(tmp_path, monkeypatch) as h:
            first = await rule(h)
            second = await rule(
                h,
                actions=[
                    dict(
                        type="device_state",
                        device_id="software_relay",
                        state={"on": True},
                    )
                ],
            )
            edit = await request(
                h,
                "PATCH",
                "/automations/" + second["id"],
                json={
                    "expected_revision": 1,
                    "trigger": dict(
                        type="device_state",
                        device_id="software_relay",
                        field="on",
                        operator="eq",
                        value=False,
                    ),
                },
            )
            assert edit.status_code == 200, edit.text
            await h.state({"correlation_id": str(uuid4())}, True)
            started = await wait(
                h, first, lambda r: r["actions"] and r["actions"][0]["receipt"]
            )
            receipt = started["actions"][0]["receipt"]
            await h.ack(receipt)
            await h.state(receipt, False)
            await wait(h, first, lambda r: r["status"] == "succeeded")
            assert await runs(h, second) == []
            # Changing away then back with independent correlation is a new external event.
            await h.state({"correlation_id": str(uuid4())}, True)
            await h.state({"correlation_id": str(uuid4())}, False)
            started = await wait(
                h, second, lambda r: r["actions"] and r["actions"][0]["receipt"]
            )
            receipt = started["actions"][0]["receipt"]
            await h.ack(receipt)
            await h.state(receipt, True)
            await wait(h, second, lambda r: r["status"] == "succeeded")

    asyncio.run(check())
