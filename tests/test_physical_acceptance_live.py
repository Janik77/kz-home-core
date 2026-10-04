"""Opt-in full production-component acceptance in an exclusively owned test stack."""

import asyncio
import json
import os
from pathlib import Path
import secrets
import subprocess
import time
from uuid import uuid4

import pytest

from deploy.commission_device import prepare_acl
from simulator.commissioning_probe import probe
from simulator.e2e_acceptance import API, Failure
from simulator.mqtt_relay import Config
from simulator.physical_acceptance import run, secret_free
from test_physical_acceptance import provision

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_PHYSICAL_E2E_DOCKER_TESTS") != "1",
    reason="explicit RUN_PHYSICAL_E2E_DOCKER_TESTS=1 required; isolated stack only",
)


def command(args, *, input_text=None, env=None, timeout=60):
    result = subprocess.run(
        args,
        input=input_text,
        env=env,
        text=True,
        capture_output=True,
        timeout=timeout,
        cwd=ROOT,
    )
    if result.returncode:
        pytest.fail("Isolated physical acceptance command failed (details withheld)")
    return result.stdout.strip()


def isolate_configuration(config, root, project, image):
    """Use effective production Compose, replace only owned fixture inputs/topology."""
    config["name"] = project
    for kind in ("networks", "volumes"):
        for name, value in config[kind].items():
            value["name"] = project + "_" + name
            value["labels"] = {"kzhome.acceptance": project}
    for name, service in config["services"].items():
        service["restart"] = "no"
        service["labels"] = {"kzhome.acceptance": project}
        for mount in service.get("volumes", []):
            target = mount["target"]
            if target in (
                "/run/mqtt/ca.crt",
                "/bootstrap/ca.crt",
                "/bootstrap/server.crt",
                "/bootstrap/server.key",
                "/bootstrap/passwords",
                "/bootstrap/acl",
            ):
                mount["source"] = str(root / target.rsplit("/", 1)[1])
                assert mount["read_only"] is True
        if name == "core":
            service.pop("build", None)
            service["image"] = image
            service["ports"] = [
                {
                    "target": 8000,
                    "published": "0",
                    "host_ip": "127.0.0.1",
                    "protocol": "tcp",
                }
            ]
            service["volumes"].append(
                {
                    "type": "bind",
                    "source": str(ROOT / "tests/physical_fixture.py"),
                    "target": "/app/acceptance_fixture.py",
                    "read_only": True,
                    "bind": {"create_host_path": False},
                }
            )
        if name == "mosquitto":
            # Host-side acceptance connections only, never the production LAN override.
            service["ports"] = [
                {
                    "target": 8883,
                    "published": "0",
                    "host_ip": "127.0.0.1",
                    "protocol": "tcp",
                }
            ]
            service["networks"]["frontend"] = None
    assert not config["services"]["postgres"].get("ports")
    assert config["networks"]["backend"]["internal"] is True
    return config


def cleanup_fixture(dc, image, root, prepared):
    failed = False
    try:
        if prepared:
            failed = (
                subprocess.run(
                    [*dc, "down", "--volumes", "--remove-orphans"],
                    capture_output=True,
                    timeout=90,
                ).returncode
                != 0
            )
    except (OSError, subprocess.SubprocessError):
        failed = True
    finally:
        try:
            subprocess.run(
                ["docker", "image", "rm", image], capture_output=True, timeout=30
            )
        except (OSError, subprocess.SubprocessError):
            failed = True
        finally:
            # Remove private host inputs even when Docker cleanup fails/times out.
            for name in (
                "server.key",
                "server.crt",
                "ca.crt",
                "passwords",
                "stack.json",
            ):
                (root / name).unlink(missing_ok=True)
    if failed:
        pytest.fail(
            "Owned acceptance stack cleanup failed; inspect labeled test resources"
        )


