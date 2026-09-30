"""Offline acceptance tests using real API/Gateway/AutomationService and SQLite."""

import asyncio
import copy
import json
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import create_engine

from app.bootstrap_e2e import bootstrap
from app.core import Settings
from app.db import Base
from app.transports.mqtt_client import FakeMQTTClient
from app.transports.mqtt_topics import TopicKind
from simulator.e2e_acceptance import Failure
from simulator.e2e_automation import (
    MOTION,
    RULE,
    RULE_ID,
    automation_evidence,
    provision,
    scenario,
)
from simulator.mqtt_motion import Motion, MotionConfig
from simulator.mqtt_relay import Relay


class Wire(FakeMQTTClient):
    """Replace only MQTT network I/O; Core still decodes every v1 envelope."""

    def __init__(self):
        super().__init__()
        self.commands = asyncio.Queue()

    async def publish(self, topic, payload, *, qos, retain):
        await super().publish(topic, payload, qos=qos, retain=retain)
        await self.commands.put(
            SimpleNamespace(topic=topic, payload=payload.encode(), retain=retain)
        )


class RelayClient:
    def __init__(self, wire):
        self.wire = wire
        self.messages = self

    async def subscribe(self, topic, qos):
        assert topic == "kzhome/v1/e2e_house/e2e_relay/set" and qos == 1

    async def __anext__(self):
        return await self.wire.commands.get()


class LocalAPI:
    def __init__(self, client):
        self.client = client
        self.calls = []
        self.omit = None

    async def request(self, method, path, body=None):
        self.calls.append((method, path))
        response = await self.client.request(method, path, json=body)
        if not response.is_success:
            raise Failure(f"HTTP request failed (status {response.status_code})")
        data = response.json()
        if path.startswith("/events") and self.omit:
            data = [e for e in data if e["event_type"] != self.omit]
        return data


@asynccontextmanager
async def stack(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    from app.main import create_app

    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'automation.db'}",
        app_env="test",
        mqtt_enabled=True,
        mqtt_host="unused",
        mqtt_client_id="offline-core",
    )
    engine = create_engine(settings.database_url)
    Base.metadata.create_all(engine)
    bootstrap(engine, "e2e@example.test", "test-only-password")
    engine.dispose()
    wire = Wire()
    app = create_app(settings, run_simulator=False, mqtt_client=wire)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            api = LocalAPI(client)
            tokens = await api.request(
                "POST",
                "/auth/login",
                {"email": "e2e@example.test", "password": "test-only-password"},
            )
            client.headers["Authorization"] = "Bearer " + tokens["access_token"]

            async def publish(topic, payload, **policy):
                await wire.inject(topic, payload)

            yield (
                api,
                wire,
                RelayClient(wire),
                Relay("e2e_house", "e2e_relay", publish),
                Motion(publish),
            )


def test_real_automation_round_trip_and_rerun(tmp_path, monkeypatch):
    async def check():
        async with stack(tmp_path, monkeypatch) as (api, wire, client, relay, motion):
            correlations = []
            for _ in range(2):
                await provision(api)
                await scenario(api, client, relay, motion, 3)
                await api.request("POST", RULE + "/disable")
                assert (await api.request("GET", MOTION))["state"] == {"motion": True}
                assert (await api.request("GET", "/devices/e2e_relay"))["state"] == {
                    "on": True
                }
                correlations.append(json.loads(wire.published[-1][1])["correlation_id"])
            assert len(set(correlations)) == 2
            assert len(wire.published) == 2
            assert len(await api.request("GET", "/automations")) == 1
            assert len(await api.request("GET", "/devices?house_id=e2e_house")) == 2
            # No manual run, direct state mutation, or relay HTTP command can fake success.
            assert not any(
                method == "PATCH" or path.endswith(("/run", "/on", "/off", "/state"))
                for method, path in api.calls
            )

    asyncio.run(check())


