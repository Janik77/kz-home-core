"""Numeric software sensors over the actual MQTT gateway and migrated database."""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4
import pytest
from sqlalchemy.orm import Session
from app.models import AutomationORM, DeviceORM, SceneRunORM
from app.schemas import AutomationTrigger
from app.services.state_validator import StateValidator
from app.core.errors import InvalidReferenceError
from test_command_lifecycle import fixture
from test_scene_runs import request
from test_durable_automations import runs, wait


async def sensor(h):
    with Session(h.engine) as db:
        relay = db.get(DeviceORM, "software_relay")
        db.add(
            DeviceORM(
                id="numeric_sensor",
                name="Software sensor",
                room_id=relay.room_id,
                type="illuminance_sensor",
                capabilities=["temperature", "humidity", "illuminance"],
                state={},
                online=True,
            )
        )
        db.commit()


async def sample(
    h, value, *, field="illuminance", stamp=None, retained=False, correlation=None
):
    stamp = stamp or datetime.now(UTC).isoformat()
    await h.message(
        "state",
        {
            "timestamp": stamp,
            "correlation_id": correlation or str(uuid4()),
            "state": {field: value},
        },
        retained,
        device="numeric_sensor",
    )
    await asyncio.sleep(0.04)  # Existing event worker runs after MQTT persistence.
    return stamp


