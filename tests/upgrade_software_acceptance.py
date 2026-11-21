"""Opt-in disposable CI upgrade acceptance. No user profiles or backup artifacts."""

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
from uuid import uuid4

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools import durable_software as manager  # noqa: E402
from durable_software_acceptance import (  # noqa: E402
    cleanup,
    command,
    dispatch_scene_action,
    halt,
    inventory,
    launch,
    login,
    read_run,
)


def cli(core, *args, success=True):
    result = subprocess.run(
        [
            sys.executable,
            "-B",
            str(core / "tools/durable_software.py"),
            *map(str, args),
        ],
        capture_output=True,
        timeout=180,
    )
    if result.returncode != 0:
        # Keep only the launcher's code-only diagnostic. Never expose captured
        # exception messages, paths, output, arguments, or fixture credentials.
        marker = re.compile(
            rb"PROFILE_FAILURE [A-Za-z][A-Za-z0-9_]* "
            rb"[A-Za-z0-9_.-]+:[0-9]+(?:,[A-Za-z0-9_.-]+:[0-9]+)*"
        )
        for line in result.stderr.splitlines():
            if marker.fullmatch(line):
                print(f"[profile CLI: {args[0]}] {line.decode('ascii')}", flush=True)
    if (result.returncode == 0) is not success:
        raise AssertionError(f"Profile CLI {args[0]} returned unexpected status")


def digest(directory):
    return {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in directory.iterdir()
        if p.is_file()
    }


def variant(backup, target, *, pins=False, identity=False, corrupt=False, dump=None):
    shutil.copytree(backup, target)
    if pins or identity:
        data = json.loads((target / "profile.json").read_text())
        if pins:
            data["web_sha"] = "unsupported"
        if identity:
            del data["database_identity"]
        (target / "profile.json").write_text(json.dumps(data))
    if dump is not None:
        (target / "database.dump").write_bytes(dump)
    if not corrupt:
        manifest = json.loads((target / "manifest.json").read_text())
        for name in manifest["sha256"]:
            manifest["sha256"][name] = hashlib.sha256(
                (target / name).read_bytes()
            ).hexdigest()
        (target / "manifest.json").write_text(json.dumps(manifest))
    else:
        with (target / "database.dump").open("ab") as stream:
            stream.write(b"corrupt")
    return target


def marker_failure_cases(backup, root, web, docker, profiles, *, old_core=None):
    """Actual restore/upgrade cannot start after either marker write fails."""
    original_write = manager.private_write
    prefix = "upgrade" if old_core is not None else "restore"
    for marker, after_write in (
        ("seeded", False),
        ("seeded", True),
        (".initialized.pending", False),
        (".initialized.pending", True),
    ):
        target = root / f"{prefix}-marker-{marker.strip('.')}-{after_write}"
        reached = False

        def failing_write(path, data):
            nonlocal reached
            if path == target / marker:
                reached = True
                if after_write:
                    original_write(path, data)
                raise OSError("Injected test-only marker write failure")
            return original_write(path, data)

        manager.private_write = failing_write
        try:
            try:
                if old_core is None:
                    manager.restore(backup, target)
                else:
                    manager.upgrade(backup, target, old_core)
            except OSError:
                assert reached, "Failure occurred before intended marker boundary"
            else:
                raise AssertionError("Marker failure did not stop restore")
        finally:
            manager.private_write = original_write
            if (target / "profile.json").is_file():
                profiles.append(manager.Profile(target, docker))
        assert not (target / "initialized").exists()
        cli(manager.CORE, "start", "--state", target, "--web-root", web, success=False)
        profile = profiles[-1]
        assert not any(
            item and item["State"]["Running"]
            for item in (
                profile.owned("container", profile.name + "-pg"),
                profile.owned("container", profile.name + "-mqtt"),
            )
        )
        profile._open_pg()
        assert (
            profile.query("SELECT version_num FROM alembic_version")
            == manager.EXPECTED_MIGRATION
        )
        profile.stop()
    print(
        f"PASS {prefix} marker faults: before/after seeded and pending completion write; no initialized, start refused, services stopped"
    )


