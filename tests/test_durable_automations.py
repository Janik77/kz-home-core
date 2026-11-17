import asyncio
from uuid import uuid4
from test_command_lifecycle import fixture
from test_scene_runs import request


async def rule(h, enabled=True, actions=None):
    r = await request(
        h,
        "POST",
        "/automations",
        json=dict(
            id="auto_" + uuid4().hex,
            name="Rule",
            house_id="fixture_house",
            enabled=enabled,
            trigger=dict(
                type="device_state",
                device_id="software_relay",
                field="on",
                operator="eq",
                value=True,
            ),
            conditions=[],
            actions=actions
            or [
                dict(
                    type="device_state", device_id="software_relay", state={"on": False}
                )
            ],
        ),
    )
    assert r.status_code == 201, r.text
    return r.json()


async def runs(h, a):
    r = await request(h, "GET", "/automations/" + a["id"] + "/runs")
    assert r.status_code == 200, r.text
    return r.json()


async def wait(h, a, predicate):
    for _ in range(120):
        rows = await runs(h, a)
        if rows and predicate(rows[0]):
            return rows[0]
        await asyncio.sleep(0.025)
    raise AssertionError(rows)


def test_confirmed_event_dedup_and_dry_run(tmp_path, monkeypatch):
    async def check():
        async with fixture(tmp_path, monkeypatch) as h:
            a = await rule(h)
            dry = await request(h, "POST", "/automations/" + a["id"] + "/dry-run")
            assert dry.status_code == 200, dry.text
            assert dry.json()["eligible"], dry.text
            assert await runs(h, a) == []
            source = {"correlation_id": str(uuid4())}
            await h.state(source, True)
            run = await wait(
                h, a, lambda r: r["actions"] and r["actions"][0]["receipt"]
            )
            receipt = run["actions"][0]["receipt"]
            await h.ack(receipt)
            assert (await runs(h, a))[0]["status"] == "running"
            await h.state(receipt, False)
            await wait(h, a, lambda r: r["status"] == "succeeded")
            await h.state(source, True)
            await asyncio.sleep(0.1)
            assert len(await runs(h, a)) == 1
            assert (
                await request(h, "POST", "/automations/" + a["id"] + "/run")
            ).status_code == 409

    asyncio.run(check())


def test_disabled_and_retained_no_commands(tmp_path, monkeypatch):
    async def check():
        async with fixture(tmp_path, monkeypatch) as h:
            a = await rule(h, False)
            dry = (
                await request(h, "POST", "/automations/" + a["id"] + "/dry-run")
            ).json()
            assert dry["conditions_match"] and not dry["eligible"]
            assert "rule_disabled" in dry["issues"]
            await h.state({"correlation_id": str(uuid4())}, True)
            await asyncio.sleep(0.1)
            assert await runs(h, a) == []
            r = await request(
                h,
                "PATCH",
                "/automations/" + a["id"],
                json={"expected_revision": 1, "enabled": True},
            )
            assert r.status_code == 200, r.text
            await h.state({"correlation_id": str(uuid4())}, False)
            await h.state({"correlation_id": str(uuid4())}, True, retained=True)
            await asyncio.sleep(0.1)
            assert await runs(h, a) == []

    asyncio.run(check())


def test_disable_between_actions(tmp_path, monkeypatch):
    async def check():
        async with fixture(tmp_path, monkeypatch) as h:
            a = await rule(
                h,
                actions=[
                    dict(
                        type="device_state",
                        device_id="software_relay",
                        state={"on": False},
                    ),
                    dict(
                        type="device_state",
                        device_id="software_relay",
                        state={"on": True},
                    ),
                ],
            )
            await h.state({"correlation_id": str(uuid4())}, True)
            run = await wait(
                h, a, lambda r: r["actions"] and r["actions"][0]["receipt"]
            )
            receipt = run["actions"][0]["receipt"]
            r = await request(
                h,
                "PATCH",
                "/automations/" + a["id"],
                json={"expected_revision": 1, "enabled": False},
            )
            assert r.status_code == 200, r.text
            await h.ack(receipt)
            await h.state(receipt, False)
            run = await wait(h, a, lambda r: r["status"] == "partial")
            assert run["actions"][0]["status"] == "applied"
            assert run["actions"][1]["receipt"] is None

    asyncio.run(check())