async def numeric_rule(h, *, enabled=True, conditions=None, operator="lt", value=100):
    response = await request(
        h,
        "POST",
        "/automations",
        json={
            "id": "numeric_" + uuid4().hex,
            "name": "Low light",
            "house_id": "fixture_house",
            "enabled": enabled,
            "trigger": {
                "type": "numeric_threshold",
                "device_id": "numeric_sensor",
                "field": "illuminance",
                "operator": operator,
                "value": value,
                "hysteresis": 20,
            },
            "conditions": conditions or [],
            "actions": [
                {
                    "type": "device_state",
                    "device_id": "software_relay",
                    "state": {"on": True},
                }
            ],
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


@pytest.mark.parametrize(
    "value", [True, False, float("nan"), float("inf"), -1, 200001, "10", None]
)
def test_bad_numeric_values(value):
    with pytest.raises(InvalidReferenceError):
        StateValidator._validate_values({"illuminance": value})
    with pytest.raises(ValueError):
        AutomationTrigger(
            type="numeric_threshold",
            device_id="x",
            field="illuminance",
            operator="lt",
            value=value,
            hysteresis=20,
        )


@pytest.mark.parametrize(
    "hysteresis", [True, 0, -1, float("nan"), float("inf"), "20", 200001]
)
def test_bad_hysteresis(hysteresis):
    with pytest.raises(ValueError):
        AutomationTrigger(
            type="numeric_threshold",
            device_id="x",
            field="illuminance",
            operator="lt",
            value=100,
            hysteresis=hysteresis,
        )


def test_crossing_hysteresis_duplicates_restart_and_confirmed_receipt(
    tmp_path, monkeypatch
):
    async def check():
        async with fixture(
            tmp_path,
            monkeypatch,
            extra_software_devices=(("fixture_house", "numeric_sensor"),),
        ) as h:
            await sensor(h)
            a = await numeric_rule(h)
            await sample(h, 50)  # Startup on bad side must not send a command.
            await sample(h, 99)
            await sample(h, 110)  # Not enough recovery.
            await sample(h, 90)
            assert await runs(h, a) == []
            old = await sample(h, 120)  # inclusive re-arm
            await sample(h, 100)  # strict crossing
            assert await runs(h, a) == []
            await sample(h, 99)
            run = await wait(
                h, a, lambda r: r["actions"] and r["actions"][0]["receipt"]
            )
            receipt = run["actions"][0]["receipt"]
            await h.ack(receipt)
            await h.state(receipt, True)
            await wait(h, a, lambda r: r["status"] == "succeeded")
            # Dispose connections: next gateway processing uses persisted state only.
            h.engine.dispose()
            await sample(h, 120, stamp=old)  # old duplicate must not rearm
            await sample(h, 90)
            assert len(await runs(h, a)) == 1
            with Session(h.engine) as db:
                latch = db.get(AutomationORM, a["id"]).threshold_state
                assert not latch["armed"]
                row = db.query(SceneRunORM).filter_by(scene_id=a["id"]).one()
                row.created_at = datetime.now(UTC) - timedelta(seconds=31)
                db.commit()
            await sample(h, 120)
            await sample(h, 99)
            assert len(await runs(h, a)) == 2

    asyncio.run(check())


def test_stale_retained_offline_disable_revision_and_field_freshness(
    tmp_path, monkeypatch
):
    async def check():
        async with fixture(
            tmp_path,
            monkeypatch,
            extra_software_devices=(("fixture_house", "numeric_sensor"),),
        ) as h:
            await sensor(h)
            a = await numeric_rule(h)
            await sample(h, 120)
            await sample(h, 50, retained=True)
            await sample(h, 50)  # retained input broke continuity: initialize only
            assert await runs(h, a) == []
            await sample(h, 120)
            await sample(
                h, 50, stamp=(datetime.now(UTC) - timedelta(minutes=3)).isoformat()
            )
            await sample(h, 50)
            assert await runs(h, a) == []
            await sample(h, 120)
            await h.message(
                "status",
                {"status": "offline", "last_seen": datetime.now(UTC).isoformat()},
                device="numeric_sensor",
            )
            await h.message(
                "status",
                {"status": "online", "last_seen": datetime.now(UTC).isoformat()},
                device="numeric_sensor",
            )
            await sample(h, 50)
            assert await runs(h, a) == []
            response = await request(
                h,
                "PATCH",
                "/automations/" + a["id"],
                json={"expected_revision": 1, "enabled": False},
            )
            assert response.status_code == 200
            await sample(h, 120)
            await sample(h, 50)
            assert await runs(h, a) == []
            response = await request(
                h,
                "PATCH",
                "/automations/" + a["id"],
                json={"expected_revision": 2, "enabled": True},
            )
            assert response.status_code == 200
            await sample(h, 50)
            assert await runs(h, a) == []
            await sample(h, 25, field="temperature")
            with Session(h.engine) as db:
                d = db.get(DeviceORM, "numeric_sensor")
                d.numeric_observed_at = {
                    **d.numeric_observed_at,
                    "illuminance": (
                        datetime.now(UTC) - timedelta(minutes=3)
                    ).isoformat(),
                }
                db.commit()
            await sample(h, 26, field="temperature")
            dry = await request(h, "POST", "/automations/" + a["id"] + "/dry-run")
            assert "state_unavailable" in dry.json()["issues"]

    asyncio.run(check())


def test_numeric_conditions_and_legacy_not_activated(tmp_path, monkeypatch):
    async def check():
        async with fixture(
            tmp_path,
            monkeypatch,
            extra_software_devices=(("fixture_house", "numeric_sensor"),),
        ) as h:
            await sensor(h)
            await sample(h, 25, field="temperature")
            c = {
                "type": "numeric_state",
                "device_id": "numeric_sensor",
                "field": "temperature",
                "operator": "gte",
                "value": 30,
            }
            a = await numeric_rule(h, conditions=[c])
            await sample(h, 120)
            await sample(h, 90)
            rows = await runs(h, a)
            assert len(rows) == 1 and rows[0]["execution_error"] == "conditions_not_met"
            await sample(h, 35, field="temperature")
            await sample(h, 80)
            assert (
                len(await runs(h, a)) == 1
            )  # refusal consumed edge; no delayed execution
            with Session(h.engine) as db:
                rule = db.get(AutomationORM, a["id"])
                rule.trigger = {
                    **rule.trigger,
                    "type": "device_state",
                    "hysteresis": None,
                }
                db.commit()
            dry = await request(h, "POST", "/automations/" + a["id"] + "/dry-run")
            assert "unsupported_definition" in dry.json()["issues"]

    asyncio.run(check())


@pytest.mark.parametrize(
    "field,value",
    [
        ("temperature", -100),
        ("temperature", 150),
        ("humidity", 0),
        ("humidity", 100),
        ("illuminance", 0),
        ("illuminance", 200000),
    ],
)
def test_numeric_bounds_are_inclusive(field, value):
    StateValidator._validate_values({field: value})


def test_greater_threshold_and_revision_initialization(tmp_path, monkeypatch):
    async def check():
        async with fixture(
            tmp_path,
            monkeypatch,
            extra_software_devices=(("fixture_house", "numeric_sensor"),),
        ) as h:
            await sensor(h)
            a = await numeric_rule(h, operator="gt")
            await sample(h, 101)  # initial above threshold never fires
            await sample(h, 80)
            await sample(h, 100)
            assert await runs(h, a) == []
            await sample(h, 101)
            run = await wait(
                h, a, lambda r: r["actions"] and r["actions"][0]["receipt"]
            )
            await h.ack(run["actions"][0]["receipt"])
            await h.state(run["actions"][0]["receipt"], True)
            await wait(h, a, lambda r: r["status"] == "succeeded")
            response = await request(
                h,
                "PATCH",
                "/automations/" + a["id"],
                json={"expected_revision": 1, "name": "Updated"},
            )
            assert response.status_code == 200
            await sample(h, 102)
            assert len(await runs(h, a)) == 1
            with Session(h.engine) as db:
                assert db.get(AutomationORM, a["id"]).threshold_state["revision"] == 2
                assert not db.get(AutomationORM, a["id"]).threshold_state["armed"]

    asyncio.run(check())


def test_numeric_simulator_duplicates_preserve_envelope_and_never_retain():
    import json
    from simulator.mqtt_numeric import NumericSensor

    calls = []

    async def publish(topic, payload, **kwargs):
        calls.append((topic, json.loads(payload), kwargs))

    async def check():
        probe = NumericSensor(publish, "e2e_house", "e2e_motion")
        await probe.reading("illuminance", 90, duplicate=True)

    asyncio.run(check())
    assert len(calls) == 2 and calls[0] == calls[1]
    assert calls[0][0] == "kzhome/v1/e2e_house/e2e_motion/state"
    assert calls[0][2] == {"qos": 1, "retain": False}
    with pytest.raises(ValueError):
        NumericSensor(publish, "real_house", "real_sensor")


def test_get_definitions_canonical_roundtrip_no_irrelevant_hysteresis(
    tmp_path, monkeypatch
):
    from test_durable_automations import rule

    async def check():
        async with fixture(
            tmp_path,
            monkeypatch,
            extra_software_devices=(("fixture_house", "numeric_sensor"),),
        ) as h:
            boolean = await rule(h)
            response = await request(h, "GET", "/automations/" + boolean["id"])
            assert "hysteresis" not in response.json()["trigger"]
            await sensor(h)
            c = {
                "type": "numeric_state",
                "device_id": "numeric_sensor",
                "field": "temperature",
                "operator": "gte",
                "value": 30,
            }
            numeric = await numeric_rule(h, conditions=[c])
            response = await request(h, "GET", "/automations/" + numeric["id"])
            definition = response.json()
            assert definition["trigger"]["hysteresis"] == 20
            assert "hysteresis" not in definition["conditions"][0]
            edit = await request(
                h,
                "PATCH",
                "/automations/" + numeric["id"],
                json={
                    "expected_revision": definition["revision"],
                    "trigger": definition["trigger"],
                    "conditions": definition["conditions"],
                },
            )
            assert edit.status_code == 200, edit.text
            assert edit.json()["conditions"] == definition["conditions"]

    asyncio.run(check())


@pytest.mark.parametrize("kind", ["numeric_threshold", "numeric_state"])
def test_numeric_unknown_fields_are_not_silently_dropped(kind):
    from app.schemas import DeviceStateCondition

    cls = AutomationTrigger if kind == "numeric_threshold" else DeviceStateCondition
    body = {
        "type": kind,
        "device_id": "x",
        "field": "illuminance",
        "operator": "lt",
        "value": 100,
        "duration_seconds": 30,
    }
    if kind == "numeric_threshold":
        body["hysteresis"] = 20
    with pytest.raises(ValueError, match="Unknown numeric"):
        cls.model_validate(body)
