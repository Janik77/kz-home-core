"""Real API/services/gateway/persistence; only MQTT network is replaced offline."""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from alembic import command as migrations
from alembic.config import Config as AlembicConfig
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.core import Settings
from app.models import EventLogORM, PhysicalDeviceORM
from app.transports.mqtt_client import FakeMQTTClient
from physical_fixture import initialize_inventory
from simulator.e2e_acceptance import Failure, evidence
from simulator.mqtt_relay import Config, Relay
from simulator.physical_acceptance import paths, preflight, scenario, secret_free


class LocalAPI:
    def __init__(self, client):
        self.client, self.token, self.omit = client, None, None

    async def request(self, method, path, body=None, *, expected_status=None):
        headers = {"Authorization": "Bearer " + self.token} if self.token else {}
        response = await self.client.request(method, path, json=body, headers=headers)
        if expected_status is not None:
            if response.status_code != expected_status:
                raise Failure("Unexpected HTTP status")
            if not response.is_success:
                return None
        elif not response.is_success:
            raise Failure(f"HTTP request failed (status {response.status_code})")
        data = response.json()
        if path.startswith("/events") and self.omit:
            data = [r for r in data if r["event_type"] != self.omit]
        return data


async def provision(api, ids, seed):
    api.token = (
        await api.request(
            "POST",
            "/auth/login",
            {
                "email": "owner@example.test",
                "password": seed["password"],
            },
        )
    )["access_token"]
    await api.request("POST", "/houses", {"id": "fixture_house", "name": "Fixture"})
    await api.request(
        "POST",
        "/floors",
        {
            "id": "fixture_floor",
            "house_id": "fixture_house",
            "name": "Floor",
            "order": 0,
        },
    )
    await api.request(
        "POST",
        "/rooms",
        {
            "id": "fixture_room",
            "floor_id": "fixture_floor",
            "name": "Room",
        },
    )
    await api.request(
        "POST",
        "/houses/fixture_house/members",
        {
            "user_id": ids["resident"],
            "role": "resident",
        },
    )
    await api.request(
        "POST",
        "/houses/fixture_house/device-claims",
        {
            "hardware_id": "fixture-serial",
            "claim_code": seed["claim_code"],
            "room_id": "fixture_room",
            "name": "Physical relay",
        },
    )
    await api.request(
        "POST",
        "/houses/fixture_house/physical-devices/fixture_relay/activate",
        {
            "broker_access_confirmed": True,
        },
    )


class Wire(FakeMQTTClient):
    def __init__(self):
        super().__init__()
        self.queue = asyncio.Queue()
        self.messages = self
        self.subscribed = False
        self.drop = False

    async def subscribe(self, topic, qos):
        assert topic == "kzhome/v1/fixture_house/fixture_relay/set" and qos == 1
        self.subscribed = True

    async def unsubscribe(self, topic):
        self.subscribed = False

    async def __anext__(self):
        return await self.queue.get()

    async def publish(self, topic, payload, *, qos, retain):
        await super().publish(topic, payload, qos=qos, retain=retain)
        if self.subscribed and not self.drop:
            await self.queue.put(
                SimpleNamespace(topic=topic, payload=payload.encode(), retain=retain)
            )


@asynccontextmanager
async def stack(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    from app.main import create_app

    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'physical.db'}",
        app_env="test",
        mqtt_enabled=True,
        mqtt_host="unused",
        mqtt_client_id="offline-core",
    )
    engine = create_engine(settings.database_url)
    migration_config = AlembicConfig("alembic.ini")
    with engine.begin() as connection:
        migration_config.attributes["connection"] = connection
        migrations.upgrade(migration_config, "head")
    seed = {"password": "fixture-human-password", "claim_code": "A" * 43}
    ids = initialize_inventory(engine, seed)
    wire = Wire()
    # Gateway subscriptions are separate from the simulator's device subscription.
    core_wire = FakeMQTTClient()
    core_wire.publish = wire.publish
    app = create_app(settings, run_simulator=False, mqtt_client=core_wire)
    try:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                api = LocalAPI(client)
                await provision(api, ids, seed)

                async def publish(topic, payload, **kwargs):
                    await core_wire.inject(topic, payload)

                config = Config(
                    "unused",
                    8883,
                    "kzdevice-fixture_relay",
                    "fixture-mqtt-password",
                    "unused",
                    "fixture_house",
                    "fixture_relay",
                    "fixture-client",
                )
                yield (
                    api,
                    wire,
                    Relay(config.house_id, config.device_id, publish),
                    config,
                    engine,
                )
    finally:
        engine.dispose()


