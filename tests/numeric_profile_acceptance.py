"""Disposable CI: numeric state/latch backup and isolated restore over real PG/TLS."""

import asyncio
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import ssl
import sys
import tempfile
import time
from uuid import uuid4
import aiomqtt
import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools import durable_software as manager  # noqa: E402
from durable_software_acceptance import cleanup, halt, inventory, launch, login  # noqa: E402


def digest(path):
    return {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in path.iterdir()
        if p.is_file()
    }


def sample(profile, value, stamp=None, *, field="illuminance"):
    stamp = stamp or datetime.now(UTC).isoformat()

    async def send():
        port = int(
            profile.docker.text("port", profile.name + "-mqtt", "8883/tcp").rsplit(
                ":", 1
            )[1]
        )
        async with aiomqtt.Client(
            hostname="localhost",
            port=port,
            username="software_lab",
            password=profile.state["mqtt_password"],
            identifier="numeric-backup-" + uuid4().hex,
            tls_context=ssl.create_default_context(cafile=str(profile.path / "ca.crt")),
        ) as client:
            await client.publish(
                "kzhome/v1/fixture_house/fixture_relay/state",
                json.dumps(
                    {
                        "timestamp": stamp,
                        "correlation_id": str(uuid4()),
                        "state": {field: value},
                    }
                ),
                qos=1,
                retain=False,
            )

    asyncio.run(send())
    time.sleep(0.25)
    return stamp


def numeric_snapshot(profile):
    return profile.query(
        "SELECT json_build_object('value',d.state->'illuminance','observed',d.numeric_observed_at,'watermarks',d.numeric_watermarks,'latch',a.threshold_state)::text FROM devices d, automations a WHERE d.id='fixture_relay' AND a.id='numeric_backup_rule'"
    )


def main():
    assert os.environ.get("GITHUB_ACTIONS") == "true", "Disposable CI only"
    web = Path(sys.argv[1]).resolve()
    manager.validate_core()
    docker = manager.Docker()
    before = inventory(docker)
    root = Path(tempfile.mkdtemp(prefix="kzsoft-durable-test-")).resolve()
    profiles = []
    process = None
    try:
        source = manager.initialize(root / "source")
        profiles.append(source)
        process, url = launch(source, web)
        with httpx.Client(base_url=url, trust_env=False, timeout=5) as http:
            login(http, source)
            result = http.patch(
                "/core-api/devices/fixture_relay",
                json={
                    "capabilities": ["on_off", "illuminance", "temperature", "humidity"],
                    "expected_metadata_revision": http.get("/core-api/devices/fixture_relay").json()["metadata_revision"]
                },
            )
            assert result.status_code == 200
            result = http.post(
                "/core-api/automations",
                json={
                    "id": "numeric_backup_rule",
                    "house_id": "fixture_house",
                    "name": "Numeric backup proof",
                    "enabled": True,
                    "trigger": {
                        "type": "numeric_threshold",
                        "device_id": "fixture_relay",
                        "field": "illuminance",
                        "operator": "lt",
                        "value": 100,
                        "hysteresis": 20,
                    },
                    "conditions": [],
                    "actions": [
                        {
                            "type": "device_state",
                            "device_id": "fixture_relay",
                            "state": {"on": True},
                        }
                    ],
                },
            )
            assert result.status_code == 201, result.status_code
            old = sample(source, 120)
            low = sample(source, 90)
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                rows = http.get("/core-api/automations/numeric_backup_rule/runs").json()
                if rows and rows[0]["status"] == "succeeded":
                    break
                time.sleep(0.1)
            assert len(rows) == 1 and rows[0]["status"] == "succeeded", rows
            assert rows[0]["actions"][0]["receipt"]["ack_status"] == "applied"
        halt(process)
        process = None
        source.pg()
        expected = numeric_snapshot(source)
        counts = source.query("SELECT count(*) FROM device_commands")
        assert json.loads(expected)["value"] == 90
        assert not json.loads(expected)["latch"]["armed"]
        source.stop()
        with source.lock():
            backup = source.backup(root / "backup")
        source_files, backup_files = digest(source.path), digest(backup)
        restored = manager.restore(backup, root / "restored")
        profiles.append(restored)
        restored.pg()
        assert numeric_snapshot(restored) == expected
        assert restored.query("SELECT count(*) FROM device_commands") == counts
        restored.stop()
        process, url = launch(restored, web)
        with httpx.Client(base_url=url, trust_env=False, timeout=5) as http:
            login(http, restored)
            time.sleep(0.5)
            assert restored.query("SELECT count(*) FROM device_commands") == counts
            sample(restored, 120, old)
            sample(restored, 90, low)
            sample(restored, 80)
            time.sleep(0.5)
            assert restored.query("SELECT count(*) FROM device_commands") == counts
            rows = http.get("/core-api/automations/numeric_backup_rule/runs").json()
            assert len(rows) == 1 and rows[0]["status"] == "succeeded"
        halt(process)
        process = None
        assert digest(source.path) == source_files and digest(backup) == backup_files
        print(
            "PASS numeric PG/TLS: confirmed threshold; state/watermarks/latch preserved through backup+new-profile restore; no startup/duplicate/out-of-order/still-low command replay; original unchanged"
        )
    finally:
        if process is not None:
            halt(process)
        for profile in reversed(profiles):
            cleanup(profile, root)
        shutil.rmtree(root)
        assert inventory(docker) == before
        print(
            "PASS preservation: original Docker inventory unchanged; no backup secrets uploaded"
        )


if __name__ == "__main__":
    main()