def seed_numeric_source(http, source):
    """Actual numeric reports + receipt on old runtime, never invented persisted rows."""
    from numeric_profile_acceptance import sample

    device = http.get("/core-api/devices/fixture_relay").json()
    token = (
        {"expected_metadata_revision": device["metadata_revision"]}
        if "metadata_revision" in device
        else {}
    )
    response = http.patch(
        "/core-api/devices/fixture_relay",
        json={
            "capabilities": ["on_off", "illuminance", "temperature", "humidity"],
            **token,
        },
    )
    assert response.status_code == 200
    response = http.post(
        "/core-api/automations",
        json={
            "id": "numeric_backup_rule",
            "house_id": "fixture_house",
            "name": "Preserve numeric history",
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
    assert response.status_code == 201
    sample(source, 22.5, field="temperature")
    sample(source, 46, field="humidity")
    old, low = sample(source, 120), sample(source, 90)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        rows = http.get("/core-api/automations/numeric_backup_rule/runs").json()
        if rows and rows[0]["status"] == "succeeded":
            break
        time.sleep(0.1)
    assert len(rows) == 1 and rows[0]["status"] == "succeeded", rows
    assert rows[0]["actions"][0]["receipt"]["ack_status"] == "applied"
    return old, low


def assert_no_numeric_replay(http, profile, stamps, counts):
    from numeric_profile_acceptance import sample

    assert profile.query("SELECT count(*) FROM device_commands") == counts
    sample(profile, 120, stamps[0])
    sample(profile, 90, stamps[1])
    sample(profile, 80)
    time.sleep(0.5)
    assert profile.query("SELECT count(*) FROM device_commands") == counts
    rows = http.get("/core-api/automations/numeric_backup_rule/runs").json()
    assert len(rows) == 1 and rows[0]["status"] == "succeeded"
    print(
        "PASS source52 numeric values/freshness/watermarks/latch/history preserved; no startup/old/duplicate/still-low replay",
        flush=True,
    )


def seed_schedule_source(http, source):
    from datetime import datetime, UTC, timedelta

    house = http.get("/core-api/houses/fixture_house").json()
    zone = http.patch(
        "/core-api/houses/fixture_house",
        json={
            "timezone": "UTC",
            "expected_timezone_revision": house["timezone_revision"],
        },
    )
    assert zone.status_code == 200, zone.text
    now = datetime.now(UTC)
    occurrence = now.replace(second=0, microsecond=0) + timedelta(minutes=1)
    if (occurrence - now).total_seconds() < 8:
        occurrence += timedelta(minutes=1)
    response = http.post(
        "/core-api/automations",
        json=dict(
            id="source54_schedule",
            name="Preserve schedule",
            house_id="fixture_house",
            enabled=True,
            trigger=dict(
                type="schedule",
                time=occurrence.strftime("%H:%M"),
                weekdays=[occurrence.weekday()],
            ),
            conditions=[],
            actions=[
                dict(
                    type="device_state", device_id="fixture_relay", state={"on": False}
                )
            ],
        ),
    )
    assert response.status_code == 201, response.text
    deadline = time.monotonic() + (occurrence - now).total_seconds() + 20
    while time.monotonic() < deadline:
        rows = http.get("/core-api/automations/source54_schedule/runs").json()
        if (
            rows
            and rows[0]["status"] == "succeeded"
            and source.query(
                "SELECT execution_finished AND execution_error IS NULL FROM scene_runs WHERE scene_id='source54_schedule'"
            )
            == "t"
        ):
            break
        time.sleep(0.2)
    assert len(rows) == 1 and rows[0]["status"] == "succeeded", rows
    assert rows[0]["actions"][0]["receipt"]["ack_status"] == "applied"
    print(
        "PASS source54 real scheduled receipt/checkpoint seeded for exact preservation",
        flush=True,
    )


def main():
    assert os.environ.get("GITHUB_ACTIONS") == "true"
    web, old_core, old_web = map(lambda p: Path(p).resolve(), sys.argv[1:])
    manager.validate_source_checkout(old_core)
    source_migration = manager.source_contract(old_core)[2]
    spec = importlib.util.spec_from_file_location(
        "old_profile", old_core / "tools/durable_software.py"
    )
    old = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(old)
    docker = manager.Docker()
    before = inventory(docker)
    root = Path(tempfile.mkdtemp(prefix="kzsoft-durable-test-")).resolve()
    process = None
    numeric_stamps = None
    profiles = []
    try:
        cli(old_core, "init", "--state", root / "source")
        source = old.Profile(root / "source", docker)
        profiles.append(source)
        process, url = launch(source, old_web, old_core)
        with httpx.Client(base_url=url, trust_env=False, timeout=5) as http:
            login(http, source)
            house = http.get("/core-api/houses/fixture_house").json()
            token = (
                {"expected_metadata_revision": house["metadata_revision"]}
                if "metadata_revision" in house
                else {}
            )
            assert (
                http.patch(
                    "/core-api/houses/fixture_house",
                    json={"name": "Keep original home", **token},
                ).status_code
                == 200
            )
            assert (
                http.post(
                    "/core-api/rooms",
                    json={
                        "id": "kept_room",
                        "floor_id": "fixture_floor",
                        "name": "Keep room",
                    },
                ).status_code
                == 201
            )
            response = http.post(
                "/core-api/scenes",
                json={
                    "id": "upgrade_scene",
                    "house_id": "fixture_house",
                    "name": "Keep scene",
                    "actions": [
                        {
                            "type": "device_state",
                            "device_id": "fixture_relay",
                            "state": {"on": True},
                        },
                        {
                            "type": "device_state",
                            "device_id": "fixture_relay",
                            "state": {"on": False},
                        },
                    ],
                },
            )
            assert response.status_code == 201
            response = http.patch(
                "/core-api/scenes/upgrade_scene",
                json={
                    "expected_revision": response.json()["revision"],
                    "name": "Keep revised scene",
                },
            )
            assert response.status_code == 200
            created = http.post(
                "/core-api/v1/houses/fixture_house/scene-runs",
                headers={"Idempotency-Key": uuid4().hex},
                json={
                    "scene_id": "upgrade_scene",
                    "expected_revision": response.json()["revision"],
                },
            )
            assert created.status_code == 201
            run_id = created.json()["id"]
            first_action = dispatch_scene_action(http, run_id, 0, True)["actions"][0]
            if source_migration in {
                "0013_device_automations",
                "0014_numeric_thresholds",
                "0015_house_schedules",
                "0016_durable_waits",
                "0017_metadata_cas",
            }:
                assert (
                    http.put(
                        "/core-api/v1/houses/fixture_house/me/device-favorites/fixture_relay"
                    ).status_code
                    == 204
                )
                kept = http.post(
                    "/core-api/automations",
                    json={
                        "id": "kept_authority",
                        "house_id": "fixture_house",
                        "name": "Preserve authority",
                        "enabled": False,
                        "trigger": {
                            "type": "device_state",
                            "device_id": "fixture_relay",
                            "field": "on",
                            "operator": "eq",
                            "value": True,
                        },
                        "conditions": [],
                        "actions": [
                            {
                                "type": "device_state",
                                "device_id": "fixture_relay",
                                "state": {"on": False},
                            }
                        ],
                    },
                )
                assert kept.status_code == 201

            if source_migration in {
                "0014_numeric_thresholds",
                "0015_house_schedules",
                "0016_durable_waits",
                "0017_metadata_cas",
            }:
                numeric_stamps = seed_numeric_source(http, source)
            if source_migration in {
                "0015_house_schedules",
                "0016_durable_waits",
                "0017_metadata_cas",
            }:
                seed_schedule_source(http, source)

            cli(
                old_core,
                "backup",
                "--state",
                source.path,
                "--destination",
                root / "active-backup",
                success=False,
            )
            assert not (root / "active-backup").exists()
        halt(process)
        process = None
        # Add a second ordinary test user; never change an existing account or privilege.
        source.pg()
        source.query(
            "INSERT INTO users (id,email,password_hash,is_active,is_superuser) SELECT 'upgrade-other','other@example.test',password_hash,true,false FROM users WHERE email='owner@example.test'"
        )
        original_rows = manager.legacy_fingerprints(source, source_migration)
        numeric_counts = source.query("SELECT count(*) FROM device_commands")
        if numeric_stamps:
            from numeric_profile_acceptance import numeric_snapshot

            numeric_before = numeric_snapshot(source)
        source.stop()
        cli(
            old_core, "backup", "--state", source.path, "--destination", root / "backup"
        )
        backup = root / "backup"
        source_files, backup_files = digest(source.path), digest(backup)

        def attempt(source_backup, target, success=False):
            cli(
                manager.CORE,
                "upgrade",
                "--backup",
                source_backup,
                "--state",
                target,
                "--source-checkout",
                old_core,
                success=success,
            )

        for kind in ("pins", "identity", "corrupt"):
            bad = variant(backup, root / ("bad-" + kind), **{kind: True})
            target = root / ("refused-" + kind)
            attempt(bad, target)
            assert not target.exists()
        link = root / "linked-backup"
        link.symlink_to(backup, target_is_directory=True)
        attempt(link, root / "refused-link")
        assert not (root / "refused-link").exists()
        attempt(backup, source.path)
        cli(
            manager.CORE,
            "restore",
            "--backup",
            backup,
            "--state",
            root / "ordinary-restore",
            success=False,
        )
        assert not (root / "ordinary-restore").exists()

        # Valid manifest but invalid SQL archive: partial target must remain uninitialized.
        partial = root / "partial"
        bad_dump = variant(backup, root / "bad-dump", dump=b"not a PostgreSQL archive")
        attempt(bad_dump, partial)
        assert not (partial / "initialized").exists()
        cli(manager.CORE, "start", "--state", partial, "--web-root", web, success=False)
        if (partial / "profile.json").is_file():
            profiles.append(manager.Profile(partial, docker))
        else:
            raise AssertionError(
                "Valid-source guard passed but restore did not reach partial target"
            )

        # A genuine archive with wrong source revision is refused before migration.
        source.pg()
        source.query("UPDATE alembic_version SET version_num='unsupported_revision'")
        wrong_dump = source.docker.run(
            "exec",
            source.name + "-pg",
            "pg_dump",
            "-U",
            "lab",
            "-d",
            "lab",
            "-Fc",
            "--no-owner",
            "--no-acl",
        ).stdout
        source.query(f"UPDATE alembic_version SET version_num='{source_migration}'")
        source.stop()
        wrong = variant(backup, root / "wrong-revision", dump=wrong_dump)
        attempt(wrong, root / "wrong-target")
        profiles.append(manager.Profile(root / "wrong-target", docker))
        assert not (root / "wrong-target" / "initialized").exists()

        # Simulate failure after the actual additive migration, before initialization.
        original_migrate = manager.migrate_upgrade

        def fail_after_migration(profile, **kwargs):
            original_migrate(profile, **kwargs)
            raise RuntimeError("Injected test-only post-migration failure")

        manager.migrate_upgrade = fail_after_migration
        try:
            try:
                manager.upgrade(backup, root / "migration-failed", old_core)
            except RuntimeError:
                pass
            else:
                raise AssertionError("Injected failure did not stop upgrade")
        finally:
            manager.migrate_upgrade = original_migrate
        assert not (root / "migration-failed" / "initialized").exists()
        cli(
            manager.CORE,
            "start",
            "--state",
            root / "migration-failed",
            "--web-root",
            web,
            success=False,
        )
        failed_profile = manager.Profile(root / "migration-failed", docker)
        profiles.append(failed_profile)
        failed_profile._open_pg()
        assert (
            failed_profile.query("SELECT version_num FROM alembic_version")
            == manager.EXPECTED_MIGRATION
        )
        assert (
            manager.legacy_fingerprints(failed_profile, source_migration)
            == original_rows
        )
        failed_profile.stop()

        marker_failure_cases(backup, root, web, docker, profiles, old_core=old_core)

        attempt(backup, root / "upgraded", success=True)
        upgraded = manager.Profile(root / "upgraded", docker)
        profiles.append(upgraded)
        upgraded.pg()
        assert (
            upgraded.query("SELECT version_num FROM alembic_version")
            == manager.EXPECTED_MIGRATION
        )
        assert manager.legacy_fingerprints(upgraded, source_migration) == original_rows
        if numeric_stamps:
            assert numeric_snapshot(upgraded) == numeric_before
        assert (
            upgraded.owner != source.owner
            and upgraded.state["database_identity"] != source.state["database_identity"]
        )
        assert upgraded.query("SELECT count(*) FROM device_favorites") == (
            "0" if source_migration == manager.SOURCE_MIGRATION else "1"
        )
        upgraded.stop()
        assert digest(source.path) == source_files and digest(backup) == backup_files

        process, url = launch(upgraded, web)
        favorites = "/core-api/v1/houses/fixture_house/me/device-favorites"
        with httpx.Client(base_url=url, trust_env=False, timeout=5) as http:
            login(http, upgraded)
            if numeric_stamps:
                assert_no_numeric_replay(http, upgraded, numeric_stamps, numeric_counts)
            assert read_run(http, run_id)["actions"][0] == first_action
            completed = dispatch_scene_action(http, run_id, 1, False)
            assert completed["status"] == "succeeded"
            assert completed["actions"][0] == first_action
            assert http.put(favorites + "/fixture_relay").status_code == 204
            assert http.get(favorites).json() == {"device_ids": ["fixture_relay"]}
            assert (
                http.post(
                    "/core-api/houses/fixture_house/members",
                    json={"user_id": "upgrade-other", "role": "resident"},
                ).status_code
                == 201
            )
            assert (
                http.post(
                    "/core-api/houses", json={"id": "other_home", "name": "Other home"}
                ).status_code
                == 201
            )
            other_favorites = "/core-api/v1/houses/other_home/me/device-favorites"
            assert http.get(other_favorites).json() == {"device_ids": []}
            assert http.put(other_favorites + "/fixture_relay").status_code in (
                404,
                422,
            )
            with httpx.Client(base_url=url, trust_env=False, timeout=5) as other:
                response = other.post(
                    "/core-api/auth/login",
                    json={
                        "email": "other@example.test",
                        "password": source.state["password"],
                    },
                )
                assert response.status_code == 200
                other.headers["Authorization"] = (
                    "Bearer " + response.json()["access_token"]
                )
                assert other.get(favorites).json() == {"device_ids": []}
                assert other.get(other_favorites).status_code == 404
            command(http, True)
        halt(process)
        process = None
        process, url = launch(upgraded, web)
        with httpx.Client(base_url=url, trust_env=False, timeout=5) as http:
            login(http, upgraded)
            assert http.get(favorites).json() == {"device_ids": ["fixture_relay"]}
            assert read_run(http, run_id) == completed
            command(http, False)
        halt(process)
        process = None
        with upgraded.lock():
            new_backup = upgraded.backup(root / "new-backup")
        marker_failure_cases(new_backup, root, web, docker, profiles)
        restored = manager.restore(new_backup, root / "restored")
        profiles.append(restored)
        process, url = launch(restored, web)
        with httpx.Client(base_url=url, trust_env=False, timeout=5) as http:
            login(http, restored)
            assert read_run(http, run_id) == completed
            assert http.get(favorites).json() == {"device_ids": ["fixture_relay"]}
        halt(process)
        process = None

        # Original remains byte-for-byte unchanged until an explicit old-runtime restart.
        assert digest(source.path) == source_files and digest(backup) == backup_files
        source.pg()
        assert manager.legacy_fingerprints(source, source_migration) == original_rows
        source.stop()
        process, url = launch(source, old_web, old_core)
        with httpx.Client(base_url=url, trust_env=False, timeout=5) as http:
            login(http, source)
            assert (
                http.get("/core-api/houses/fixture_house").json()["name"]
                == "Keep original home"
            )
            assert read_run(http, run_id)["actions"][0] == first_action
            command(http, False)
        halt(process)
        process = None
        source.pg()
        assert (
            source.query("SELECT version_num FROM alembic_version") == source_migration
        )
        assert source.query(
            "SELECT count(*) FROM pg_tables WHERE schemaname='public' AND tablename='device_favorites'"
        ) == ("0" if source_migration == manager.SOURCE_MIGRATION else "1")
        source.stop()
        assert digest(backup) == backup_files
        print(
            f"PASS upgrade: exact supported source backup, all legacy rows preserved, {source_migration}->{manager.EXPECTED_MIGRATION}, scene ON/restart/explicit OFF, per-user/home favorites, restart/new backup restore, original old-checkout rollback"
        )
        print(
            "PASS upgrade refusal: active backup, pins, missing identity, corrupt manifest, symlink, existing target, ordinary cross-version restore, wrong revision, partial restore unstartable"
        )
    finally:
        if process is not None:
            halt(process)
        for name in (
            "partial",
            "wrong-target",
            "migration-failed",
            "upgraded",
            "restored",
        ):
            path = root / name
            if (path / "profile.json").is_file() and all(
                p.path != path for p in profiles
            ):
                profiles.append(manager.Profile(path, docker))
        if (root / "source" / "profile.json").is_file() and all(
            p.path != root / "source" for p in profiles
        ):
            profiles.append(old.Profile(root / "source", docker))
        for profile in reversed(profiles):
            cleanup(profile, root)
        shutil.rmtree(root)
        assert inventory(docker) == before
        print(
            "PASS upgrade cleanup: original Docker inventory unchanged; no private backup artifacts uploaded"
        )


if __name__ == "__main__":
    main()
