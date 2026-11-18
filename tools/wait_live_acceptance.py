"""CI-only real PostgreSQL/TLS wait checkpoints, restart and cancellation."""

import asyncio
from datetime import datetime
import os
from uuid import uuid4
import httpx
from sqlalchemy import text
from app.transports.mqtt_models import CommandEnvelope


async def acceptance(api, mqtt, relay, engine, stop, start):
    assert os.environ.get("GITHUB_ACTIONS") == "true", "CI-only disposable fixture"
    base = "/v1/houses/fixture_house/scene-runs"
    async with httpx.AsyncClient(
        base_url=api.base, headers={"Authorization": "Bearer " + api.token}, timeout=15
    ) as http:

        async def read(run):
            response = await http.get(base + "/" + run["id"])
            assert response.status_code == 200, response.text
            return response.json()

        async def status(run, expected):
            async with asyncio.timeout(12):
                while True:
                    value = await read(run)
                    if value["status"] == expected:
                        return value
                    await asyncio.sleep(0.1)

        async def silent(seconds=1):
            try:
                await asyncio.wait_for(mqtt.messages.__anext__(), seconds)
            except TimeoutError:
                return
            raise AssertionError("Unexpected MQTT command/replay")

        async def complete(run, index):
            message = await asyncio.wait_for(mqtt.messages.__anext__(), 12)
            envelope = CommandEnvelope.model_validate_json(bytes(message.payload))
            assert not message.retain and envelope.state == {"on": index == 0}
            value = await read(run)
            receipt = value["actions"][index]["receipt"]
            assert receipt["command_id"] == envelope.command_id
            assert datetime.fromisoformat(
                receipt["expires_at"]
            ) - datetime.fromisoformat(receipt["created_at"]) <= __import__(
                "datetime"
            ).timedelta(seconds=30)
            await relay.handle(
                str(message.topic), bytes(message.payload), message.retain
            )
            return receipt

        for mode in ("restart", "cancel", "overdue"):
            scene = "wait_" + mode
            seconds = 3 if mode == "restart" else 1 if mode == "overdue" else 10
            await api.request(
                "POST",
                "/scenes",
                dict(
                    id=scene,
                    name=scene,
                    house_id="fixture_house",
                    actions=[
                        dict(
                            type="device_state",
                            device_id="fixture_relay",
                            state={"on": True},
                        ),
                        dict(type="durable_wait", seconds=seconds),
                        dict(
                            type="device_state",
                            device_id="fixture_relay",
                            state={"on": False},
                        ),
                    ],
                ),
            )
            key = str(uuid4())
            body = {"scene_id": scene, "expected_revision": 1}
            response = await http.post(
                base + "/execute", json=body, headers={"Idempotency-Key": key}
            )
            assert response.status_code == 201, response.text
            run = response.json()
            first = await complete(run, 0)
            waiting = await status(run, "waiting")
            assert waiting["wait_due_at"] and waiting["actions"][1]["receipt"] is None
            if mode == "restart":
                await stop()
                await start()
                second = await complete(run, 2)
                assert datetime.fromisoformat(
                    second["created_at"]
                ) >= datetime.fromisoformat(waiting["wait_due_at"])
                done = await status(run, "succeeded")
                assert done["dispatch_deadline"] == run["dispatch_deadline"]
                assert (
                    done["actions"][0]["receipt"]["command_id"] == first["command_id"]
                )
                assert [x["status"] for x in done["actions"]] == [
                    "applied",
                    "completed",
                    "applied",
                ]
                replay = await http.post(
                    base + "/execute", json=body, headers={"Idempotency-Key": key}
                )
                assert replay.json()["id"] == run["id"]
            elif mode == "cancel":
                path = base + "/" + run["id"] + "/cancel"
                cancelled = await http.post(path)
                assert (
                    cancelled.status_code == 200
                    and cancelled.json()["status"] == "cancelled"
                )
                assert (await http.post(path)).json() == cancelled.json()
            else:
                await stop()
                await asyncio.sleep(33)
                await start()
                done = await status(run, "expired")
                assert done["actions"][2]["receipt"] is None
            await silent()
        with engine.connect() as conn:
            assert conn.scalar(text("SELECT count(*) FROM physical_devices")) == 0
            assert conn.scalar(text("SELECT count(*) FROM scene_run_receipts")) == 4
        print(
            "PASS actual PG/TLS A -> persisted wait -> restart -> B ACK/state; waiting cancellation and real overdue restart emit zero successor; TTL unchanged; software only",
            flush=True,
        )
