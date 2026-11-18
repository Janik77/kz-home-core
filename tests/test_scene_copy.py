"""Scene lifecycle/copy operations never imply execution or copy runtime state."""

import asyncio
from uuid import uuid4
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.models import SceneORM, AutomationORM, SceneRunORM, DeviceCommandORM
from tests.test_scene_runs import fixture, request, BASE


async def source(h):
    scene = await request(
        h,
        "POST",
        "/scenes",
        json={
            "id": "source",
            "name": "Source",
            "house_id": "fixture_house",
            "actions": [{"device_id": "software_relay", "state": {"on": True}}],
        },
    )
    assert scene.status_code == 201, scene.text
    return scene.json()


async def copied(h):
    original = await source(h)
    response = await request(
        h,
        "POST",
        "/scenes/source/duplicate",
        json={"id": "copy", "name": "Copy", "expected_revision": original["revision"]},
    )
    assert response.status_code == 201, response.text
    return original, response.json()


def test_copy_edit_activate_explicit_run_ack_state(tmp_path, monkeypatch):
    async def check():
        async with fixture(tmp_path, monkeypatch) as h:
            original, draft = await copied(h)
            assert draft["is_draft"] and draft["revision"] == 1
            assert not original["is_draft"]
            for endpoint in ("/scenes/copy/run", BASE, BASE + "/execute"):
                kw = (
                    {}
                    if endpoint.startswith("/scenes")
                    else {
                        "json": {"scene_id": "copy", "expected_revision": 1},
                        "headers": {"Idempotency-Key": str(uuid4())},
                    }
                )
                denied = await request(h, "POST", endpoint, **kw)
                assert denied.status_code == 409, denied.text
                assert denied.json()["code"] == "scene_draft"
            retry = await request(
                h,
                "POST",
                "/scenes/source/duplicate",
                json={"id": "copy", "name": "Copy", "expected_revision": 1},
            )
            assert retry.status_code == 409
            for bad in (True, 0, "1", 10**100):
                assert (
                    await request(
                        h,
                        "POST",
                        "/scenes/copy/activate",
                        json={"expected_revision": bad},
                    )
                ).status_code == 422
            update = await request(
                h,
                "PATCH",
                "/scenes/copy",
                json={
                    "name": "Edited",
                    "actions": [
                        {"device_id": "software_relay", "state": {"on": False}}
                    ],
                    "expected_revision": 1,
                },
            )
            assert update.status_code == 200, update.text
            assert update.json()["is_draft"] and update.json()["revision"] == 2
            assert (
                await request(
                    h,
                    "PATCH",
                    "/scenes/copy",
                    json={"name": "stale", "expected_revision": 1},
                )
            ).status_code == 409
            assert (
                await request(
                    h, "POST", "/scenes/copy/activate", json={"expected_revision": 1}
                )
            ).status_code == 409
            assert (
                await request(
                    h,
                    "PATCH",
                    "/scenes/copy",
                    json={"is_draft": False, "expected_revision": 2},
                )
            ).status_code == 422
            activated = await request(
                h, "POST", "/scenes/copy/activate", json={"expected_revision": 2}
            )
            assert activated.status_code == 200, activated.text
            assert (
                activated.json()["revision"] == 3 and not activated.json()["is_draft"]
            )
            assert (
                await request(
                    h, "POST", "/scenes/copy/activate", json={"expected_revision": 3}
                )
            ).status_code == 409
            assert (await request(h, "GET", "/scenes/source")).json() == original
            assert not h.wire.published
            with Session(h.engine) as s:
                assert not s.scalars(select(SceneRunORM)).all()
                assert not s.scalars(select(DeviceCommandORM)).all()
            run = await request(
                h,
                "POST",
                BASE,
                json={"scene_id": "copy", "expected_revision": 3},
                headers={"Idempotency-Key": str(uuid4())},
            )
            assert run.status_code == 201, run.text
            dispatched = await request(
                h, "POST", f"{BASE}/{run.json()['id']}/actions/0/dispatch"
            )
            assert dispatched.status_code == 202, dispatched.text
            receipt = dispatched.json()["actions"][0]["receipt"]
            await h.ack(receipt)
            await h.state(receipt, False)
            final = await request(h, "GET", BASE + "/" + run.json()["id"])
            assert final.json()["status"] == "succeeded"
            assert len(h.wire.published) == 1

    asyncio.run(check())


