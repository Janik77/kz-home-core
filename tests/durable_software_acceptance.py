"""Opt-in CI acceptance for the durable software profile; never touches installed data."""

import os
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
from tools.durable_software import CORE, Docker, Profile, initialize, restore  # noqa: E402


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


def launch(profile, web):
    process = subprocess.Popen(
        [
            sys.executable,
            "-B",
            str(CORE / "tools/durable_software.py"),
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
                    raise AssertionError(f"Owned durable launcher exited: {process.returncode}")
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


def snapshot(http):
    paths = ("/houses/fixture_house", "/rooms/saved_room", "/scenes/saved_scene")
    values = {}
    for path in paths:
        response = http.get("/core-api" + path)
        assert response.status_code == 200
        values[path] = response.json()
    return values


def cleanup(profile, root):
    # Test-created profiles only, in this test's fresh temp root; absent from public CLI.
    assert os.environ.get("GITHUB_ACTIONS") == "true"
    assert profile.path.parent == root and root.name.startswith("kzsoft-durable-test-")
    for suffix in ("-mqtt", "-pg"):
        name = profile.name + suffix
        if profile.owned("container", name):
            profile.docker.run("rm", "-f", name)
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
                json={"name": "Persistent house settings"},
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
                        }
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
            expected = snapshot(http)
            command(http, True)
            command(http, False)
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
            command(http, True)
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
        process, url = launch(restored, web)
        with httpx.Client(base_url=url, trust_env=False, timeout=5) as http:
            login(http, restored)
            assert snapshot(http) == expected
            command(http, False)
            response = http.patch(
                "/core-api/rooms/saved_room", json={"name": "Restored copy only"}
            )
            assert response.status_code == 200
        halt(process)
        process = None
        process, url = launch(source, web)
        with httpx.Client(base_url=url, trust_env=False, timeout=5) as http:
            login(http, source)
            assert snapshot(http) == expected, "Restore modified source"
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
        print(
            "PASS durable: heartbeat, software ACK/state, restart, settings/room/scene revisions, "
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