def test_full_real_core_round_trip_replay_offline_and_repeat(tmp_path, monkeypatch):
    async def check():
        async with stack(tmp_path, monkeypatch) as (api, wire, relay, config, engine):
            commands = []
            for _ in range(2):
                await preflight(
                    config, api, "owner@example.test", "fixture-human-password"
                )
                commands += await scenario(config, api, wire, relay, 3, quiet=0.05)
            assert len({c.command_id for c in commands}) == 4
            assert all(
                (qos, retained) == (1, False) for _, _, qos, retained in wire.published
            )
            with Session(engine) as session:
                assert (
                    session.get(PhysicalDeviceORM, config.device_id).status == "active"
                )
                rows = list(session.scalars(select(EventLogORM)))
                assert (
                    sum(r.event_type == "device_onboarding_changed" for r in rows) == 2
                )
                assert sum(r.event_type == "device_ack_received" for r in rows) == 6
                assert sum(r.event_type == "device_state_changed" for r in rows) == 5
                secret_free([r.payload for r in rows], (config.password, api.token))

    asyncio.run(check())


@pytest.mark.parametrize(
    "missing", ["device_ack_received", "device_state_changed", "receipt"]
)
def test_missing_evidence_cannot_pass(tmp_path, monkeypatch, missing):
    async def check():
        async with stack(tmp_path, monkeypatch) as (api, wire, relay, config, _):
            api.omit = missing
            wire.drop = missing == "receipt"
            with pytest.raises(Failure, match="Timeout"):
                await scenario(config, api, wire, relay, 0.5, quiet=0.01)

    asyncio.run(check())


@pytest.mark.parametrize(
    "key",
    [
        "password_hash",
        "mqtt_password",
        "claim_code_hash",
        "access_token",
        "hardware_id",
    ],
)
def test_private_fields_and_values_are_never_echoed(key):
    with pytest.raises(Failure) as error:
        secret_free({"nested": [{key: "unit-secret"}]})
    assert "unit-secret" not in str(error.value)
    with pytest.raises(Failure):
        secret_free({"metadata": {"accidental": "unit-secret"}}, ("unit-secret",))


@pytest.mark.parametrize(
    "changes",
    [
        {"username": "relay-simulator"},
        {"house_id": "bad/house"},
        {"device_id": "x" * 65},
    ],
)
def test_only_canonical_commissioned_identity_allowed(changes):
    config = Config(
        "unused",
        8883,
        "kzdevice-fixture_relay",
        "hidden",
        "unused",
        "fixture_house",
        "fixture_relay",
        "client",
    )
    with pytest.raises((Failure, ValueError)):
        paths(replace(config, **changes))


@pytest.mark.parametrize(
    "change", ["inactive", "wrong_house", "automation", "resident"]
)
def test_preflight_fails_before_mqtt_or_commands(change):
    from simulator.physical_acceptance import run

    config = Config(
        "unused", 8883, "kzdevice-relay", "hidden", "unused", "house", "relay", "client"
    )
    binding = {
        "device_id": "relay",
        "house_id": "house",
        "status": "active",
        "protocol_version": "v1",
        "hardware_model": "esp32-c6-relay-v1",
    }
    current = {"type": "relay", "capabilities": ["on_off"]}
    rules = []
    if change == "inactive":
        binding["status"] = "inactive"
    if change == "wrong_house":
        binding["house_id"] = "foreign"
    if change == "automation":
        rules = [{"enabled": True, "actions": [{"device_id": "relay"}]}]
    api = SimpleNamespace(
        token=None,
        request=AsyncMock(
            side_effect=[
                {"status": "ready"},
                {"access_token": "token"},
                binding,
                current,
                Failure("HTTP request failed (status 403)")
                if change == "resident"
                else [],
                rules,
            ]
        ),
    )
    with pytest.raises(Failure):
        asyncio.run(run(config, api, "email", "password", 1))
    assert not any(
        call.args[0] == "POST" and call.args[1].startswith("/devices")
        for call in api.request.call_args_list
    )


def test_physical_evidence_is_house_device_command_and_correlation_scoped():
    from app.transports.mqtt_models import CommandEnvelope, utc_now

    command = CommandEnvelope(
        command_id="cmd", correlation_id="corr", timestamp=utc_now(), state={"on": True}
    )
    rows = [
        {
            "id": "event",
            "house_id": "house",
            "entity_id": "relay",
            "event_type": "device_ack_received",
            "correlation_id": "corr",
            "payload": {"command_id": "cmd", "status": "applied"},
        }
    ]
    assert evidence(
        rows, command, True, set(), house_id="house", device_id="relay"
    ) == (True, False)
    assert evidence(
        rows, command, True, set(), house_id="foreign", device_id="relay"
    ) == (False, False)


def test_api_expected_status_rejects_success_and_wrong_denial(monkeypatch):
    from urllib.error import HTTPError
    from simulator.e2e_acceptance import API

    api = API("http://unused")
    api.opener.open = lambda *a, **kw: (_ for _ in ()).throw(
        HTTPError("unused", 403, "secret", {}, None)
    )
    assert (
        asyncio.run(api.request("POST", "/devices/relay/on", expected_status=403))
        is None
    )
    with pytest.raises(Failure, match="status 403"):
        asyncio.run(api.request("POST", "/devices/relay/on", expected_status=401))


