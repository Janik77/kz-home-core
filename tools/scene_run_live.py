"""Real PostgreSQL/TLS and a separately restarted Core process; software relay only."""

import argparse
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import secrets
import socket
import ssl
import subprocess
import sys
from uuid import uuid4

from run_software_stack import CORE, validate_web


def serve(path, port):
    import uvicorn
    from app.core import Settings
    from app.main import create_app
    from app.transports.mqtt_client import AiomqttClient

    values = json.loads(path.read_text())
    ca = values.pop("ca_file")
    values["mqtt_software_devices"] = tuple(
        tuple(pair) for pair in values["mqtt_software_devices"]
    )
    settings = Settings(**values)
    wire = AiomqttClient(
        host=settings.mqtt_host,
        port=settings.mqtt_port,
        username=settings.mqtt_username,
        password=settings.mqtt_password,
        tls_enabled=True,
        keepalive=15,
        client_id=settings.mqtt_client_id,
    )
    wire._options["tls_context"] = ssl.create_default_context(cafile=ca)
    uvicorn.run(
        create_app(settings, run_simulator=False, mqtt_client=wire),
        host="127.0.0.1",
        port=port,
        access_log=False,
        log_level="warning",
    )


async def run():
    import aiomqtt
    import httpx
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import Session
    from confirmed_live_stack import Services, provision
    from app.repositories import UserRepository
    from app.services.auth_service import UserService
    from simulator.e2e_acceptance import API
    from simulator.mqtt_relay import Relay
    from app.transports.mqtt_models import CommandEnvelope
    from app.transports.mqtt_topics import TopicKind

    services = Services()
    process = engine = None
    configuration = services.directory / "scene-core.json"
    try:
        services.start()
        engine = create_engine(services.url)
        migration = Config(str(CORE / "alembic.ini"))
        migration.set_main_option("script_location", str(CORE / "alembic"))
        with engine.begin() as connection:
            migration.attributes["connection"] = connection
            command.upgrade(migration, "head")
        seed = {"password": secrets.token_urlsafe(32)}
        ids = {}
        with Session(engine) as session:
            for role in ("owner", "resident", "foreign"):
                ids[role] = (
                    UserService(UserRepository(session))
                    .create(f"{role}@example.test", seed["password"])
                    .id
                )
        jwt_secret = secrets.token_urlsafe(48)
        with configuration.open(
            "x", opener=lambda path, flags: os.open(path, flags, 0o600)
        ) as file:
            json.dump(
                dict(
                    database_url=services.url,
                    app_env="production",
                    auth_jwt_secret=jwt_secret,
                    mqtt_enabled=True,
                    mqtt_host="localhost",
                    mqtt_port=services.mqtt_port,
                    mqtt_tls_enabled=True,
                    mqtt_username="core_lab",
                    mqtt_password=services.password,
                    mqtt_client_id=services.name + "-scene-core",
                    mqtt_software_devices=[["fixture_house", "fixture_relay"]],
                    ca_file=str(services.directory / "ca.crt"),
                ),
                file,
            )
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        api = API(f"http://127.0.0.1:{port}")

        async def start():
            nonlocal process
            env = {
                key: os.environ[key]
                for key in ("PATH", "HOME", "TMPDIR")
                if key in os.environ
            }
            env.update(
                APP_ENV="production",
                DATABASE_URL=services.url,
                AUTH_JWT_SECRET=jwt_secret,
                MQTT_ENABLED="true",
                MQTT_TLS_ENABLED="true",
                MQTT_HOST="localhost",
                MQTT_PORT=str(services.mqtt_port),
                MQTT_CLIENT_ID="unused-scene-import",
                KZHOME_SIMULATOR_ENABLED="false",
            )
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-B",
                    __file__,
                    "--serve",
                    str(configuration),
                    "--port",
                    str(port),
                ],
                cwd=CORE,
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            async with httpx.AsyncClient() as client:
                async with asyncio.timeout(30):
                    while True:
                        if process.poll() is not None:
                            raise RuntimeError(
                                "Owned Core process exited before readiness"
                            )
                        try:
                            response = await client.get(api.base + "/ready")
                            if response.status_code == 200:
                                return
                        except httpx.ConnectError:
                            pass
                        await asyncio.sleep(0.1)

        async def stop():
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    await asyncio.to_thread(process.wait, timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    await asyncio.to_thread(process.wait, timeout=5)

        await start()
        await provision(api, ids, seed)
        async with aiomqtt.Client(
            hostname="localhost",
            port=services.mqtt_port,
            username="software_lab",
            password=services.password,
            identifier=services.name + "-scene-relay",
            tls_context=ssl.create_default_context(
                cafile=str(services.directory / "ca.crt")
            ),
        ) as mqtt:
            relay = Relay("fixture_house", "fixture_relay", mqtt.publish)
            await mqtt.subscribe(relay.topic(TopicKind.SET), qos=1)
            await relay.send(TopicKind.STATUS, relay.status(True))
            await relay.state("scene-baseline")
            async with asyncio.timeout(10):
                while not (await api.request("GET", "/devices/fixture_relay"))[
                    "online"
                ]:
                    await asyncio.sleep(0.1)
            await api.request(
                "POST",
                "/scenes",
                {
                    "id": "durable_scene",
                    "name": "Software ON OFF",
                    "house_id": "fixture_house",
                    "actions": [
                        {"device_id": "fixture_relay", "state": {"on": on}}
                        for on in (True, False)
                    ],
                },
            )
            base = "/v1/houses/fixture_house/scene-runs"
            async with httpx.AsyncClient(
                base_url=api.base,
                headers={"Authorization": "Bearer " + api.token},
                timeout=15,
            ) as http:
                key = str(uuid4())
                response = await http.post(
                    base,
                    json={"scene_id": "durable_scene", "expected_revision": 1},
                    headers={"Idempotency-Key": key},
                )
                assert response.status_code == 201
                run_id = response.json()["id"]
                path = f"{base}/{run_id}"

                async def read():
                    response = await http.get(path)
                    assert response.status_code == 200
                    return response.json()

                async def complete(index):
                    # Duplicate dispatch requests race over a real PostgreSQL CAS.
                    responses = await asyncio.gather(
                        *[
                            http.post(f"{path}/actions/{index}/dispatch")
                            for _ in range(2)
                        ]
                    )
                    assert all(r.status_code == 202 for r in responses)
                    result = await read()
                    receipt = result["actions"][index]["receipt"]
                    assert (
                        result["status"] == "running"
                        and receipt["status"] == "dispatched"
                    )
                    message = await asyncio.wait_for(mqtt.messages.__anext__(), 10)
                    envelope = CommandEnvelope.model_validate_json(
                        bytes(message.payload)
                    )
                    assert (
                        envelope.command_id == receipt["command_id"]
                        and envelope.correlation_id == receipt["correlation_id"]
                    )
                    assert envelope.state == {"on": index == 0} and not message.retain
                    await relay.handle(
                        str(message.topic), bytes(message.payload), message.retain
                    )
                    async with asyncio.timeout(10):
                        while (await read())["actions"][index]["status"] != "applied":
                            await asyncio.sleep(0.1)
                    try:
                        await asyncio.wait_for(mqtt.messages.__anext__(), 0.3)
                    except TimeoutError:
                        pass
                    else:
                        raise AssertionError("Duplicate scene MQTT command")

                await complete(0)
                await stop()
                old_pid = process.pid
                await start()
                assert process.pid != old_pid
                restored = await read()
                assert [a["status"] for a in restored["actions"]] == [
                    "applied",
                    "ready",
                ]
                replay = await http.post(
                    base,
                    json={"scene_id": "durable_scene", "expected_revision": 1},
                    headers={"Idempotency-Key": key},
                )
                assert replay.json()["id"] == run_id
                await complete(1)
                result = await read()
                assert result["status"] == "succeeded"
                assert all(
                    a["receipt"]["ack_status"] == "applied"
                    and a["receipt"]["reported_on"] == a["requested_on"]
                    for a in result["actions"]
                )
                with engine.begin() as connection:
                    assert (
                        connection.scalar(text("SELECT count(*) FROM physical_devices"))
                        == 0
                    )
                    rows = connection.execute(
                        text(
                            "SELECT r.position, c.status, c.requested_on, c.reported_on FROM scene_run_receipts r JOIN device_commands c ON c.command_id=r.command_id ORDER BY r.position"
                        )
                    ).all()
                    assert rows == [
                        (0, "applied", True, True),
                        (1, "applied", False, False),
                    ]
                print(
                    "PASS scene lifecycle: PostgreSQL CAS, TLS SET/ACK/state, atomic receipt links, real Core process restart, ON/OFF and zero physical devices",
                    flush=True,
                )
        await stop()
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                await asyncio.to_thread(process.wait, timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                await asyncio.to_thread(process.wait, timeout=5)
        if engine is not None:
            engine.dispose()
        configuration.unlink(missing_ok=True)
        services.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--web-root", type=Path)
    parser.add_argument("--serve", type=Path)
    parser.add_argument("--port", type=int)
    args = parser.parse_args()
    sys.path.insert(0, str(CORE))
    if args.serve:
        serve(args.serve, args.port)
        return
    web = validate_web(args.web_root)
    sys.path.insert(0, str(web / "e2e"))
    spec = importlib.util.spec_from_file_location(
        "owned_web_runner", web / "e2e/confirmed-live-runner.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    before = module.inventory()
    try:
        asyncio.run(run())
    finally:
        assert module.inventory() == before, (
            "Docker inventory changed; do not remove unrelated resources"
        )
        print(
            "PASS scene fixture cleanup and Docker inventory preservation", flush=True
        )


if __name__ == "__main__":
    main()
