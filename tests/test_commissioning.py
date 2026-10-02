"""Offline commissioning checks; never read/write deploy/local or real secrets."""

from pathlib import Path
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from app.transports.mqtt_models import (
    AckEnvelope,
    CommandEnvelope,
    StateEnvelope,
    StatusEnvelope,
)

from deploy import commission_device as tool
from simulator import commissioning_probe as probe

ROOT = Path(__file__).resolve().parents[1]


def test_exact_directional_acl_preserves_core_and_e2e():
    base = (ROOT / "deploy/mosquitto/acl").read_text()
    result = tool.render_acl(base, "house_001", "device_c6_001")
    assert result.startswith(base.rstrip() + "\n\n")
    entry = result.split(tool.MARKER + "\n")[1]
    assert entry.splitlines() == [
        "user kzdevice-device_c6_001",
        "topic read kzhome/v1/house_001/device_c6_001/set",
        "topic write kzhome/v1/house_001/device_c6_001/ack",
        "topic write kzhome/v1/house_001/device_c6_001/state",
        "topic write kzhome/v1/house_001/device_c6_001/status",
    ]
    assert not any(c in entry for c in "+#%")
    assert "telemetry" not in entry and "password" not in entry


@pytest.mark.parametrize(
    "value",
    ["", "a/b", "a+", "a#", "a\nuser kzhome-core", "../x", "a" * 65, "дом", "a\x00"],
)
@pytest.mark.parametrize("field", ["house_id", "device_id"])
def test_topic_or_acl_injection_rejected(value, field):
    args = {"house_id": "house", "device_id": "device", field: value}
    with pytest.raises(ValueError):
        tool.render_acl("user kzhome-core\n", **args)


@pytest.mark.parametrize(
    "base",
    [
        "topic read #\n",
        "pattern read #\n",
        "user kzhome-core\npattern read #",
        "user kzdevice-device\n",
        "user x\nuser x\n",
    ],
)
def test_global_patterns_and_duplicate_identity_rejected(base):
    with pytest.raises(ValueError):
        tool.render_acl(base, "house", "device")


def test_grant_check_revoke_reruns_and_binding_conflicts(tmp_path):
    base = (ROOT / "deploy/mosquitto/acl").read_text()
    path = tmp_path / "mqtt/acl"
    with pytest.raises(ValueError):
        tool.prepare_acl(base, path, "house", "device", "check")
    tool.prepare_acl(base, path, "house", "device", "grant")
    original = path.read_bytes()
    modified = path.stat().st_mtime_ns
    for action in ("grant", "check"):
        tool.prepare_acl(base, path, "house", "device", action)
        assert path.stat().st_mtime_ns == modified
    for house, device in (("other", "device"), ("house", "other")):
        for action in ("grant", "revoke"):
            with pytest.raises(ValueError):
                tool.prepare_acl(base, path, house, device, action)
            assert path.read_bytes() == original
    tool.prepare_acl(base, path, "house", "device", "revoke")
    assert path.read_text() == base.rstrip() + "\n"
    modified = path.stat().st_mtime_ns
    tool.prepare_acl(base, path, "house", "device", "revoke")
    assert path.stat().st_mtime_ns == modified
    assert list(path.parent.iterdir()) == [path]


def test_failed_atomic_replacement_preserves_original(tmp_path, monkeypatch):
    path = tmp_path / "acl"
    base = "user kzhome-core\n"
    path.write_text(base)
    monkeypatch.setattr(
        tool.os, "replace", Mock(side_effect=OSError("synthetic failure"))
    )
    with pytest.raises(OSError):
        tool.prepare_acl(base, path, "house", "device", "grant")
    assert path.read_text() == base
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize(
    "value",
    [
        "0.0.0.0",
        "127.0.0.1",
        "::",
        "::1",
        "8.8.8.8",
        "169.254.1.1",
        "224.0.0.1",
        "192.168.1.2:8883",
        "",
    ],
)
def test_lan_address_must_be_explicit_private_ipv4(value):
    with pytest.raises(ValueError):
        tool.validate_lan_ip(value)


@pytest.mark.parametrize("value", ["10.1.2.3", "172.16.1.2", "192.168.1.20"])
def test_private_lan_address_allowed(value):
    tool.validate_lan_ip(value)


def test_secret_paths_ignored_without_creating_files():
    paths = [
        "deploy/local/mqtt/passwords",
        "deploy/local/mqtt/acl",
        "deploy/local/device-c6.env",
        "deploy/local/production.env",
        "deploy/local/mqtt/server.key",
        "deploy/local/mqtt/ca.crt",
        ".idea/workspace.xml",
    ]
    result = subprocess.run(
        ["git", "check-ignore", "--no-index", "-z", "--stdin"],
        input=("\x00".join(paths) + "\x00").encode(),
        capture_output=True,
        cwd=ROOT,
        check=True,
    )
    assert set(result.stdout.decode().rstrip("\x00").split("\x00")) == set(paths)
    dockerignore = (ROOT / ".dockerignore").read_text().splitlines()
    assert "deploy/local/" in dockerignore
    assert "**/passwords" in dockerignore
    assert "**/*.key" in dockerignore