@pytest.fixture
def stack(tmp_path):
    command(["docker", "version", "--format", "{{.Server.Version}}"])
    project = "kzhome-physical-test-" + uuid4().hex
    image = "kz-home-physical-test:" + uuid4().hex
    core_password, db_password = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    seed = {
        "password": secrets.token_urlsafe(32),
        "claim_code": secrets.token_urlsafe(32),
    }
    device_password = secrets.token_urlsafe(32)
    config = Config(
        "localhost",
        8883,
        "kzdevice-fixture_relay",
        device_password,
        str(tmp_path / "ca.crt"),
        "fixture_house",
        "fixture_relay",
        "fixture-" + uuid4().hex,
    )
    # Override every interpolated production variable. Never read .env/deploy/local.
    env = {
        **os.environ,
        "POSTGRES_PASSWORD": secrets.token_urlsafe(32),
        "DATABASE_URL": f"postgresql+psycopg://kzhome:{db_password}@postgres:5432/kzhome",
        "AUTH_JWT_SECRET": secrets.token_urlsafe(48),
        "MQTT_PASSWORD": core_password,
        "MQTT_CLIENT_ID": project + "-core",
        "MQTT_ACL_FILE": str(tmp_path / "acl"),
        "TZ": "UTC",
    }
    envfile = tmp_path / "empty.env"
    envfile.write_text("# Fixture values supplied explicitly by the test process\n")
    compose = tmp_path / "stack.json"
    dc = [
        "docker",
        "compose",
        "--env-file",
        str(envfile),
        "-p",
        project,
        "-f",
        str(compose),
    ]
    prepared = False
    try:
        effective = json.loads(
            command(
                [
                    "docker",
                    "compose",
                    "--env-file",
                    str(envfile),
                    "-p",
                    project,
                    "-f",
                    str(ROOT / "compose.production.yaml"),
                    "config",
                    "--format",
                    "json",
                ],
                env=env,
            )
        )
        effective = isolate_configuration(effective, tmp_path, project, image)
        compose.write_text(json.dumps(effective), encoding="utf-8")
        os.chmod(compose, 0o600)  # Contains only disposable fixture process secrets.
        prepared = True
        for runtime_image in (
            "postgres:17-bookworm",
            "eclipse-mosquitto:2",
            "alpine/openssl:latest",
            "python:3.12-slim-bookworm",
        ):
            command(
                ["docker", "image", "inspect", runtime_image]
            )  # Refuse implicit pulls.
        command(["docker", "build", "--pull=false", "-t", image, "."], timeout=300)
        prepare_acl(
            (ROOT / "deploy/mosquitto/acl").read_text(),
            tmp_path / "acl",
            config.house_id,
            config.device_id,
            "grant",
        )
        mount = f"type=bind,source={tmp_path},target=/fixture"
        command(
            [
                "docker",
                "run",
                "--pull=never",
                "--rm",
                "--mount",
                mount,
                "alpine/openssl:latest",
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-keyout",
                "/fixture/server.key",
                "-out",
                "/fixture/server.crt",
                "-subj",
                "/CN=mosquitto",
                "-addext",
                "subjectAltName=DNS:mosquitto,DNS:localhost",
                "-days",
                "1",
            ]
        )
        (tmp_path / "ca.crt").write_bytes((tmp_path / "server.crt").read_bytes())
        for username, password, options in (
            ("kzhome-core", core_password, ["-c"]),
            (config.username, device_password, []),
        ):
            command(
                [
                    "docker",
                    "run",
                    "--pull=never",
                    "--rm",
                    "-i",
                    "--user",
                    "0:0",
                    "--entrypoint",
                    "mosquitto_passwd",
                    "--mount",
                    mount,
                    "eclipse-mosquitto:2",
                    *options,
                    "/fixture/passwords",
                    username,
                ],
                input_text=password + "\n" + password + "\n",
            )
        command([*dc, "config", "--quiet"])
        command(
            [*dc, "up", "-d", "--pull", "never", "--wait", "postgres", "mosquitto"],
            timeout=120,
        )
        command(
            [
                *dc,
                "exec",
                "-T",
                "postgres",
                "psql",
                "-v",
                "ON_ERROR_STOP=1",
                "-U",
                "postgres",
                "-d",
                "kzhome",
            ],
            input_text=f"CREATE ROLE kzhome LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD '{db_password}';\nALTER DATABASE kzhome OWNER TO kzhome;\n",
        )
        # Explicit migration of the new owned test volume. No startup hooks/seed.
        command(
            [
                *dc,
                "run",
                "--rm",
                "--no-deps",
                "-T",
                "core",
                "python",
                "-m",
                "alembic",
                "upgrade",
                "head",
            ]
        )
        ids = json.loads(
            command(
                [
                    *dc,
                    "run",
                    "--rm",
                    "--no-deps",
                    "-T",
                    "core",
                    "python",
                    "-c",
                    "import sys,json; from app.core import Settings; from app.db import create_db_engine; from acceptance_fixture import initialize_inventory; e=create_db_engine(Settings.from_env()); print(json.dumps(initialize_inventory(e,json.load(sys.stdin)))); e.dispose()",
                ],
                input_text=json.dumps(seed),
            )
        )
        command([*dc, "up", "-d", "--no-deps", "--pull", "never", "core"])
        api_port = int(command([*dc, "port", "core", "8000"]).rsplit(":", 1)[1])
        config = config.__class__(
            **{
                **config.__dict__,
                "port": int(
                    command([*dc, "port", "mosquitto", "8883"]).rsplit(":", 1)[1]
                ),
            }
        )
        api = API(f"http://127.0.0.1:{api_port}")
        yield dc, config, core_password, seed, ids, api
    finally:
        # The random project and all explicit resource names belong only to this test.
        # Do not use the installed kzhome project, deploy/local, or its volumes.
        cleanup_fixture(dc, image, tmp_path, prepared)


