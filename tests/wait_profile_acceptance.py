"""CI-only real PG/TLS NEW-copy invalidation, same-source resume and second restore."""

from datetime import datetime
import json
import os
from pathlib import Path
import shutil
import sys
import subprocess
import tempfile
import time
from uuid import uuid4
import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools import durable_software as manager  # noqa:E402
from durable_software_acceptance import (  # noqa:E402
    cleanup,
    command,
    halt,
    inventory,
    launch,
    login,
)
from numeric_profile_acceptance import digest  # noqa:E402
from upgrade_software_acceptance import seed_schedule_source  # noqa:E402


def historical_backup(old_core, source_path, backup):
    """Never import historical backup helpers into the current tooling process."""
    paths_before = list(sys.path)
    modules_before = {
        name: module
        for name, module in sys.modules.items()
        if name == "tools" or name.startswith("tools.")
    }
    subprocess.run(
        [
            sys.executable,
            "-B",
            str(old_core / "tools/durable_software.py"),
            "backup",
            "--state",
            str(source_path),
            "--destination",
            str(backup),
        ],
        check=True,
    )
    assert sys.path == paths_before, "Historical backup changed parent import paths"
    assert {
        name: module
        for name, module in sys.modules.items()
        if name == "tools" or name.startswith("tools.")
    } == modules_before, "Historical backup leaked modules into current tooling"


