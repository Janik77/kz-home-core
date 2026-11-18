"""CI-only real PG/TLS NEW-copy invalidation, same-source resume and second restore."""

import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time
from uuid import uuid4
import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools import durable_software as manager  # noqa:E402
from durable_software_acceptance import cleanup, halt, inventory, launch, login  # noqa:E402
from numeric_profile_acceptance import digest  # noqa:E402


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
        base = "/core-api/v1/houses/fixture_house/scene-runs"
        with httpx.Client(base_url=url, trust_env=False, timeout=10) as http:
            login(http, source)
            response = http.post(
                "/core-api/scenes",
                json=dict(
                    id="restore_wait",
                    name="Explicit source wait",
                    house_id="fixture_house",
                    actions=[
                        dict(
                            type="device_state",
                            device_id="fixture_relay",
                            state={"on": True},
                        ),
                        dict(type="durable_wait", seconds=120),
                        dict(
                            type="device_state",
                            device_id="fixture_relay",
                            state={"on": False},
                        ),
                    ],
                ),
            )
            assert response.status_code == 201, response.text
            response = http.post(
                base + "/execute",
                headers={"Idempotency-Key": uuid4().hex},
                json=dict(scene_id="restore_wait", expected_revision=1),
            )
            assert response.status_code == 201, response.text
            run = response.json()
            path = base + "/" + run["id"]
            limit = time.monotonic() + 10
            while time.monotonic() < limit:
                waiting = http.get(path).json()
                if waiting["status"] == "waiting":
                    break
                time.sleep(0.1)
            assert waiting["status"] == "waiting", waiting
            first = waiting["actions"][0]["receipt"]
            assert first["ack_status"] == "applied" and first["reported_on"] is True
        halt(process)
        process = None
        with source.lock():
            backup = source.backup(root / "backup")
        source_files, backup_files = digest(source.path), digest(backup)
        manifest = json.loads((backup / "manifest.json").read_text())
        assert manifest["format"] == 2
        target = manager.restore(backup, root / "target")
        profiles.append(target)
        audit = json.loads((target.path / "restore-policy.json").read_text())
        assert audit["before"] == manifest["database"]
        assert len(audit["changes"]) == 1 and audit["changes"][0]["run_id"] == run["id"]
        assert audit["changes"][0]["reason"] == "restore_invalidation"
        process, url = launch(target, web)
        with httpx.Client(base_url=url, trust_env=False, timeout=10) as http:
            login(http, target)
            time.sleep(2)
            cancelled = http.get(path).json()
            assert (
                cancelled["status"] == "cancelled"
                and cancelled["execution_error"] == "restore_invalidation"
            )
            assert [a["status"] for a in cancelled["actions"]] == [
                "applied",
                "cancelled",
                "cancelled",
            ]
            assert cancelled["actions"][0]["receipt"] == first
            assert target.query("SELECT count(*) FROM device_commands") == "1"
        halt(process)
        process = None
        with target.lock():
            second_backup = target.backup(root / "second-backup")
        second = manager.restore(second_backup, root / "second")
        profiles.append(second)
        second_audit = json.loads((second.path / "restore-policy.json").read_text())
        assert (
            second_audit["changes"] == []
            and second_audit["before"] == second_audit["after"]
        )
        process, url = launch(second, web)
        with httpx.Client(base_url=url, trust_env=False, timeout=10) as http:
            login(http, second)
            time.sleep(2)
            assert http.get(path).json() == cancelled
            assert second.query("SELECT count(*) FROM device_commands") == "1"
        halt(process)
        process = None
        source.pg()
        assert manager.profile_fingerprints(source) == manifest["database"]
        assert (
            source.query(
                "SELECT execution_finished FROM scene_runs WHERE scene_id='restore_wait'"
            )
            == "f"
        )
        source.stop()
        assert digest(source.path) == source_files and digest(backup) == backup_files
        # Original database retains its own intent; only it may resume once.
        process, url = launch(source, web)
        with httpx.Client(base_url=url, trust_env=False, timeout=10) as http:
            login(http, source)
            limit = time.monotonic() + 150
            while time.monotonic() < limit:
                result = http.get(path).json()
                if result["status"] == "succeeded":
                    break
                assert result["status"] in {"waiting", "running"}, result
                time.sleep(0.2)
            assert result["status"] == "succeeded", result
            assert [a["status"] for a in result["actions"]] == [
                "applied",
                "completed",
                "applied",
            ]
            assert result["actions"][0]["receipt"] == first
            assert result["actions"][2]["receipt"]["ack_status"] == "applied"
            assert source.query("SELECT count(*) FROM device_commands") == "2"
        halt(process)
        process = None
        # Cancellation remains stable after the original due time has passed.
        process, url = launch(target, web)
        with httpx.Client(base_url=url, trust_env=False, timeout=10) as http:
            login(http, target)
            time.sleep(2)
            assert http.get(path).json() == cancelled
            assert target.query("SELECT count(*) FROM device_commands") == "1"
        halt(process)
        process = None
        assert digest(backup) == backup_files
        print(
            "PASS real PG/TLS parked wait: full pre-policy fingerprints equal; NEW target-only exact cancellation; source unchanged until explicit restart then resumes once; second restore cannot revive cloned intent; target emits zero commands",
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
            "PASS wait restore inventory preserved; software only; no private backup upload",
            flush=True,
        )


if __name__ == "__main__":
    main()