def test_unknown_constraints_readonly_and_activation_static_offline(
    tmp_path, monkeypatch
):
    async def check():
        async with fixture(tmp_path, monkeypatch) as h:
            await copied(h)
            with Session(h.engine) as s:
                src = s.get(SceneORM, "source")
                src.actions = [
                    {**src.actions[0], "future_constraint": "do not discard"}
                ]
                from app.models import DeviceORM

                s.get(DeviceORM, "software_relay").online = False
                s.commit()
            read = await request(h, "GET", "/scenes/source")
            assert read.json()["actions"][0]["future_constraint"] == "do not discard"
            assert (
                await request(
                    h,
                    "PATCH",
                    "/scenes/source",
                    json={"name": "lossy", "expected_revision": 1},
                )
            ).status_code == 422
            assert (
                await request(
                    h,
                    "POST",
                    "/scenes/source/duplicate",
                    json={"id": "bad", "name": "bad", "expected_revision": 1},
                )
            ).status_code == 422
            # Static activation intentionally doesn't require online/live control authority.
            activated = await request(
                h, "POST", "/scenes/copy/activate", json={"expected_revision": 1}
            )
            assert activated.status_code == 200, activated.text
            assert not h.wire.published

    asyncio.run(check())


def test_automation_copy_definition_only_and_disabled_edit_no_authority(
    tmp_path, monkeypatch
):
    async def check():
        async with fixture(tmp_path, monkeypatch) as h:
            response = await request(
                h,
                "POST",
                "/automations",
                json={
                    "id": "rule",
                    "house_id": "fixture_house",
                    "name": "Rule",
                    "enabled": False,
                    "trigger": {
                        "device_id": "software_relay",
                        "field": "on",
                        "value": True,
                    },
                    "actions": [
                        {"device_id": "software_relay", "state": {"on": False}}
                    ],
                },
            )
            assert response.status_code == 201, response.text
            with Session(h.engine) as s:
                row = s.get(AutomationORM, "rule")
                row.threshold_state = {"sentinel": "old latch"}
                row.schedule_state = {"sentinel": "old occurrence"}
                s.commit()
            copied = await request(
                h,
                "POST",
                "/automations/rule/duplicate",
                json={"id": "rule-copy", "name": "Copy", "expected_revision": 1},
            )
            assert copied.status_code == 201, copied.text
            assert copied.json()["enabled"] is False
            edited = await request(
                h,
                "PATCH",
                "/automations/rule-copy",
                json={"name": "Edited", "expected_revision": 1},
            )
            assert edited.status_code == 200, edited.text
            with Session(h.engine) as s:
                row = s.get(AutomationORM, "rule-copy")
                assert row.authorized_by is None
                assert row.threshold_state == row.schedule_state == {}
                assert s.get(AutomationORM, "rule").threshold_state == {
                    "sentinel": "old latch"
                }
                assert not s.scalars(select(SceneRunORM)).all()
            assert not h.wire.published

    asyncio.run(check())


def test_source_revision_permissions_and_invalid_activation_leave_no_intent(
    tmp_path, monkeypatch
):
    async def check():
        async with fixture(tmp_path, monkeypatch) as h:
            await copied(h)
            changed = await request(
                h,
                "PATCH",
                "/scenes/source",
                json={"name": "Changed", "expected_revision": 1},
            )
            assert changed.status_code == 200
            stale = await request(
                h,
                "POST",
                "/scenes/source/duplicate",
                json={"id": "stale-copy", "name": "Copy", "expected_revision": 1},
            )
            assert (
                stale.status_code == 409
                and stale.json()["code"] == "definition_revision_conflict"
            )
            with Session(h.engine) as s:
                from app.models import HouseMembershipORM, DeviceORM

                member = s.scalars(
                    select(HouseMembershipORM).where(
                        HouseMembershipORM.house_id == "fixture_house"
                    )
                ).first()
                member.role = "resident"
                s.commit()
            for endpoint, data in (
                (
                    "/scenes/source/duplicate",
                    {"id": "forbidden", "name": "Copy", "expected_revision": 2},
                ),
                ("/scenes/copy/activate", {"expected_revision": 1}),
            ):
                assert (
                    await request(h, "POST", endpoint, json=data)
                ).status_code == 403
            with Session(h.engine) as s:
                s.scalars(
                    select(HouseMembershipORM).where(
                        HouseMembershipORM.house_id == "fixture_house"
                    )
                ).first().role = "owner"
                s.get(DeviceORM, "software_relay").capabilities = []
                s.commit()
            bad = await request(
                h, "POST", "/scenes/copy/activate", json={"expected_revision": 1}
            )
            assert bad.status_code == 422, bad.text
            unchanged = await request(h, "GET", "/scenes/copy")
            assert unchanged.json()["is_draft"] and unchanged.json()["revision"] == 1
            with Session(h.engine) as s:
                assert s.get(SceneORM, "stale-copy") is None
                assert s.get(SceneORM, "forbidden") is None
                assert not s.scalars(select(SceneRunORM)).all()
                assert not s.scalars(select(DeviceCommandORM)).all()
            assert not h.wire.published

    asyncio.run(check())