async def ready(api):
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        try:
            if await api.request("GET", "/ready") == {"status": "ready"}:
                return
        except Failure:
            pass
        await asyncio.sleep(0.2)
    pytest.fail("Isolated Core did not become ready")


def test_full_commissioned_identity_api_postgresql_tls_round_trip_and_restart(stack):
    dc, config, observer, seed, ids, api = stack
    if os.name == "nt":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    async def check():
        await ready(api)
        await provision(api, ids, seed)
        # Same v0.11 negative broker probe; no anonymous/reverse/cross-device access.
        await asyncio.to_thread(probe, config, observer)
        owner_token = api.token
        api.token = (
            await api.request(
                "POST",
                "/auth/login",
                {
                    "email": "foreign@example.test",
                    "password": seed["password"],
                },
            )
        )["access_token"]
        await api.request("POST", "/houses", {"id": "foreign_house", "name": "Foreign"})
        for method, path in (
            ("GET", "/devices/fixture_relay"),
            ("POST", "/devices/fixture_relay/on"),
            ("GET", "/events?house_id=fixture_house"),
            ("GET", "/houses/fixture_house/physical-devices/fixture_relay"),
            ("GET", "/houses/foreign_house/physical-devices/fixture_relay"),
        ):
            await api.request(method, path, expected_status=404)
        assert not await api.request("GET", "/devices")
        await api.request(
            "POST",
            "/houses/foreign_house/physical-devices/fixture_relay/activate",
            {"broker_access_confirmed": True},
            expected_status=404,
        )
        api.token = (
            await api.request(
                "POST",
                "/auth/login",
                {
                    "email": "resident@example.test",
                    "password": seed["password"],
                },
            )
        )["access_token"]
        await api.request(
            "POST",
            "/houses/fixture_house/physical-devices/fixture_relay/activate",
            {"broker_access_confirmed": True},
            expected_status=403,
        )
        api.token = owner_token
        commands = []
        for _ in range(2):
            commands += await run(
                config, api, "owner@example.test", seed["password"], 30
            )
        rows = await api.request("GET", "/events?house_id=fixture_house&limit=500")
        event_ids = {r["id"] for r in rows}
        assert any(r["event_type"] == "authorization_denied" for r in rows)
        assert sum(r["event_type"] == "device_onboarding_changed" for r in rows) == 2
        # Independent PostgreSQL connection, not API/ORM memory, proves exact evidence.
        stored = json.loads(
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
                    "-tAc",
                    "SELECT json_build_object('state',(SELECT state FROM devices WHERE id='fixture_relay'), 'online',(SELECT online FROM devices WHERE id='fixture_relay'), 'binding',(SELECT json_build_object('house_id',house_id,'status',status,'claim_code_hash',claim_code_hash) FROM physical_devices WHERE device_id='fixture_relay'), 'events',(SELECT json_agg(row_to_json(e)) FROM event_logs e WHERE house_id='fixture_house'), 'role_superuser',(SELECT rolsuper FROM pg_roles WHERE rolname='kzhome'), 'head',(SELECT version_num FROM alembic_version))",
                ],
            )
        )
        assert stored["head"] == "0005_device_onboarding"
        assert stored["role_superuser"] is False
        assert stored["state"] == {"on": False} and stored["online"] is False
        assert stored["binding"] == {
            "house_id": "fixture_house",
            "status": "active",
            "claim_code_hash": None,
        }
        assert event_ids <= {r["id"] for r in stored["events"]}
        for cmd in commands:
            relevant = [
                r
                for r in stored["events"]
                if r["correlation_id"] == cmd.correlation_id
                and r["entity_id"] == config.device_id
            ]
            assert any(
                r["event_type"] == "device_ack_received"
                and r["payload"].get("command_id") == cmd.command_id
                and r["payload"].get("status") == "applied"
                for r in relevant
            )
            assert (
                sum(
                    r["event_type"] == "device_state_changed"
                    and r["payload"].get("state") == cmd.state
                    for r in relevant
                )
                == 1
            )
        secret_free(
            stored["events"],
            (
                seed["password"],
                seed["claim_code"],
                config.password,
                observer,
                api.token,
            ),
        )
        await asyncio.to_thread(command, [*dc, "restart", "postgres"])
        await asyncio.to_thread(
            command,
            [
                *dc,
                "up",
                "-d",
                "--no-deps",
                "--force-recreate",
                "--pull",
                "never",
                "core",
            ],
        )
        # A newly created Core process and restarted PostgreSQL retain all evidence.
        api_port = int(
            (await asyncio.to_thread(command, [*dc, "port", "core", "8000"])).rsplit(
                ":", 1
            )[1]
        )
        api.base = f"http://127.0.0.1:{api_port}"
        await ready(api)
        assert (await api.request("GET", "/devices/fixture_relay"))["state"] == {
            "on": False
        }
        assert event_ids <= {
            r["id"]
            for r in await api.request(
                "GET", "/events?house_id=fixture_house&limit=500"
            )
        }

    asyncio.run(check())