def main():
    assert os.environ.get("GITHUB_ACTIONS") == "true", "CI only"
    web = Path(sys.argv[1]).resolve()
    manager.validate_core()
    old_core = Path(sys.argv[2]).resolve() if len(sys.argv) > 2 else None
    old_web = Path(sys.argv[3]).resolve() if old_core else None
    source_manager = manager
    same_schema_upgrade = False
    if old_core:
        import importlib.util

        manager.validate_source_checkout(old_core)
        source_migration = manager.source_contract(old_core)[2]
        same_schema_upgrade = source_migration == manager.EXPECTED_MIGRATION
        assert source_migration in {
            "0018_scene_drafts",
            "0016_durable_waits",
            "0017_metadata_cas",
        }
        spec = importlib.util.spec_from_file_location(
            "source56_wait_profile", old_core / "tools/durable_software.py"
        )
        source_manager = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(source_manager)
        source_manager.validate_core()

    def restore_copy(backup, target):
        try:
            return (
                manager.upgrade(backup, target, old_core)
                if old_core
                else manager.restore(backup, target)
            )
        except Exception:
            # Even an unexpected fixture failure must retain ownership-aware
            # cleanup of a partial target, without masking the original error.
            if (target / "profile.json").is_file():
                profiles.append(manager.Profile(target, docker))
            raise

    docker = manager.Docker()
    before = inventory(docker)
    root = Path(tempfile.mkdtemp(prefix="kzsoft-durable-test-")).resolve()
    profiles = []
    process = None
    try:
        source = source_manager.initialize(root / "source")
        profiles.append(source)
        process, url = launch(source, old_web or web, old_core or manager.CORE)
        base = "/core-api/v1/houses/fixture_house/scene-runs"
        with httpx.Client(base_url=url, trust_env=False, timeout=10) as http:
            login(http, source)
            expected_paths = {}
            if same_schema_upgrade:
                # Real #60 API populates existing revisions and non-executable
                # drafts alongside a consumed schedule, not hand-edited rows.
                seed_schedule_source(http, source)
                house_path = "/core-api/houses/fixture_house"
                house = http.get(house_path).json()
                assert (
                    http.patch(
                        house_path,
                        json={
                            "name": "Original #60 house",
                            "expected_metadata_revision": house["metadata_revision"],
                        },
                    ).status_code
                    == 200
                )
                assert (
                    http.post(
                        "/core-api/rooms",
                        json={
                            "id": "source60_room",
                            "floor_id": "fixture_floor",
                            "name": "Original #60 room",
                        },
                    ).status_code
                    == 201
                )
                favorites = "/core-api/v1/houses/fixture_house/me/device-favorites"
                assert http.put(favorites + "/fixture_relay").status_code == 204
                for p in (
                    house_path,
                    "/core-api/rooms/source60_room",
                    favorites,
                    "/core-api/automations/source54_schedule/runs",
                ):
                    response = http.get(p)
                    assert response.status_code == 200
                    expected_paths[p] = response.json()
            commands_before_wait = int(
                source.query("SELECT count(*) FROM device_commands")
            )
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
            if same_schema_upgrade:
                scene = "/core-api/scenes/restore_wait"
                draft = http.post(
                    scene + "/duplicate",
                    json={
                        "id": "source60_draft",
                        "name": "Original #60 draft",
                        "expected_revision": response.json()["revision"],
                    },
                )
                assert draft.status_code == 201 and draft.json()["is_draft"]
                draft_path = "/core-api/scenes/source60_draft"
                persisted_draft = http.get(draft_path)
                assert (
                    persisted_draft.status_code == 200
                    and persisted_draft.json()["is_draft"]
                )
                expected_paths[draft_path] = persisted_draft.json()
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
        if old_core:
            # Run the genuine historical backup in its own interpreter. Loading
            # old and new tools.restore_policy in one process poisons sys.modules.
            backup = root / "backup"
            historical_backup(old_core, source.path, backup)
        else:
            with source.lock():
                backup = source.backup(root / "backup")
        source_files, backup_files = digest(source.path), digest(backup)
        manifest = json.loads((backup / "manifest.json").read_text())
        assert manifest["format"] == 2
        if same_schema_upgrade:
            unverified = root / "old-format"
            shutil.copytree(backup, unverified)
            legacy_manifest = dict(manifest)
            legacy_manifest["format"] = 1
            (unverified / "manifest.json").write_text(json.dumps(legacy_manifest))
            refused_format = root / "refused-format"
            try:
                restore_copy(unverified, refused_format)
            except ValueError as error:
                assert str(error) == "Invalid backup manifest"
            else:
                raise AssertionError(
                    "Same-schema upgrade accepted an old manifest format"
                )
            assert not refused_format.exists()
        # A structurally valid but false fingerprint must fail before policy/audit
        # or completion markers, even with an intact SQL archive and file hashes.
        bad_backup = root / "bad-fingerprint-backup"
        shutil.copytree(backup, bad_backup)
        bad_manifest = json.loads((bad_backup / "manifest.json").read_text())
        bad_manifest["database"]["scene_runs"]["sha256"] = "a" * 64
        (bad_backup / "manifest.json").write_text(json.dumps(bad_manifest))
        refused = root / "refused-fingerprint"
        try:
            restore_copy(bad_backup, refused)
        except ValueError as error:
            assert "differs from complete backup fingerprints" in str(
                error
            ) or "Source backup database fingerprint mismatch" in str(error), str(error)
        else:
            raise AssertionError("False fingerprint accepted")
        failed = next(profile for profile in profiles if profile.path == refused)
        assert not (refused / "initialized").exists()
        assert not (refused / "seeded").exists()
        assert not (refused / "restore-policy.json").exists()
        failed._open_pg()
        assert (
            failed.query(
                "SELECT count(*) FROM scene_runs WHERE cancelled_at IS NOT NULL"
            )
            == "0"
        )
        failed.stop()
        target = restore_copy(backup, root / "target")
        profiles.append(target)
        assert target.owner != source.owner
        assert target.state["database_identity"] != source.state["database_identity"]
        assert (target.path / "initialized").is_file()
        audit = json.loads((target.path / "restore-policy.json").read_text())
        if old_core and not same_schema_upgrade:
            # migrate_upgrade first compares the FULL source manifest before any
            # DDL, then all legacy columns and only counter=1 additions. The
            # policy receives that verified post-migration baseline atomically.
            target._open_pg()
            assert (
                target.query("SELECT version_num FROM alembic_version")
                == manager.EXPECTED_MIGRATION
            )
            for table in ("houses", "floors", "rooms", "devices"):
                assert (
                    target.query(
                        f"SELECT count(*) FROM {table} WHERE metadata_revision != 1"
                    )
                    == "0"
                )
            assert (
                audit["before"]["scene_run_waits"]
                == manifest["database"]["scene_run_waits"]
            )
            assert (
                audit["before"]["device_commands"]
                == manifest["database"]["device_commands"]
            )
            target.stop()
        else:
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
            assert (
                int(target.query("SELECT count(*) FROM device_commands"))
                == commands_before_wait + 1
            )
            for p, expected in expected_paths.items():
                actual = http.get(p)
                assert actual.status_code == 200, (
                    f"Restored fixture HTTP {p}: {actual.status_code}"
                )
                value = actual.json()
                if p == "/core-api/houses/fixture_house":
                    # The complete manifest was already compared on the stopped
                    # copy. Live scheduler ticks update schedule_cursor and
                    # organically advance HouseORM.updated_at after startup.
                    expected = dict(expected)
                    updated_before = datetime.fromisoformat(expected.pop("updated_at"))
                    updated_after = datetime.fromisoformat(value.pop("updated_at"))
                    assert updated_after >= updated_before
                assert value == expected, f"Restored fixture payload mismatch at {p}"
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
            assert (
                int(second.query("SELECT count(*) FROM device_commands"))
                == commands_before_wait + 1
            )
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
        process, url = launch(source, old_web or web, old_core or manager.CORE)
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
            assert (
                int(source.query("SELECT count(*) FROM device_commands"))
                == commands_before_wait + 2
            )
        halt(process)
        process = None
        # Cancellation remains stable after the original due time has passed.
        process, url = launch(target, web)
        with httpx.Client(base_url=url, trust_env=False, timeout=10) as http:
            login(http, target)
            time.sleep(2)
            assert http.get(path).json() == cancelled
            assert (
                int(target.query("SELECT count(*) FROM device_commands"))
                == commands_before_wait + 1
            )
            if same_schema_upgrade:
                # Only an explicit new action may operate the restored relay.
                command(http, False)
                assert (
                    int(target.query("SELECT count(*) FROM device_commands"))
                    == commands_before_wait + 2
                )
        halt(process)
        process = None
        assert digest(backup) == backup_files
        if same_schema_upgrade:
            print(
                "PASS exact #60 format-2 same-schema upgrade: favorite, house/room metadata, draft, schedule receipt/history and prior ACK preserved; one new explicit OFF acknowledged",
                flush=True,
            )
        print(
            "PASS real PG/TLS parked wait: full pre-policy fingerprints equal; NEW target-only exact cancellation; source unchanged until explicit restart then resumes once; second restore cannot revive cloned intent; zero replayed target commands",
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
