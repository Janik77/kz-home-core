"""Explicit isolated Docker validation; never uses the installed stack or secrets."""

import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path
import secrets
import socket
import ssl
import subprocess
import time
from uuid import uuid4

import aiomqtt
import pytest

from deploy.commission_device import prepare_acl
from simulator.commissioning_probe import Failure, connection, probe, require_denied
from simulator.mqtt_relay import Config, Relay
from app.transports.mqtt_models import CommandEnvelope, utc_now
from app.transports.mqtt_topics import TopicKind

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_COMMISSIONING_DOCKER_TESTS") != "1",
    reason="explicit RUN_COMMISSIONING_DOCKER_TESTS=1 required; isolated Docker only",
)
ROOT = Path(__file__).resolve().parents[1]


def docker(*args, input_text=None):
    if args and args[0] == "run":
        args = ("run", "--pull=never", *args[1:])
    result = subprocess.run(
        ["docker", *args],
        input=input_text,
        text=True,
        capture_output=True,
        timeout=60,
    )
    if result.returncode:
        # No raw command, input, environment or external error in diagnostics.
        pytest.fail("Isolated Docker validation command failed")
    return result.stdout.strip()


def test_compose_effective_configuration_and_explicit_lan(tmp_path, monkeypatch):
    envfile = tmp_path / "compose.env"
    envfile.write_text("# All values below are injected as dummy process settings\n")
    for key, value in {
        "DATABASE_URL": "postgresql+psycopg://test:test@postgres/kzhome",
        "POSTGRES_PASSWORD": "test-only",
        "AUTH_JWT_SECRET": "test-only-not-deployable",
        "MQTT_PASSWORD": "test-only",
        "MQTT_ACL_FILE": "./deploy/local/mqtt/acl",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("MQTT_LAN_BIND_IP", raising=False)
    command = [
        "compose",
        "--env-file",
        str(envfile),
        "-f",
        str(ROOT / "compose.production.yaml"),
    ]
    base = json.loads(docker(*command, "config", "--format", "json"))
    assert not base["services"]["postgres"].get("ports")
    assert not base["services"]["mosquitto"].get("ports")
    assert base["services"]["core"]["ports"][0]["host_ip"] == "127.0.0.1"
    assert set(base["services"]["mosquitto"]["networks"]) == {"backend"}
    assert base["networks"]["backend"]["internal"] is True
    inputs = base["services"]["mosquitto"]["volumes"]
    acl = next(mount for mount in inputs if mount["target"] == "/bootstrap/acl")
    assert acl["source"].replace("\\", "/").endswith("/deploy/local/mqtt/acl")
    assert acl["read_only"] is True and acl["bind"]["create_host_path"] is False
    combined = [*command, "-f", str(ROOT / "compose.mqtt-lan.yaml")]
    missing = subprocess.run(
        ["docker", *combined, "config", "--quiet"],
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert missing.returncode != 0
    monkeypatch.setenv("MQTT_LAN_BIND_IP", "192.168.50.10")
    lan = json.loads(docker(*combined, "config", "--format", "json"))
    assert not lan["services"]["postgres"].get("ports")
    assert lan["services"]["core"] == base["services"]["core"]
    assert lan["services"]["mosquitto"]["ports"] == [
        {
            "mode": "ingress",
            "host_ip": "192.168.50.10",
            "target": 8883,
            "published": "8883",
            "protocol": "tcp",
        }
    ]
    assert set(lan["services"]["mosquitto"]["networks"]) == {"backend", "mqtt_lan"}


@pytest.fixture
def broker(tmp_path):
    # Only throwaway test material. No production CA, passwords, env or volumes.
    name = "kzhome-commissioning-test-" + uuid4().hex
    password = secrets.token_urlsafe(32)
    observer = secrets.token_urlsafe(32)
    base = (ROOT / "deploy/mosquitto/acl").read_text()
    acl = tmp_path / "acl"
    prepare_acl(base, acl, "fixture_house", "fixture_relay", "grant")
    mount = f"type=bind,source={tmp_path},target=/fixture"
    docker(
        "run",
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
        "/CN=localhost",
        "-addext",
        "subjectAltName=DNS:localhost,DNS:mosquitto",
        "-days",
        "1",
    )
    (tmp_path / "ca.crt").write_bytes((tmp_path / "server.crt").read_bytes())

    def set_password(username, value, create=False, delete=False):
        options = ["-c"] if create else ["-D"] if delete else []
        docker(
            "run",
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
            input_text=None if delete else value + "\n" + value + "\n",
        )

    def recreate():
        subprocess.run(
            ["docker", "rm", "--force", name], capture_output=True, timeout=30
        )
        args = [
            "run",
            "--detach",
            "--rm",
            "--name",
            name,
            "--publish",
            "127.0.0.1::8883",
            "--user",
            "0:0",
            "--entrypoint",
            "/bin/sh",
            "--tmpfs",
            "/run/mosquitto:rw,noexec,nosuid,nodev,size=1m,mode=0700",
            "--tmpfs",
            "/mosquitto/data",
        ]
        files = {
            "start.sh": ROOT / "deploy/mosquitto/start.sh",
            "mosquitto.conf": ROOT / "deploy/mosquitto/mosquitto.conf",
            **{
                f: tmp_path / f
                for f in ("ca.crt", "server.crt", "server.key", "passwords", "acl")
            },
        }
        for filename, path in files.items():
            target = (
                "/mosquitto/config/mosquitto.conf"
                if filename == "mosquitto.conf"
                else "/bootstrap/" + filename
            )
            args += ["--mount", f"type=bind,source={path},target={target},readonly"]
        docker(*args, "eclipse-mosquitto:2", "/bootstrap/start.sh")
        port = int(docker("port", name, "8883/tcp").rsplit(":", 1)[1])
        deadline = time.monotonic() + 10
        context = ssl.create_default_context(cafile=str(tmp_path / "ca.crt"))
        while True:
            try:
                with socket.create_connection(("localhost", port), timeout=1) as raw:
                    with context.wrap_socket(raw, server_hostname="localhost"):
                        break
            except OSError:
                if time.monotonic() >= deadline:
                    pytest.fail("Isolated TLS broker did not become ready")
                time.sleep(0.1)
        return port

    try:
        set_password("kzhome-core", observer, create=True)
        set_password("kzdevice-fixture_relay", password)
        config = Config(
            "localhost",
            recreate(),
            "kzdevice-fixture_relay",
            password,
            str(tmp_path / "ca.crt"),
            "fixture_house",
            "fixture_relay",
            "fixture-relay",
        )
        yield config, observer, set_password, recreate, base, acl
    finally:
        subprocess.run(
            ["docker", "rm", "--force", name], capture_output=True, timeout=30
        )
        # Remove only generated fixture inputs; never traverse deployment paths.
        for input_name in ("server.key", "server.crt", "ca.crt", "passwords"):
            (tmp_path / input_name).unlink(missing_ok=True)


def test_real_tls_acl_relay_rotation_and_revocation(broker, monkeypatch):
    config, observer, set_password, recreate, base, acl = broker
    probe(config, observer)
    # The certificate has DNS SAN localhost; connecting by IP must fail closed.
    with pytest.raises(ssl.SSLCertVerificationError):
        with connection(
            replace(config, host="127.0.0.1"), config.username, config.password
        ):
            pass

    default_context = ssl.create_default_context
    with monkeypatch.context() as patch:
        patch.setattr(ssl, "create_default_context", lambda **kwargs: default_context())
        with pytest.raises(ssl.SSLCertVerificationError):
            with connection(config, config.username, config.password):
                pass

    # A deliberately broadened fixture must be detected by actual deliveries.
    original_acl = acl.read_bytes()
    acl.write_bytes(original_acl + b"topic read kzhome/v1/+/+/set\n")
    config = replace(config, port=recreate())
    with pytest.raises(Failure, match="forbidden topic"):
        probe(config, observer)
    acl.write_bytes(original_acl)
    config = replace(config, port=recreate())

    async def wire_round_trip():
        with connection(config, "kzhome-core", observer) as monitor:
            own = f"kzhome/v1/{config.house_id}/{config.device_id}/"
            assert monitor.subscribe([own + "ack", own + "state"]) == [1, 1]
            async with aiomqtt.Client(
                hostname=config.host,
                port=config.port,
                username=config.username,
                password=config.password,
                identifier=config.client_id,
                tls_context=ssl.create_default_context(cafile=config.ca_file),
                timeout=5,
            ) as client:
                relay = Relay(config.house_id, config.device_id, client.publish)
                await client.subscribe(relay.topic(TopicKind.SET), qos=1)
                command = CommandEnvelope(
                    command_id=uuid4().hex,
                    correlation_id=uuid4().hex,
                    timestamp=utc_now(),
                    state={"on": True},
                )
                monitor.publish(own + "set", command.model_dump_json().encode())
                async with asyncio.timeout(5):
                    message = await client.messages.__anext__()
                    assert isinstance(message.payload, bytes)
                    await relay.handle(
                        str(message.topic), message.payload, message.retain
                    )
                reports = {}
                for _ in range(2):
                    topic, payload = monitor.messages.get(timeout=5)
                    reports[topic.rsplit("/", 1)[1]] = json.loads(payload)
                assert reports["ack"]["command_id"] == command.command_id
                assert reports["ack"]["status"] == "applied"
                assert reports["state"]["state"] == {"on": True}
                assert all(
                    report["correlation_id"] == command.correlation_id
                    for report in reports.values()
                )

    if os.name == "nt":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(wire_round_trip())
    rotated = secrets.token_urlsafe(32)
    with connection(config, config.username, config.password) as old_session:
        set_password(config.username, rotated)
        config = replace(config, port=recreate())
        deadline = time.monotonic() + 5
        while old_session.client.is_connected() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not old_session.client.is_connected()
    require_denied(config, config.username, config.password)
    config = replace(config, password=rotated)
    probe(config, observer)
    prepare_acl(base, acl, config.house_id, config.device_id, "revoke")
    set_password(config.username, "", delete=True)
    config = replace(config, port=recreate())
    require_denied(config, config.username, config.password)
    # Revoking the relay must preserve Core's broker account.
    with connection(config, "kzhome-core", observer):
        pass