@pytest.mark.parametrize(
    "path,patch",
    [
        (MOTION, {"capabilities": ["temperature"]}),
        (
            RULE,
            {
                "actions": [
                    {
                        "type": "device_state",
                        "device_id": "e2e_relay",
                        "state": {"on": False},
                    }
                ]
            },
        ),
    ],
)
def test_conflicting_provisioning_is_not_overwritten(
    tmp_path, monkeypatch, path, patch
):
    async def check():
        async with stack(tmp_path, monkeypatch) as (api, *_):
            await provision(api)
            await api.request("PATCH", path, patch)
            with pytest.raises(Failure, match="incompatible"):
                await provision(api)
            current = await api.request("GET", path)
            assert all(current[key] == value for key, value in patch.items())

    asyncio.run(check())


@pytest.mark.parametrize(
    "missing",
    [
        "automation_triggered",
        "automation_completed",
        "device_ack_received",
        "device_state_changed",
    ],
)
def test_missing_persisted_evidence_fails_bounded(tmp_path, monkeypatch, missing):
    async def check():
        async with stack(tmp_path, monkeypatch) as (api, _, client, relay, motion):
            await provision(api)
            api.omit = missing
            with pytest.raises(Failure, match="Timeout.*checks="):
                await scenario(api, client, relay, motion, 0.5)

    asyncio.run(check())


def test_unrelated_stale_and_failure_automation_events():
    rows = [
        {
            "id": "event",
            "house_id": "e2e_house",
            "entity_id": RULE_ID,
            "correlation_id": "corr",
            "event_type": "automation_triggered",
            "payload": {},
        }
    ]
    assert automation_evidence(rows, "corr", set())["triggered"]
    assert not automation_evidence(rows, "other", set())["triggered"]
    assert not automation_evidence(rows, "corr", {"event"})["triggered"]
    for key in ("house_id", "entity_id"):
        modified = copy.deepcopy(rows)
        modified[0][key] = "foreign"
        assert not automation_evidence(modified, "corr", set())["triggered"]
    rows[0]["event_type"] = "automation_failed"
    rows[0]["payload"] = {"error": "secret-password"}
    with pytest.raises(
        Failure, match="^Core persisted a correlated automation failure$"
    ):
        automation_evidence(rows, "corr", set())


def test_motion_envelopes_and_publish_boundaries():
    async def check():
        publish = AsyncMock()
        motion = Motion(publish)
        for value in (False, True):
            await motion.state(value, "correlation")
            args = publish.call_args
            assert args.args[0] == "kzhome/v1/e2e_house/e2e_motion/state"
            envelope = json.loads(args.args[1])
            assert envelope["protocol_version"] == "v1"
            assert envelope["state"]["motion"] is value
            assert envelope["correlation_id"] == "correlation"
            assert args.kwargs == {"qos": 1, "retain": True}
        await motion.send_status(motion.status(False))
        assert json.loads(publish.call_args.args[1])["status"] == "offline"
        with pytest.raises(ValueError):
            await motion.state(1, "correlation")
        for kind in (TopicKind.SET, TopicKind.ACK, TopicKind.TELEMETRY):
            with pytest.raises(ValueError):
                motion.topic(kind)

    asyncio.run(check())


def test_motion_configuration_requires_separate_identity(monkeypatch):
    for key, value in {
        "MQTT_HOST": "mosquitto",
        "MQTT_USERNAME": "motion-simulator",
        "MQTT_PASSWORD": "hidden-unit-test-password",
        "CA_FILE": "/ca.crt",
        "MQTT_CLIENT_ID": "motion-test",
    }.items():
        monkeypatch.setenv("MOTION_" + key, value)
    monkeypatch.delenv("MOTION_MQTT_PORT", raising=False)
    assert MotionConfig.from_env().port == 8883
    assert "hidden-unit-test-password" not in repr(MotionConfig.from_env())
    for username in ("kzhome-core", "relay-simulator"):
        monkeypatch.setenv("MOTION_MQTT_USERNAME", username)
        with pytest.raises(ValueError):
            MotionConfig.from_env()


