"""Opt-in CI acceptance for the durable software profile; never touches installed data."""

import os
import json
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from uuid import uuid4

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.durable_software import (
    CORE,
    BROKER_FILES,
    EXPECTED_MIGRATION,
    Docker,
    Profile,
    initialize,
    restore,
)  # noqa: E402


def inventory(docker):
    return {
        kind: sorted(
            docker.text(
                kind,
                "ls",
                *extra,
                "--format",
                "{{.ID}}" if kind != "volume" else "{{.Name}}",
            ).splitlines()
        )
        for kind, extra in (
            ("container", ["-a"]),
            ("volume", []),
            ("network", []),
            ("image", []),
        )
    }


def launch(profile, web, core=CORE):
    process = subprocess.Popen(
        [
            sys.executable,
            "-B",
            str(core / "tools/durable_software.py"),
            "start",
            "--state",
            str(profile.path),
            "--web-root",
            str(web),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        env={
            key: value
            for key, value in os.environ.items()
            if key
            in (
                "PATH",
                "HOME",
                "GITHUB_ACTIONS",
                "DOCKER_CONTEXT",
                "LD_LIBRARY_PATH",
                "PYTHONDONTWRITEBYTECODE",
            )
        },
    )
    try:
        deadline = time.monotonic() + 60
        url = f"http://127.0.0.1:{profile.state['web_port']}"
        with httpx.Client(base_url=url, trust_env=False, timeout=3) as http:
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    diagnostic = process.stderr.read().decode(errors="replace")
                    for line in diagnostic.splitlines():
                        if line.startswith("PROFILE_FAILURE "):
                            print(line, flush=True)
                    raise AssertionError(
                        f"Owned durable launcher exited: {process.returncode}"
                    )
                try:
                    if http.get("/core-api/ready").status_code == 200:
                        login(http, profile)
                        if (
                            http.get("/core-api/devices/fixture_relay").status_code
                            == 200
                        ):
                            return process, url
                except httpx.TransportError:
                    pass
                time.sleep(0.2)
        raise AssertionError("Owned durable launcher readiness timeout")
    except BaseException:
        if process.poll() is None:
            halt(process)
        raise


def halt(process):
    if process.poll() is None:
        process.send_signal(signal.SIGINT)
    process.wait(timeout=25)
    assert process.returncode == 0


def login(http, profile):
    result = http.post(
        "/core-api/auth/login",
        json={"email": "owner@example.test", "password": profile.state["password"]},
    )
    assert result.status_code == 200
    http.headers["Authorization"] = "Bearer " + result.json()["access_token"]


def command(http, on):
    result = http.post(
        "/core-api/v1/devices/fixture_relay/commands",
        headers={"Idempotency-Key": str(uuid4())},
        json={"on": on},
    )
    assert result.status_code in (200, 202), result.status_code
    identifier = result.json()["command_id"]
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        result = http.get("/core-api/v1/houses/fixture_house/commands/" + identifier)
        assert result.status_code == 200
        receipt = result.json()
        if receipt["status"] == "applied":
            assert receipt["ack_status"] == "applied" and receipt["reported_on"] is on
            return
        assert receipt["status"] in ("queued", "dispatched")
        time.sleep(0.1)
    raise AssertionError("Software command did not complete")


def read_run(http, run_id):
    response = http.get("/core-api/v1/houses/fixture_house/scene-runs/" + run_id)
    assert response.status_code == 200
    return response.json()


def dispatch_scene_action(http, run_id, position, on):
    response = http.post(
        f"/core-api/v1/houses/fixture_house/scene-runs/{run_id}/actions/{position}/dispatch"
    )
    assert response.status_code == 202
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        run = read_run(http, run_id)
        action = run["actions"][position]
        if action["status"] == "applied":
            receipt = action["receipt"]
            assert receipt["status"] == "applied"
            assert receipt["ack_status"] == "applied" and receipt["reported_on"] is on
            return run
        assert action["status"] in ("queued", "dispatched", "dispatch_unknown")
        time.sleep(0.1)
    raise AssertionError("Scene action lacks confirmed ACK/state")


def snapshot(http):
    paths = (
        "/houses/fixture_house",
        "/rooms/saved_room",
        "/scenes/saved_scene",
        "/automations/saved_automation",
        "/scenes/saved_draft_copy",
        "/automations/saved_disabled_copy",
        "/v1/houses/fixture_house/me/device-favorites",
    )
    values = {}
    for path in paths:
        response = http.get("/core-api" + path)
        assert response.status_code == 200
        values[path] = response.json()
    return values


def assert_inert_copies(http, profile):
    before = profile.query("SELECT count(*) FROM device_commands")
    scene = http.get("/core-api/scenes/saved_draft_copy").json()
    assert scene["is_draft"] and scene["revision"] == 1
    rule = http.get("/core-api/automations/saved_disabled_copy").json()
    assert not rule["enabled"] and rule["revision"] == 1
    assert http.get("/core-api/automations/saved_disabled_copy/runs").json() == []
    assert (
        profile.query(
            "SELECT count(*) FROM automations WHERE id='saved_disabled_copy' AND authorized_by IS NULL AND threshold_state::text='{}' AND schedule_state::text='{}'"
        )
        == "1"
    )
    for path in (
        "/core-api/scenes/saved_draft_copy/run",
        "/core-api/v1/houses/fixture_house/scene-runs",
        "/core-api/v1/houses/fixture_house/scene-runs/execute",
    ):
        result = http.post(
            path,
            headers={"Idempotency-Key": uuid4().hex},
            json={"scene_id": "saved_draft_copy", "expected_revision": 1},
        )
        assert result.status_code == 409 and result.json()["code"] == "scene_draft", (
            result.text
        )
    assert (
        profile.query(
            "SELECT count(*) FROM scene_runs WHERE scene_id IN ('saved_draft_copy','saved_disabled_copy')"
        )
        == "0"
    )
    assert profile.query("SELECT count(*) FROM device_commands") == before


def cleanup(profile, root):
    # Test-created profiles only, in this test's fresh temp root; absent from public CLI.
    assert os.environ.get("GITHUB_ACTIONS") == "true"
    assert profile.path.parent == root and root.name.startswith("kzsoft-durable-test-")
    for suffix in ("-mqtt", "-pg"):
        name = profile.name + suffix
        if profile.owned("container", name):
            profile.docker.run("rm", "-f", "-v", name)
    if profile.owned("volume", profile.name + "-pgdata"):
        profile.docker.run("volume", "rm", profile.name + "-pgdata")
    if profile.owned("network", profile.name):
        profile.docker.run("network", "rm", profile.name)


def main():
    assert os.environ.get("GITHUB_ACTIONS") == "true", (
        "Run acceptance only on a disposable CI runner"
    )
    web = Path(sys.argv[1]).resolve()
    docker = Docker()
    before = inventory(docker)
    root = Path(tempfile.mkdtemp(prefix="kzsoft-durable-test-")).resolve()
    profiles = []
    process = None
    try:
        source = initialize(root / "source")
        profiles.append(source)
        process, url = launch(source, web)
        with httpx.Client(base_url=url, trust_env=False, timeout=5) as http:
            login(http, source)
            assert http.get("/").status_code == 200
            response = http.patch(
                "/core-api/houses/fixture_house",
                json={
                    "name": "Persistent house settings",
                    "expected_metadata_revision": http.get(
                        "/core-api/houses/fixture_house"
                    ).json()["metadata_revision"],
                },
            )
            assert response.status_code == 200
            response = http.post(
                "/core-api/rooms",
                json={
                    "id": "saved_room",
                    "floor_id": "fixture_floor",
                    "name": "Persistent room",
                },
            )
            assert response.status_code == 201
            metadata_expected = {}
            for metadata_path, patch in (
                ("/houses/fixture_house", {"name": "Persistent house settings"}),
                ("/floors/fixture_floor", {"name": "Persistent floor"}),
                ("/rooms/saved_room", {"name": "Persistent room"}),
                (
                    "/devices/fixture_relay",
                    {"name": "Persistent relay", "room_id": "saved_room"},
                ),
            ):
                original = http.get("/core-api" + metadata_path).json()
                saved = http.patch(
                    "/core-api" + metadata_path,
                    json={
                        **patch,
                        "expected_metadata_revision": original["metadata_revision"],
                    },
                )
                assert saved.status_code == 200, saved.text
                assert (
                    saved.json()["metadata_revision"]
                    == original["metadata_revision"] + 1
                )
                metadata_expected[metadata_path] = {
                    key: value
                    for key, value in saved.json().items()
                    if key
                    in {
                        "name",
                        "house_id",
                        "floor_id",
                        "room_id",
                        "order",
                        "icon",
                        "metadata_revision",
                    }
                }
            assert source.query("SELECT count(*) FROM device_commands") == "0"
            response = http.post(
                "/core-api/scenes",
                json={
                    "id": "saved_scene",
                    "house_id": "fixture_house",
                    "name": "Persistent scene",
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
            revision = response.json()["revision"]
            response = http.patch(
                "/core-api/scenes/saved_scene",
                json={"expected_revision": revision, "name": "Saved revision"},
            )
            assert response.status_code == 200
            favorite_path = "/core-api/v1/houses/fixture_house/me/device-favorites"
            before_commands = http.get("/core-api/devices/fixture_relay").json()[
                "state"
            ]
            assert http.put(favorite_path + "/fixture_relay").status_code == 204
            assert http.put(favorite_path + "/fixture_relay").status_code == 204
            assert http.get(favorite_path).json() == {"device_ids": ["fixture_relay"]}
            assert (
                http.get("/core-api/devices/fixture_relay").json()["state"]
                == before_commands
            )
            response = http.post(
                "/core-api/automations",
                json={
                    "id": "saved_automation",
                    "name": "Persistent software rule",
                    "house_id": "fixture_house",
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
            assert response.status_code == 201, response.text
            for kind, original, copied in (
                ("scenes", "saved_scene", "saved_draft_copy"),
                ("automations", "saved_automation", "saved_disabled_copy"),
            ):
                source_definition = http.get(f"/core-api/{kind}/{original}").json()
                before_copy = source.query("SELECT count(*) FROM device_commands")
                copy = http.post(
                    f"/core-api/{kind}/{original}/duplicate",
                    json={
                        "id": copied,
                        "name": "Preserved non-executable copy",
                        "expected_revision": source_definition["revision"],
                    },
                )
                assert copy.status_code == 201, copy.text
                assert (
                    source.query("SELECT count(*) FROM device_commands") == before_copy
                )
                assert (
                    http.get(f"/core-api/{kind}/{original}").json() == source_definition
                )
            assert_inert_copies(http, source)
            expected = snapshot(http)
            command(http, True)
            command(http, False)
            created = http.post(
                "/core-api/v1/houses/fixture_house/scene-runs",
                headers={"Idempotency-Key": uuid4().hex},
                json={
                    "scene_id": "saved_scene",
                    "expected_revision": expected["/scenes/saved_scene"]["revision"],
                },
            )
            assert created.status_code == 201
            run_id = created.json()["id"]
            first_action = dispatch_scene_action(http, run_id, 0, True)["actions"][0]
            # Long-running launcher must emit periodic status, not only startup status.
            prior = http.get("/core-api/devices/fixture_relay").json()["metadata"][
                "last_seen"
            ]
            time.sleep(65)
            current = http.get("/core-api/devices/fixture_relay").json()
            assert current["online"] and current["metadata"]["last_seen"] != prior
            attempt = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    str(CORE / "tools/durable_software.py"),
                    "backup",
                    "--state",
                    str(source.path),
                    "--destination",
                    str(root / "blocked"),
                ],
                capture_output=True,
                timeout=20,
            )
            assert attempt.returncode != 0 and not (root / "blocked").exists()

        # A copied directory has an independent lock but must not control this owner.
        copied = root / "copied"
        shutil.copytree(source.path, copied)
        original_started = source.owned("container", source.name + "-pg")["State"][
            "StartedAt"
        ]
        try:
            Profile(copied, docker)
        except ValueError:
            pass
        else:
            raise AssertionError("Copied directory adopted original profile")
        metadata = json.loads((copied / "profile.json").read_text())
        metadata["profile_path"] = str(copied)
        metadata["directory_identity"] = (
            f"{copied.stat().st_dev}:{copied.stat().st_ino}"
        )
        (copied / "profile.json").write_text(json.dumps(metadata))
        copy_profile = Profile(copied, docker)
        try:
            with copy_profile.lock():
                copy_profile.stop()
        except ValueError:
            pass
        else:
            raise AssertionError("Edited copy bypassed resource path binding")
        assert (
            source.owned("container", source.name + "-pg")["State"]["StartedAt"]
            == original_started
        )
        broker = source.owned("container", source.name + "-mqtt")
        binds = [item for item in broker["Mounts"] if item["Type"] == "bind"]
        assert {item["Source"] for item in binds} == {
            str(source.path / filename) for filename in BROKER_FILES
        }
        assert all(not item["RW"] for item in binds)
        assert not any(
            item["Source"]
            in (
                str(source.path),
                str(source.path / "ca.key"),
                str(source.path / "profile.json"),
            )
            for item in binds
        )
        first_start = source.owned("container", source.name + "-pg")["State"][
            "StartedAt"
        ]
        halt(process)
        process = None
        process, url = launch(source, web)
        assert (
            source.owned("container", source.name + "-pg")["State"]["StartedAt"]
            != first_start
        )
        with httpx.Client(base_url=url, trust_env=False, timeout=5) as http:
            login(http, source)
            assert snapshot(http) == expected
            assert_inert_copies(http, source)
            recovered = read_run(http, run_id)
            assert recovered["actions"][0] == first_action
            assert recovered["actions"][1]["status"] == "ready"
            completed_run = dispatch_scene_action(http, run_id, 1, False)
            assert completed_run["status"] == "succeeded"
            assert completed_run["actions"][0] == first_action
            assert (
                first_action["receipt"]["command_id"]
                != completed_run["actions"][1]["receipt"]["command_id"]
            )
            response = http.patch(
                "/core-api/automations/saved_automation",
                json={"expected_revision": 1, "enabled": True},
            )
            assert response.status_code == 200, response.text
            # A real manual relay command supplies an unrelated MQTT state event;
            # the enabled rule then dispatches OFF through the confirmed executor.
            command(http, True)
            until = time.monotonic() + 20
            while time.monotonic() < until:
                response = http.get("/core-api/automations/saved_automation/runs")
                assert response.status_code == 200, response.text
                automation_history = response.json()
                if (
                    automation_history
                    and automation_history[0]["status"] == "succeeded"
                    and source.query(
                        "SELECT count(*) FROM scene_runs WHERE source_kind='automation' AND scene_id='saved_automation' AND execution_finished IS TRUE"
                    )
                    == "1"
                ):
                    break
                time.sleep(0.1)
            else:
                raise AssertionError("Durable automation lacks confirmed ACK/state")
            assert len(automation_history) == 1
            action = automation_history[0]["actions"][0]
            assert action["status"] == "applied" and action["requested_on"] is False
            assert action["receipt"]["ack_status"] == "applied"
            assert action["receipt"]["reported_on"] is False
            assert action["receipt"]["state_received_at"]
            response = http.patch(
                "/core-api/automations/saved_automation",
                json={"expected_revision": 2, "enabled": False},
            )
            assert response.status_code == 200, response.text
            expected = snapshot(http)
            expected_command_count = source.query(
                "SELECT count(*) FROM device_commands"
            )
            assert (
                source.query(
                    "SELECT count(*) FROM scene_run_receipts l JOIN scene_runs r ON r.id=l.run_id WHERE r.source_kind='automation'"
                )
                == "1"
            )
        halt(process)
        process = None
        with source.lock():
            backup = source.backup(root / "backup")
        assert (
            source.owned("container", source.name + "-pg")["State"]["Running"] is False
        )
        # Existing destination must never be overwritten.
        try:
            restore(backup, source.path)
        except FileExistsError:
            pass
        else:
            raise AssertionError("Restore accepted an existing profile")
        restored = restore(backup, root / "restored")
        profiles.append(restored)
        assert restored.owner != source.owner
        original_name = source.name
        foreign_before = inventory(docker)
        try:
            source.name = restored.name
            try:
                with source.lock():
                    source.backup(root / "foreign-volume")
            except ValueError:
                pass
            else:
                raise AssertionError("Foreign volume adopted")
        finally:
            source.name = original_name
        assert not (root / "foreign-volume").exists()
        assert inventory(docker) == foreign_before
        process, url = launch(restored, web)
        with httpx.Client(base_url=url, trust_env=False, timeout=5) as http:
            login(http, restored)
            assert snapshot(http) == expected
            for metadata_path, preserved in metadata_expected.items():
                actual = http.get("/core-api" + metadata_path).json()
                assert {key: actual[key] for key in preserved} == preserved
                stale = http.patch(
                    "/core-api" + metadata_path,
                    json={
                        "name": "must not replace restored data",
                        "expected_metadata_revision": preserved["metadata_revision"]
                        - 1,
                    },
                )
                assert (
                    stale.status_code == 409
                    and stale.json()["code"] == "metadata_revision_conflict"
                )
                assert (
                    http.patch(
                        "/core-api" + metadata_path, json={"name": "legacy writer"}
                    ).status_code
                    == 422
                )
            print(
                "PASS metadata PG/TLS: all four revisions and names/move preserved; stale/legacy writes refused; zero metadata commands"
            )
            assert (
                http.get("/core-api/automations/saved_automation/runs").json()
                == automation_history
            )
            assert (
                restored.query("SELECT count(*) FROM device_commands")
                == expected_command_count
            )
            assert (
                restored.query(
                    "SELECT count(*) FROM scene_run_receipts l JOIN scene_runs r ON r.id=l.run_id WHERE r.source_kind='automation'"
                )
                == "1"
            )
            assert_inert_copies(http, restored)
            before_activation = restored.query("SELECT count(*) FROM device_commands")
            edited = http.patch(
                "/core-api/scenes/saved_draft_copy",
                json={
                    "expected_revision": 1,
                    "actions": [
                        {
                            "type": "device_state",
                            "device_id": "fixture_relay",
                            "state": {"on": False},
                        }
                    ],
                },
            )
            assert edited.status_code == 200 and edited.json()["is_draft"]
            activated = http.post(
                "/core-api/scenes/saved_draft_copy/activate",
                json={"expected_revision": 2},
            )
            assert activated.status_code == 200 and not activated.json()["is_draft"]
            assert (
                restored.query("SELECT count(*) FROM device_commands")
                == before_activation
            )
            assert (
                restored.query(
                    "SELECT count(*) FROM scene_runs WHERE scene_id='saved_draft_copy'"
                )
                == "0"
            )
            explicit = http.post(
                "/core-api/v1/houses/fixture_house/scene-runs",
                headers={"Idempotency-Key": uuid4().hex},
                json={"scene_id": "saved_draft_copy", "expected_revision": 3},
            )
            assert explicit.status_code == 201
            assert (
                dispatch_scene_action(http, explicit.json()["id"], 0, False)["status"]
                == "succeeded"
            )
            assert (
                int(restored.query("SELECT count(*) FROM device_commands"))
                == int(before_activation) + 1
            )
            print(
                "PASS copied definitions PG/TLS: draft+disabled copy preserved across restart/backup/restore; zero copied intent; activation zero commands; explicit run ACK/state; source untouched"
            )
            favorite_path = "/core-api/v1/houses/fixture_house/me/device-favorites"
            assert http.delete(favorite_path + "/fixture_relay").status_code == 204
            assert http.get(favorite_path).json() == {"device_ids": []}
            assert read_run(http, run_id) == completed_run
            command(http, False)
            response = http.patch(
                "/core-api/rooms/saved_room",
                json={
                    "name": "Restored copy only",
                    "expected_metadata_revision": http.get(
                        "/core-api/rooms/saved_room"
                    ).json()["metadata_revision"],
                },
            )
            assert response.status_code == 200
        halt(process)
        process = None
        process, url = launch(source, web)
        with httpx.Client(base_url=url, trust_env=False, timeout=5) as http:
            login(http, source)
            assert snapshot(http) == expected, "Restore modified source"
            assert_inert_copies(http, source)
            assert (
                http.get("/core-api/automations/saved_automation/runs").json()
                == automation_history
            )
            assert (
                source.query("SELECT count(*) FROM device_commands")
                == expected_command_count
            )
            print(
                "PASS automation durable: disabled rule survives restart; real MQTT trigger ACK/state result and exact receipt history survive backup/restore and source restart; no replay; rooms/scenes/favorites unchanged",
                flush=True,
            )
        halt(process)
        process = None
        # Corrupt backup rejected before creating resources or destination.
        with (backup / "database.dump").open("ab") as stream:
            stream.write(b"corruption-test")
        try:
            restore(backup, root / "corrupt")
        except ValueError:
            pass
        else:
            raise AssertionError("Corrupt backup accepted")
        assert not (root / "corrupt").exists()
        for profile in profiles:
            profile.pg()
            assert (
                profile.docker.text(
                    "exec",
                    profile.name + "-pg",
                    "psql",
                    "-U",
                    "lab",
                    "-d",
                    "lab",
                    "-Atc",
                    "SELECT count(*) FROM physical_devices",
                )
                == "0"
            )
            profile.stop()

        # Wrong migration and cluster identity must fail before any backup artifact.
        restored.pg()
        restored.query("UPDATE alembic_version SET version_num='wrong_revision'")
        try:
            with restored.lock():
                restored.backup(root / "wrong-migration")
        except ValueError:
            pass
        else:
            raise AssertionError("Wrong migration accepted")
        assert not (root / "wrong-migration").exists()
        restored._open_pg()
        restored.query(
            "UPDATE alembic_version SET version_num='" + EXPECTED_MIGRATION + "'"
        )
        expected_identity = restored.state["database_identity"]
        restored.state["database_identity"] = "wrong-cluster"
        try:
            with restored.lock():
                restored.backup(root / "wrong-identity")
        except ValueError:
            pass
        else:
            raise AssertionError("Wrong database identity accepted")
        assert not (root / "wrong-identity").exists()
        restored.state["database_identity"] = expected_identity
        restored.stop()
        # Delete ONLY the restored test profile's labelled storage to reproduce loss.
        assert restored.owned("container", restored.name + "-pg")
        restored.docker.run("rm", "-v", restored.name + "-pg")
        assert restored.owned("volume", restored.name + "-pgdata")
        restored.docker.run("volume", "rm", restored.name + "-pgdata")
        after_loss = inventory(docker)
        for operation in (
            lambda: restored.pg(),
            lambda: restored.backup(root / "missing-volume"),
        ):
            try:
                with restored.lock():
                    operation()
            except ValueError:
                pass
            else:
                raise AssertionError("Lost volume silently recreated")
        assert not (root / "missing-volume").exists()
        assert inventory(docker) == after_loss
        print(
            "PASS fail-closed: missing volume not recreated, no backup artifact; wrong migration/identity rejected; copied directory cannot control source; broker private inputs excluded"
        )
        print(
            "PASS durable: scene ON/restart/OFF with linked receipts and restored run history; foreign-volume rejection; heartbeat, software ACK/state, restart, settings/room/scene revisions, "
            "backup/restore, source preservation, active-profile/existing-target/corruption rejection; zero physical inventory"
        )
    finally:
        if process is not None:
            halt(process)
        for path in (root / "source", root / "restored"):
            if (path / "profile.json").is_file() and all(
                p.path != path for p in profiles
            ):
                profiles.append(Profile(path, docker))
        for profile in reversed(profiles):
            cleanup(profile, root)
        shutil.rmtree(root)  # Only the test-created temp root, never a user profile.
        assert inventory(docker) == before, (
            "Docker inventory changed; never delete unrelated resources"
        )
        print(
            "PASS preservation: original Docker container/volume/network/image inventory unchanged"
        )


if __name__ == "__main__":
    main()
