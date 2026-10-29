"""Opt-in read-only Compose validation: no containers, volumes or real secrets."""
import json
import os
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_ARM64_CONFIG_TESTS") != "1",
    reason="explicit RUN_ARM64_CONFIG_TESTS=1 requires Docker Compose",
)


def configuration(tmp_path, *, overlay=True, lan=False, address="127.0.0.1"):
    envfile = tmp_path / "validation.env"
    envfile.write_text("# Dummy validation only; no installed environment\n")
    env = {**os.environ, "COMPOSE_PROJECT_NAME": "kzhome-arm64-validation",
           "DATABASE_URL": "postgresql+psycopg://dummy:dummy@postgres:5432/kzhome",
           "POSTGRES_PASSWORD": "dummy-not-for-deployment",
           "AUTH_JWT_SECRET": "dummy-not-for-deployment",
           "MQTT_PASSWORD": "dummy-not-for-deployment",
           "MQTT_ACL_FILE": "./deploy/local/mqtt/acl",
           "CORE_ARM64_IMAGE": "kz-home-core:v0.13-validation",
           "MQTT_CLIENT_ID": "validation-only", "MQTT_LAN_BIND_IP": "192.0.2.10"}
    env.pop("HTTPS_BIND_IP", None)
    if address is not None:
        env["HTTPS_BIND_IP"] = address
    args = ["docker", "compose", "--env-file", str(envfile),
            "-f", str(ROOT / "compose.production.yaml")]
    if overlay:
        args += ["-f", str(ROOT / "compose.arm64.yaml")]
    if lan:
        args += ["-f", str(ROOT / "compose.mqtt-lan.yaml")]
    return subprocess.run([*args, "config", "--format", "json"], env=env,
                          capture_output=True, text=True, timeout=30)


def test_arm64_preserves_production_security_and_storage(tmp_path):
    base_result = configuration(tmp_path, overlay=False)
    arm_result = configuration(tmp_path)
    assert base_result.returncode == arm_result.returncode == 0
    base = json.loads(base_result.stdout)
    arm = json.loads(arm_result.stdout)
    for name in ("core", "postgres", "mosquitto"):
        original, adapted = base["services"][name], arm["services"][name]
        assert adapted["platform"] == "linux/arm64"
        for field in ("environment", "volumes", "networks", "ports"):
            assert original.get(field) == adapted.get(field)
    assert not arm["services"]["postgres"].get("ports")
    assert not arm["services"]["mosquitto"].get("ports")
    assert arm["networks"]["backend"]["internal"] is True
    proxy = arm["services"]["proxy"]
    assert set(proxy["networks"]) == {"frontend"}
    assert proxy["platform"] == "linux/arm64"
    assert proxy["ports"][0]["host_ip"] == "127.0.0.1"
    assert str(proxy["ports"][0]["published"]) == "443"
    assert proxy["depends_on"]["core"]["condition"] == "service_healthy"
    assert all(m["read_only"] and not m["bind"]["create_host_path"] for m in proxy["volumes"])
    assert all(v["name"].startswith("kzhome-arm64-validation_") for v in arm["volumes"].values())
    assert not any(v.get("external") for v in arm["volumes"].values())


def test_arm64_requires_explicit_https_address(tmp_path):
    assert configuration(tmp_path, address=None).returncode != 0
    assert configuration(tmp_path, address="").returncode != 0


def test_optional_mqtt_lan_still_publishes_tls_only(tmp_path):
    result = configuration(tmp_path, lan=True)
    assert result.returncode == 0
    merged = json.loads(result.stdout)
    assert not merged["services"]["postgres"].get("ports")
    ports = merged["services"]["mosquitto"]["ports"]
    assert len(ports) == 1 and ports[0]["target"] == 8883
    assert str(ports[0]["published"]) == "8883"
    assert ports[0]["host_ip"] == "192.0.2.10"