def test_all_mqtt_identities_have_only_required_directional_permissions():
    acl = (Path(__file__).parents[1] / "deploy/mosquitto/acl").read_text()
    identities = {}
    current = None
    for line in acl.splitlines():
        line = line.strip()
        if line.startswith("user "):
            current = line[5:]
            assert current not in identities
            identities[current] = []
        elif line.startswith("topic "):
            assert current is not None
            identities[current].append(line)
        elif line and not line.startswith("#"):
            pytest.fail("Unexpected MQTT ACL directive")
    assert set(identities) == {"kzhome-core", "relay-simulator", "motion-simulator"}
    assert identities["motion-simulator"] == [
        "topic write kzhome/v1/e2e_house/e2e_motion/state",
        "topic write kzhome/v1/e2e_house/e2e_motion/status",
    ]
    assert identities["kzhome-core"] == [
        "topic read kzhome/v1/+/+/state",
        "topic read kzhome/v1/+/+/ack",
        "topic read kzhome/v1/+/+/telemetry",
        "topic read kzhome/v1/+/+/status",
        "topic write kzhome/v1/+/+/set",
    ]
    assert identities["relay-simulator"] == [
        "topic read kzhome/v1/e2e_house/e2e_relay/set",
        "topic write kzhome/v1/e2e_house/e2e_relay/state",
        "topic write kzhome/v1/e2e_house/e2e_relay/ack",
        "topic write kzhome/v1/e2e_house/e2e_relay/status",
    ]


@pytest.mark.parametrize("bad_correlation", [False, True])
def test_missing_or_uncorrelated_mqtt_command_cannot_pass(
    tmp_path, monkeypatch, bad_correlation
):
    async def check():
        async with stack(tmp_path, monkeypatch) as (api, wire, client, relay, motion):
            await provision(api)
            original = wire.publish

            async def publish(topic, payload, **policy):
                if bad_correlation:
                    data = json.loads(payload)
                    data["correlation_id"] = "unrelated"
                    await original(topic, json.dumps(data), **policy)

            wire.publish = publish
            with pytest.raises(Failure, match="uncorrelated|Timeout during correlated"):
                await scenario(api, client, relay, motion, 0.5)

    asyncio.run(check())


@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_run_disables_rule_after_failure_without_echoing_errors(
    monkeypatch, cleanup_fails
):
    from simulator import e2e_automation as module

    client = SimpleNamespace(publish=AsyncMock())

    @asynccontextmanager
    async def broker(**kwargs):
        assert kwargs["timeout"] == 5
        yield client

    @asynccontextmanager
    async def motion_connection(config):
        yield Motion(AsyncMock())

    api = SimpleNamespace(
        request=AsyncMock(
            side_effect=[
                {"access_token": "unit-test-token"},
                RuntimeError("secret-password") if cleanup_fails else {},
            ]
        )
    )
    config = SimpleNamespace(
        host="unused",
        port=8883,
        username="relay-simulator",
        password="hidden",
        client_id="relay",
        ca_file="unused",
    )
    monkeypatch.setattr(module.aiomqtt, "Client", broker)
    monkeypatch.setattr(module.ssl, "create_default_context", lambda **kwargs: object())
    monkeypatch.setattr(module, "connection", motion_connection)
    monkeypatch.setattr(module, "provision", AsyncMock())
    monkeypatch.setattr(
        module, "scenario", AsyncMock(side_effect=Failure("scenario failed"))
    )
    with pytest.raises(Failure) as error:
        asyncio.run(module.run(config, None, api, "email", "password", 1))
    assert str(error.value).startswith(
        "Cleanup could not confirm" if cleanup_fails else "scenario failed"
    )
    assert "secret" not in str(error.value)
    assert api.request.call_args.args == ("POST", RULE + "/disable")
    assert json.loads(client.publish.call_args.args[1])["status"] == "offline"