def test_production_artifacts_keep_private_defaults_and_verified_tls():
    compose = (ROOT / "compose.production.yaml").read_text()
    postgres = compose.split("\n  postgres:\n", 1)[1].split("\n  mosquitto:\n", 1)[0]
    broker = compose.split("\n  mosquitto:\n", 1)[1].split("\nnetworks:", 1)[0]
    assert "    ports:" not in postgres and "    ports:" not in broker
    assert '"127.0.0.1:8000:8000"' in compose
    assert "${MQTT_ACL_FILE:-./deploy/mosquitto/acl}" in broker
    lan = (ROOT / "compose.mqtt-lan.yaml").read_text()
    assert "${MQTT_LAN_BIND_IP:?" in lan
    assert "0.0.0.0" not in lan and "5432" not in lan and "8000" not in lan
    assert "networks: [backend, mqtt_lan]" in lan
    config = (ROOT / "deploy/mosquitto/mosquitto.conf").read_text()
    for required in (
        "listener 8883",
        "allow_anonymous false",
        "tls_version tlsv1.2",
        "certfile /run/mosquitto/server.crt",
        "password_file /run/mosquitto/passwords",
        "acl_file /run/mosquitto/acl",
    ):
        assert required in config.splitlines()
    assert config.count("listener ") == 1


def test_probe_requires_explicit_auth_denial_not_connection_failure(monkeypatch):
    monkeypatch.setattr(
        probe,
        "connection",
        Mock(side_effect=probe.Denied("Broker authentication denied")),
    )
    probe.require_denied(None, None, None)
    monkeypatch.setattr(
        probe, "connection", Mock(side_effect=OSError("TLS/network failure"))
    )
    with pytest.raises(OSError):
        probe.require_denied(None, None, None)


def test_probe_delivery_has_a_deadline_even_with_unrelated_payloads(monkeypatch):
    session = object.__new__(probe.Session)
    session.messages = probe.Queue()
    session.messages.put(("own/set", b"unrelated"))
    clock = iter((0, 0, 6))
    monkeypatch.setattr(probe.time, "monotonic", lambda: next(clock))
    with pytest.raises(probe.Failure, match="timed out"):
        session.received({"own/set"}, b"marker")


@pytest.mark.parametrize(
    "reason,denied,passes",
    [
        (0, False, True),
        (16, False, True),
        (135, True, True),
        (0, True, False),
        (128, True, False),
        (135, False, False),
    ],
)
def test_probe_checks_mqtt5_publish_reason(reason, denied, passes):
    session = object.__new__(probe.Session)
    session.client = SimpleNamespace(
        publish=lambda *args, **kwargs: SimpleNamespace(rc=0, mid=7)
    )
    session.published = probe.Queue()
    session.published.put((7, reason))
    if passes:
        session.publish("unused", b"marker", denied)
    else:
        with pytest.raises(probe.Failure):
            session.publish("unused", b"marker", denied)


def test_probe_delivery_checks_filtering_even_when_wildcard_subscription_accepted():
    session = object.__new__(probe.Session)
    session.messages = probe.Queue()
    session.messages.put(("foreign/set", b"marker"))
    session.messages.put(("own/set", b"marker"))
    with pytest.raises(probe.Failure, match="forbidden topic"):
        session.received({"own/set"}, b"marker")


def test_probe_cli_never_echoes_password_or_exception(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["probe", "--run"])
    monkeypatch.setattr(
        probe.Config,
        "from_env",
        Mock(side_effect=ValueError("password=synthetic-secret")),
    )
    with pytest.raises(SystemExit) as error:
        probe.main()
    assert error.value.code == 1
    assert "synthetic-secret" not in capsys.readouterr().out


@pytest.mark.parametrize(
    "envelope", [AckEnvelope, CommandEnvelope, StateEnvelope, StatusEnvelope]
)
def test_probe_uses_invalid_nonretained_markers_not_v1_commands(envelope):
    source = (ROOT / "simulator/commissioning_probe.py").read_text()
    assert "qos=1, retain=False" in source
    assert '"commissioning_probe"' in source
    assert '"command_id"' not in source
    assert "tls_insecure_set" not in source
    assert "ssl.create_default_context(cafile=config.ca_file)" in source
    with pytest.raises(ValidationError):
        envelope.model_validate({"commissioning_probe": "test"})
