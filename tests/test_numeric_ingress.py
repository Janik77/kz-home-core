"""Oversized JSON integer must fail closed at real API and MQTT ingress."""

import asyncio
import pytest
from app.core.errors import InvalidReferenceError
from datetime import UTC, datetime
from uuid import uuid4
from sqlalchemy.orm import Session
from app.models import DeviceORM
from test_command_lifecycle import fixture
from test_numeric_thresholds import sensor, sample, numeric_rule
from test_durable_automations import runs
from test_scene_runs import request


def test_oversized_numeric_api_422_and_mqtt_no_state_mutation(tmp_path, monkeypatch):
    async def check():
        async with fixture(
            tmp_path,
            monkeypatch,
            extra_software_devices=(("fixture_house", "numeric_sensor"),),
        ) as h:
            await sensor(h)
            a = await numeric_rule(h)
            await sample(h, 120)
            with Session(h.engine) as db:
                d = db.get(DeviceORM, "numeric_sensor")
                before = (
                    dict(d.state),
                    dict(d.numeric_observed_at),
                    dict(d.numeric_watermarks),
                )
            for body in ({"value": 10**400}, {"hysteresis": 10**400}):
                trigger = {**a["trigger"], **body}
                response = await request(
                    h,
                    "PATCH",
                    "/automations/" + a["id"],
                    json={"expected_revision": 1, "trigger": trigger},
                )
                assert response.status_code == 422, response.status_code
            response = await request(
                h,
                "PATCH",
                "/devices/numeric_sensor",
                json={"state": {"illuminance": 10**400}},
            )
            assert response.status_code == 422, response.status_code
            with pytest.raises(InvalidReferenceError, match="Invalid numeric"):
                await h.message(
                    "state",
                    {
                        "timestamp": datetime.now(UTC).isoformat(),
                        "correlation_id": str(uuid4()),
                        "state": {"illuminance": 10**400},
                    },
                    False,
                    device="numeric_sensor",
                )
            await asyncio.sleep(0.05)
            with Session(h.engine) as db:
                d = db.get(DeviceORM, "numeric_sensor")
                assert (d.state, d.numeric_observed_at, d.numeric_watermarks) == before
            assert await runs(h, a) == []

    asyncio.run(check())
