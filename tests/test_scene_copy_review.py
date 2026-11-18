"""Independent API/SQLite lifecycle guards; network is a recording double."""

import asyncio
from sqlalchemy.orm import Session
from app.models import SceneORM, DeviceCommandORM
from sqlalchemy import select
from tests.test_scene_runs import fixture, request, create, dispatch, BASE


def test_existing_receipt_replay_survives_but_new_dispatch_denied_for_draft(
    tmp_path, monkeypatch
):
    async def check():
        async with fixture(tmp_path, monkeypatch) as h:
            response, key = await create(h)
            run = response.json()
            sent = await dispatch(h, run, 0)
            assert sent.status_code == 202, sent.text
            receipt = sent.json()["actions"][0]["receipt"]
            await h.ack(receipt)
            await h.state(receipt, True)
            # Adversarial persisted state (e.g. same-ID replacement), not a permitted PATCH.
            with Session(h.engine) as session:
                session.get(SceneORM, run["scene_id"]).is_draft = True
                session.commit()
            replay = await request(
                h,
                "POST",
                BASE,
                json={"scene_id": run["scene_id"], "expected_revision": 1},
                headers={"Idempotency-Key": key},
            )
            assert replay.status_code in (200, 201), replay.text
            assert replay.json()["id"] == run["id"]
            previous = await dispatch(h, run, 0)
            assert previous.status_code == 202, previous.text
            assert (
                previous.json()["actions"][0]["receipt"]
                == (await request(h, "GET", BASE + "/" + run["id"])).json()["actions"][
                    0
                ]["receipt"]
            )
            denied = await dispatch(h, run, 1)
            assert denied.status_code == 409, denied.text
            assert denied.json()["code"] == "scene_draft"
            assert len(h.wire.published) == 1
            with Session(h.engine) as session:
                assert len(session.scalars(select(DeviceCommandORM)).all()) == 1

    asyncio.run(check())


def test_nondurable_manual_snapshot_rechecks_fresh_draft_state(tmp_path, monkeypatch):
    async def check():
        async with fixture(tmp_path, monkeypatch) as h:
            response, _ = await create(h)
            run = response.json()
            with Session(h.engine) as session:
                session.get(SceneORM, run["scene_id"]).is_draft = True
                session.commit()
            denied = await dispatch(h, run, 0)
            assert denied.status_code == 409, denied.text
            assert denied.json()["code"] == "scene_draft"
            assert not h.wire.published
            with Session(h.engine) as session:
                assert not session.scalars(select(DeviceCommandORM)).all()

    asyncio.run(check())


def test_unknown_action_fields_remain_readable_and_inert(tmp_path, monkeypatch):
    async def check():
        async with fixture(tmp_path, monkeypatch) as h:
            response, _ = await create(h)
            scene_id = response.json()["scene_id"]
            plans = [
                [
                    {
                        "type": "durable_wait",
                        "seconds": 2,
                        "future_guard": "must preserve",
                    }
                ],
                [{"future_guard": {"required": True}}],
                [{"type": "future_action", "future_guard": [1, "strict"]}],
            ]
            for index, plan in enumerate(plans):
                with Session(h.engine) as session:
                    session.get(SceneORM, scene_id).actions = plan
                    session.commit()
                read = await request(h, "GET", "/scenes/" + scene_id)
                assert read.status_code == 200, read.text
                assert read.json()["actions"] == plan
                copy = await request(
                    h,
                    "POST",
                    "/scenes/" + scene_id + "/duplicate",
                    json={
                        "id": f"unsupported-copy-{index}",
                        "name": "Must not copy",
                        "expected_revision": 1,
                    },
                )
                assert copy.status_code == 422, copy.text
                edit = await request(
                    h,
                    "PATCH",
                    "/scenes/" + scene_id,
                    json={"name": "Must not drop fields", "expected_revision": 1},
                )
                assert edit.status_code == 422, edit.text
                run = await request(h, "POST", "/scenes/" + scene_id + "/run")
                assert run.status_code == 422, run.text
                with Session(h.engine) as session:
                    assert session.get(SceneORM, scene_id).actions == plan
                    assert session.get(SceneORM, f"unsupported-copy-{index}") is None
                assert not h.wire.published

    asyncio.run(check())