def test_cli_requires_explicit_opt_in(monkeypatch):
    from simulator.physical_acceptance import main

    monkeypatch.setattr("sys.argv", ["physical_acceptance"])
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2


def test_expected_http_denial_cannot_accept_a_successful_response():
    from simulator.e2e_acceptance import API

    api = API("http://unused")

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    api.opener.open = lambda *a, **kw: Response()
    with pytest.raises(Failure, match="Unexpected HTTP status"):
        asyncio.run(api.request("POST", "/devices/relay/on", expected_status=401))


@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_run_cleanup_and_failures_do_not_echo_transport_secrets(
    monkeypatch, cleanup_fails
):
    from simulator import physical_acceptance as module

    client = SimpleNamespace(
        publish=AsyncMock(
            side_effect=RuntimeError("secret-password") if cleanup_fails else None,
        )
    )

    @asynccontextmanager
    async def broker(**kwargs):
        assert kwargs["timeout"] == 5
        assert kwargs["tls_context"] is not None
        yield client

    config = Config(
        "unused", 8883, "kzdevice-relay", "hidden", "unused", "house", "relay", "client"
    )
    monkeypatch.setattr(module, "preflight", AsyncMock())
    monkeypatch.setattr(
        module, "scenario", AsyncMock(side_effect=Failure("scenario failed"))
    )
    monkeypatch.setattr("simulator.physical_acceptance.aiomqtt.Client", broker)
    monkeypatch.setattr(module.ssl, "create_default_context", lambda **kwargs: object())
    with pytest.raises(Failure) as error:
        asyncio.run(module.run(config, None, "email", "password", 1))
    assert str(error.value).startswith(
        "Cleanup" if cleanup_fails else "scenario failed"
    )
    assert "secret-password" not in str(error.value)


def test_isolated_configuration_never_reuses_installation_inputs(tmp_path):
    from test_physical_acceptance_live import isolate_configuration

    inputs = ["ca.crt", "server.crt", "server.key", "passwords", "acl"]
    original = {
        "name": "kzhome",
        "networks": {
            "backend": {"name": "kzhome_backend", "internal": True},
            "frontend": {"name": "kzhome_frontend"},
        },
        "volumes": {"postgres_data": {"name": "kzhome_postgres_data"}},
        "services": {
            "core": {
                "build": {},
                "volumes": [
                    {
                        "source": "deploy/local/mqtt/ca.crt",
                        "target": "/run/mqtt/ca.crt",
                        "read_only": True,
                    }
                ],
            },
            "postgres": {
                "volumes": [
                    {
                        "source": "postgres_data",
                        "target": "/var/lib/postgresql/data",
                        "type": "volume",
                    }
                ]
            },
            "mosquitto": {
                "networks": {"backend": None},
                "volumes": [
                    {
                        "source": "deploy/local/mqtt/" + filename,
                        "target": "/bootstrap/" + filename,
                        "read_only": True,
                    }
                    for filename in inputs
                ],
            },
        },
    }
    result = isolate_configuration(original, tmp_path, "owned-fixture", "owned-image")
    assert result["name"] == "owned-fixture"
    assert all(
        value["name"].startswith("owned-fixture_")
        for kind in ("networks", "volumes")
        for value in result[kind].values()
    )
    assert result["services"]["core"]["image"] == "owned-image"
    for name in ("core", "mosquitto"):
        assert result["services"][name]["ports"][0]["host_ip"] == "127.0.0.1"
        for mount in result["services"][name]["volumes"]:
            assert "deploy/local" not in mount["source"]
            assert mount["read_only"] is True
    assert not result["services"]["postgres"].get("ports")
    assert result["networks"]["backend"]["internal"] is True


def test_docker_cleanup_failure_still_removes_only_owned_secret_inputs(
    tmp_path, monkeypatch
):
    import subprocess
    from test_physical_acceptance_live import cleanup_fixture

    private = ("server.key", "server.crt", "ca.crt", "passwords", "stack.json")
    for name in private:
        (tmp_path / name).write_text("fixture-secret")
    other = tmp_path / "unrelated.txt"
    other.write_text("preserve")

    def unavailable(*args, **kwargs):
        raise subprocess.TimeoutExpired("docker", 1)

    monkeypatch.setattr(subprocess, "run", unavailable)
    with pytest.raises(pytest.fail.Exception, match="cleanup failed"):
        cleanup_fixture(
            ["docker", "compose", "-p", "owned-fixture"], "owned-image", tmp_path, True
        )
    assert all(not (tmp_path / name).exists() for name in private)
    assert other.read_text() == "preserve"
