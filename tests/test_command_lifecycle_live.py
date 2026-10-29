"""Opt-in HTTP -> PostgreSQL -> real TLS Mosquitto -> existing software Relay.

Reuses the v0.12 exclusively owned production-component fixture; never adopts
deploy/local or installed volumes. No physical device or frontend is involved.
"""

import asyncio
import os
import ssl
import time
from uuid import uuid4

import aiomqtt
import httpx
import pytest

from simulator.mqtt_relay import Relay
from app.transports.mqtt_models import CommandEnvelope
from app.transports.mqtt_topics import TopicKind
from test_physical_acceptance import provision
from test_physical_acceptance_live import command, ready, stack as stack

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_COMMAND_LIFECYCLE_DOCKER_TESTS") != "1",
    reason="explicit RUN_COMMAND_LIFECYCLE_DOCKER_TESTS=1 required; isolated stack only",
)


def test_durable_command_api_postgresql_mqtt_tls_and_restart(request):
    dc, config, _, seed, ids, api = request.getfixturevalue("stack")
    if os.name == "nt":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    async def check():
        await ready(api)
        await provision(api, ids, seed)
        async with aiomqtt.Client(
            hostname=config.host,
            port=config.port,
            username=config.username,
            password=config.password,
            identifier=config.client_id,
            tls_context=ssl.create_default_context(cafile=config.ca_file),
        ) as mqtt:
            relay = Relay(config.house_id, config.device_id, mqtt.publish)
            await mqtt.subscribe(relay.topic(TopicKind.SET), qos=1)
            await relay.send(TopicKind.STATUS, relay.status(True))
            await relay.state("initial-uncorrelated-state")
            async with httpx.AsyncClient(base_url=api.base, timeout=10) as http:
                headers = {"Authorization": "Bearer " + api.token}

                async def wait_for(read, predicate, seconds=15):
                    deadline = time.monotonic() + seconds
                    while time.monotonic() < deadline:
                        value = await read()
                        if predicate(value):
                            return value
                        await asyncio.sleep(0.1)
                    pytest.fail("Isolated command lifecycle evidence timed out")

                async def device():
                    response = await http.get("/devices/fixture_relay", headers=headers)
                    assert response.status_code == 200
                    return response.json()

                await wait_for(
                    device, lambda d: d["online"] and d["state"].get("on") is False
                )
                messages = mqtt.messages
                receipts = []

                async def submit(on, key=None):
                    response = await http.post(
                        "/v1/devices/fixture_relay/commands",
                        json={"on": on},
                        headers={**headers, "Idempotency-Key": key or str(uuid4())},
                    )
                    assert response.status_code == 202
                    receipt = response.json()
                    receipts.append(receipt)
                    return receipt

                async def read(receipt):
                    response = await http.get(
                        f"/v1/houses/fixture_house/commands/{receipt['command_id']}",
                        headers=headers,
                    )
                    assert response.status_code == 200
                    return response.json()

                async def receive(receipt, apply=True):
                    message = await asyncio.wait_for(messages.__anext__(), 10)
                    envelope = CommandEnvelope.model_validate_json(
                        bytes(message.payload)
                    )
                    assert not message.retain
                    assert envelope.command_id == receipt["command_id"]
                    assert envelope.correlation_id == receipt["correlation_id"]
                    assert envelope.state == {"on": receipt["requested_on"]}
                    if apply:
                        await relay.handle(
                            str(message.topic), bytes(message.payload), message.retain
                        )
                        result = await wait_for(
                            lambda: read(receipt), lambda r: r["status"] == "applied"
                        )
                        assert result["reported_on"] is receipt["requested_on"]
                        assert (
                            result["ack_status"] == "applied"
                            and result["state_received_at"]
                        )
                    return message

                # Two concurrent same-key HTTP requests must produce one MQTT attempt.
                key = str(uuid4())
                a, b = await asyncio.gather(submit(True, key), submit(True, key))
                assert a["command_id"] == b["command_id"]
                conflict = await http.post(
                    "/v1/devices/fixture_relay/commands",
                    json={"on": False},
                    headers={**headers, "Idempotency-Key": str(uuid4())},
                )
                assert conflict.status_code == 409
                await receive(a)
                off = await submit(False)
                await receive(off)
                unchanged = await submit(False)
                await receive(unchanged)  # Equal state has no old state-change event.
                replay = await submit(True, key)
                assert (
                    replay["command_id"] == a["command_id"]
                    and replay["status"] == "applied"
                )
                with pytest.raises(TimeoutError):
                    await asyncio.wait_for(messages.__anext__(), 0.25)

                resident = (
                    await api.request(
                        "POST",
                        "/auth/login",
                        {
                            "email": "resident@example.test",
                            "password": seed["password"],
                        },
                    )
                )["access_token"]
                resident_headers = {"Authorization": "Bearer " + resident}
                assert (
                    await http.get(
                        f"/v1/houses/fixture_house/commands/{off['command_id']}",
                        headers=resident_headers,
                    )
                ).status_code == 200
                assert (
                    await http.get(
                        "/events?house_id=fixture_house", headers=resident_headers
                    )
                ).status_code == 403
                foreign = (
                    await api.request(
                        "POST",
                        "/auth/login",
                        {"email": "foreign@example.test", "password": seed["password"]},
                    )
                )["access_token"]
                foreign_headers = {"Authorization": "Bearer " + foreign}
                assert (
                    await http.get(
                        f"/v1/houses/fixture_house/commands/{off['command_id']}",
                        headers=foreign_headers,
                    )
                ).status_code == 404
                assert (
                    await http.post(
                        "/v1/devices/fixture_relay/commands",
                        json={"on": True},
                        headers={**foreign_headers, "Idempotency-Key": str(uuid4())},
                    )
                ).status_code == 404
                assert (
                    await http.get(
                        f"/v1/houses/fixture_house/commands/{off['command_id']}"
                    )
                ).status_code == 401

                # Deliberately withhold handling/ACK, then restart only the owned stack.
                pending = await submit(True)
                saved = await receive(pending, apply=False)
                assert (await read(pending))["status"] == "dispatched"
                await asyncio.to_thread(
                    command, [*dc, "restart", "postgres"], timeout=90
                )
                await asyncio.to_thread(command, [*dc, "restart", "core"], timeout=90)
                # Docker can allocate a different host port for a target published as 0.
                port = (
                    await asyncio.to_thread(command, [*dc, "port", "core", "8000"])
                ).rsplit(":", 1)[1]
                api.base = f"http://127.0.0.1:{port}"
                http.base_url = api.base
                await ready(api)
                restored = await read(pending)
                assert restored["command_id"] == pending["command_id"]
                assert restored["status"] in ("dispatched", "timed_out")
                assert (
                    restored["ack_status"] is None
                )  # Retained old OFF must not confirm.
                with pytest.raises(TimeoutError):
                    await asyncio.wait_for(
                        messages.__anext__(), 0.3
                    )  # No automatic resend.
                timed = await wait_for(
                    lambda: read(pending),
                    lambda r: r["status"] == "timed_out",
                    seconds=40,
                )
                assert timed["failure_code"] == "deadline_exceeded"
                await relay.handle(str(saved.topic), bytes(saved.payload), saved.retain)
                await wait_for(device, lambda d: d["state"].get("on") is True)
                assert (
                    await read(pending)
                ) == timed  # Late physical report != command success.
                final = await submit(False)
                await receive(final)

                # Independent reads of this fixture's PostgreSQL; no installed DB access.
                sql = "SELECT status, count(*) FROM device_commands GROUP BY status ORDER BY status"
                rows = await asyncio.to_thread(
                    command,
                    [
                        *dc,
                        "exec",
                        "-T",
                        "postgres",
                        "psql",
                        "-U",
                        "postgres",
                        "-d",
                        "kzhome",
                        "-At",
                        "-c",
                        sql,
                    ],
                )
                assert rows.splitlines() == ["applied|4", "timed_out|1"]
                sql = "SELECT count(*) FROM device_commands WHERE status='applied' AND (ack_status IS DISTINCT FROM 'applied' OR state_received_at IS NULL OR reported_on IS DISTINCT FROM requested_on)"
                assert (
                    await asyncio.to_thread(
                        command,
                        [
                            *dc,
                            "exec",
                            "-T",
                            "postgres",
                            "psql",
                            "-U",
                            "postgres",
                            "-d",
                            "kzhome",
                            "-At",
                            "-c",
                            sql,
                        ],
                    )
                    == "0"
                )
                assert (await device())["state"]["on"] is False

    asyncio.run(check())
