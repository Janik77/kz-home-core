"""CI-only schedule checkpoint/history backup -> new profile, without replay."""

from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time
import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools import durable_software as manager  # noqa:E402
from durable_software_acceptance import cleanup, halt, inventory, launch, login  # noqa:E402
from numeric_profile_acceptance import digest  # noqa:E402


def snapshot(profile):
    return profile.query(
        "SELECT json_build_object('timezone',h.timezone,'zone_revision',h.timezone_revision,'zone_effective',h.timezone_effective_after,'cursor',h.schedule_cursor,'effective',a.schedule_effective_after,'state',a.schedule_state,'run_zone',r.schedule_timezone_revision)::text FROM houses h,automations a,scene_runs r WHERE h.id='fixture_house' AND a.id='schedule_backup_rule' AND r.scene_id=a.id"
    )


def main():
    assert os.environ.get("GITHUB_ACTIONS") == "true", "CI only"
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
            response = http.patch(
                "/core-api/houses/fixture_house",
                json={"timezone": "UTC", "expected_timezone_revision": 1},
            )
            assert response.status_code == 200, response.status_code
            now = datetime.now(UTC)
            occurrence = now.replace(second=0, microsecond=0) + timedelta(minutes=1)
            if (occurrence - now).total_seconds() < 8:
                occurrence += timedelta(minutes=1)
            response = http.post(
                "/core-api/automations",
                json={
                    "id": "schedule_backup_rule",
                    "name": "Backup schedule",
                    "house_id": "fixture_house",
                    "enabled": True,
                    "trigger": {
                        "type": "schedule",
                        "time": occurrence.strftime("%H:%M"),
                        "weekdays": [occurrence.weekday()],
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
            assert response.status_code == 201, response.status_code
            deadline = time.monotonic() + (occurrence - now).total_seconds() + 15
            while time.monotonic() < deadline:
                rows = http.get(
                    "/core-api/automations/schedule_backup_rule/runs"
                ).json()
                if rows and rows[0]["status"] == "succeeded":
                    break
                time.sleep(0.2)
            assert len(rows) == 1 and rows[0]["status"] == "succeeded", rows
            assert rows[0]["actions"][0]["receipt"]["ack_status"] == "applied"
        halt(process)
        process = None
        source.pg()
        expected = snapshot(source)
        counts = source.query("SELECT count(*) FROM device_commands")
        assert json.loads(expected)["state"]["occurrence"] == occurrence.isoformat()
        assert json.loads(expected)["zone_revision"] == 2
        source.stop()
        with source.lock():
            backup = source.backup(root / "backup")
        source_files, backup_files = digest(source.path), digest(backup)
        restored = manager.restore(backup, root / "restored")
        profiles.append(restored)
        restored.pg()
        assert snapshot(restored) == expected
        assert restored.query("SELECT count(*) FROM device_commands") == counts
        restored.stop()
        process, url = launch(restored, web)
        with httpx.Client(base_url=url, trust_env=False, timeout=5) as http:
            login(http, restored)
            time.sleep(3)
            assert restored.query("SELECT count(*) FROM device_commands") == counts
            assert (
                http.get("/core-api/automations/schedule_backup_rule/runs").json()
                == rows
            )
        halt(process)
        process = None
        assert digest(source.path) == source_files and digest(backup) == backup_files
        print(
            "PASS schedule PG/TLS: confirmed occurrence; timezone/revision/effective-after/cursor/checkpoint/run evidence preserved through backup+new profile restore; no startup replay; original unchanged",
            flush=True,
        )
    finally:
        if process is not None:
            halt(process)
        for profile in reversed(profiles):
            cleanup(profile, root)
        shutil.rmtree(root)
        assert inventory(docker) == before
        print(
            "PASS schedule backup inventory preserved; no backup secrets uploaded",
            flush=True,
        )


if __name__ == "__main__":
    main()
