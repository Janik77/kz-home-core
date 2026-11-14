"""Opt-in disposable CI upgrade acceptance. No user profiles or backup artifacts."""

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
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
    assert (result.returncode == 0) is success, "Profile CLI returned unexpected result"


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
            == "0012_server_scene_execution"
        )
        profile.stop()
    print(
        f"PASS {prefix} marker faults: before/after seeded and pending completion write; no initialized, start refused, services stopped"
    )


def main():
    assert os.environ.get("GITHUB_ACTIONS") == "true"
    web, old_core, old_web = map(lambda p: Path(p).resolve(), sys.argv[1:])
    manager.validate_source_checkout(old_core)
    spec = importlib.util.spec_from_file_location(
        "old_profile", old_core / "tools/durable_software.py"
    )
    old = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(old)
    docker = manager.Docker()
    before = inventory(docker)
    root = Path(tempfile.mkdtemp(prefix="kzsoft-durable-test-")).resolve()
    process = None
    profiles = []
    try:
        cli(old_core, "init", "--state", root / "source")
        source = old.Profile(root / "source", docker)
        profiles.append(source)
        process, url = launch(source, old_web, old_core)
        with httpx.Client(base_url=url, trust_env=False, timeout=5) as http:
            login(http, source)
            assert (
                http.patch(
                    "/core-api/houses/fixture_house",
                    json={"name": "Keep original home"},
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
        original_rows = manager.legacy_fingerprints(source)
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
        profiles.append(manager.Profile(partial, docker))

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
        source.query("UPDATE alembic_version SET version_num='0010_scene_runs'")
        source.stop()
        wrong = variant(backup, root / "wrong-revision", dump=wrong_dump)
        attempt(wrong, root / "wrong-target")
        profiles.append(manager.Profile(root / "wrong-target", docker))
        assert not (root / "wrong-target" / "initialized").exists()

        # Simulate failure after the actual additive migration, before initialization.
        original_migrate = manager.migrate_upgrade

        def fail_after_migration(profile):
            original_migrate(profile)
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
            == "0012_server_scene_execution"
        )
        assert manager.legacy_fingerprints(failed_profile) == original_rows
        failed_profile.stop()

        marker_failure_cases(backup, root, web, docker, profiles, old_core=old_core)

        attempt(backup, root / "upgraded", success=True)
        upgraded = manager.Profile(root / "upgraded", docker)
        profiles.append(upgraded)
        upgraded.pg()
        assert (
            upgraded.query("SELECT version_num FROM alembic_version")
            == "0012_server_scene_execution"
        )
        assert manager.legacy_fingerprints(upgraded) == original_rows
        assert (
            upgraded.owner != source.owner
            and upgraded.state["database_identity"] != source.state["database_identity"]
        )
        assert upgraded.query("SELECT count(*) FROM device_favorites") == "0"
        upgraded.stop()
        assert digest(source.path) == source_files and digest(backup) == backup_files

        process, url = launch(upgraded, web)
        favorites = "/core-api/v1/houses/fixture_house/me/device-favorites"
        with httpx.Client(base_url=url, trust_env=False, timeout=5) as http:
            login(http, upgraded)
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
        assert manager.legacy_fingerprints(source) == original_rows
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
            source.query("SELECT version_num FROM alembic_version") == "0010_scene_runs"
        )
        assert (
            source.query(
                "SELECT count(*) FROM pg_tables WHERE tablename='device_favorites'"
            )
            == "0"
        )
        source.stop()
        assert digest(backup) == backup_files
        print(
            "PASS upgrade: exact #42 backup, all legacy rows preserved, 0010->0011, scene ON/restart/explicit OFF, per-user/home favorites, restart/new backup restore, original old-checkout rollback"
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
