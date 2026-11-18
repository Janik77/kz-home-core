"""Actual clock, Core process, PostgreSQL and MQTT TLS; CI software fixture only."""

import asyncio
from datetime import UTC, datetime, timedelta
import os
from sqlalchemy import text
from app.transports.mqtt_models import CommandEnvelope


async def acceptance(api, mqtt, relay, engine, stop, start):
    assert os.environ.get("GITHUB_ACTIONS") == "true", (
        "CI only; do not touch local containers"
    )
    house = await api.request("GET", "/houses/fixture_house")
    await api.request(
        "PATCH",
        "/houses/fixture_house",
        {"timezone": "UTC", "expected_timezone_revision": house["timezone_revision"]},
    )
    now = datetime.now(UTC)
    # Save strictly before target minute; never depend on same-minute catch-up.
    occurrence = now.replace(second=0, microsecond=0) + timedelta(minutes=1)
    if (occurrence - now).total_seconds() < 8:
        occurrence += timedelta(minutes=1)
    await api.request(
        "POST",
        "/automations",
        dict(
            id="live_schedule",
            name="CI schedule",
            house_id="fixture_house",
            enabled=True,
            trigger=dict(
                type="schedule",
                time=occurrence.strftime("%H:%M"),
                weekdays=[occurrence.weekday()],
            ),
            conditions=[],
            actions=[
                dict(type="device_state", device_id="fixture_relay", state={"on": True})
            ],
        ),
    )
    # Existing software state remains fresh (<120sec). No fake clock or event trigger.
    message = await asyncio.wait_for(
        mqtt.messages.__anext__(), (occurrence - now).total_seconds() + 15
    )
    envelope = CommandEnvelope.model_validate_json(bytes(message.payload))
    assert not message.retain and envelope.state == {"on": True}
    assert datetime.now(UTC) >= occurrence
    await relay.handle(str(message.topic), bytes(message.payload), message.retain)
    async with asyncio.timeout(10):
        while True:
            runs = await api.request("GET", "/automations/live_schedule/runs")
            if runs and runs[0]["status"] == "succeeded":
                break
            await asyncio.sleep(0.1)
    assert (
        len(runs) == 1
        and runs[0]["actions"][0]["receipt"]["command_id"] == envelope.command_id
    )
    await stop()
    await start()
    try:
        await asyncio.wait_for(mqtt.messages.__anext__(), 3)
    except TimeoutError:
        pass
    else:
        raise AssertionError("Schedule command replayed after Core process restart")
    restored = await api.request("GET", "/automations/live_schedule/runs")
    assert len(restored) == 1 and restored[0]["id"] == runs[0]["id"]
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM physical_devices")) == 0
        assert (
            connection.scalar(
                text("SELECT count(*) FROM scene_runs WHERE scene_id='live_schedule'")
            )
            == 1
        )
        assert (
            connection.scalar(
                text(
                    "SELECT schedule_state->>'occurrence' FROM automations WHERE id='live_schedule'"
                )
            )
            == occurrence.isoformat()
        )
    print(
        "PASS real schedule clock -> PG occurrence claim -> MQTT TLS software relay ACK+state; Core process restart zero replay; no physical devices",
        flush=True,
    )
