"""Opt-in owned Windows demo acceptance. Retains all test volumes and backups."""

import json
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.request
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from deploy.demo import Demo  # noqa: E402
from deploy.demo_upgrade import PRIVATE, digest  # noqa: E402
from test_house_permissions_live import inventory  # noqa: E402


def port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def main():
    web = Path(sys.argv[1]).resolve()
    before = inventory()
    demo = Demo("kzhome-demo-check-v020" + uuid4().hex[:10])
    print("Owned acceptance project: " + demo.project, flush=True)
    simulator = None
    try:
        demo.prepare(web, "linux/amd64", port(), port())
        demo.start(build=True)
        original = {name: digest(demo.path / name) for name in PRIVATE}
        credentials = json.loads((demo.path / "user.json").read_text())
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        base = "http://127.0.0.1:" + demo.state["env"]["DEMO_WEB_PORT"] + "/core-api"

        def api(path, body=None, token=None):
            request = urllib.request.Request(
                base + path,
                data=json.dumps(body).encode() if body is not None else None,
                headers={
                    "Content-Type": "application/json",
                    **({"Authorization": "Bearer " + token} if token else {}),
                },
            )
            with opener.open(request, timeout=10) as response:
                return json.load(response) if response.status != 204 else None

        pair = api("/auth/login", credentials)
        token = pair["access_token"]
        assert (
            "device.control"
            in api("/v1/houses/e2e_house/me/permissions", token=token)["permissions"]
        )
        api(
            "/rooms",
            {
                "id": "preserved_room",
                "floor_id": "e2e_floor",
                "name": "Preserved extra room",
            },
            token,
        )
        api("/auth/logout", {"refresh_token": pair["refresh_token"]})

        def sql(query):
            return demo.dc(
                "exec",
                "-T",
                "postgres",
                "psql",
                "-U",
                "postgres",
                "-d",
                "kzhome",
                "-Atc",
                query,
            )

        business = "SELECT id,password_hash FROM users ORDER BY id; SELECT id,name FROM houses ORDER BY id; SELECT id,name FROM rooms ORDER BY id; SELECT id,actions FROM scenes ORDER BY id; SELECT id,state FROM devices ORDER BY id"
        saved = sql(business)
        # Emulate a legacy prepared DB only in this NEW fixture: no commands exist.
        # The downgrade removes the empty new ledger, not user/business rows.
        assert sql("SELECT count(*) FROM device_commands") == "0"
        demo.dc("stop", "proxy", "core")
        demo.dc(
            "run",
            "--rm",
            "--no-deps",
            "-T",
            "core",
            "python",
            "-m",
            "alembic",
            "downgrade",
            "0005_device_onboarding",
        )
        assert (
            sql("SELECT version_num FROM alembic_version") == "0005_device_onboarding"
        )
        demo.upgrade_check(web)
        dc = demo.dc
        interrupted = False

        def interrupt(*args, **kwargs):
            nonlocal interrupted
            result = dc(*args, **kwargs)
            if "alembic" in args and not interrupted:
                interrupted = True
                raise RuntimeError("Acceptance interruption after migration committed")
            return result

        demo.dc = interrupt
        try:
            demo.upgrade(web)
            raise AssertionError("Interruption not exercised")
        except RuntimeError:
            assert (demo.path / "upgrade.pending.json").exists()
        demo.dc = dc
        demo.upgrade(web)
        assert sql(business) == saved
        assert original == {name: digest(demo.path / name) for name in PRIVATE}
        current = {
            n: (demo.path / n).read_bytes() for n in ("state.json", "compose.env")
        }
        demo.upgrade(web)
        assert current == {n: (demo.path / n).read_bytes() for n in current}
        print(
            "PASS migration interruption/resume, repeated upgrade, business data/password/certificate preservation",
            flush=True,
        )
        for mode in ("apply", "fail", "timeout", "offline"):
            if mode != "offline":
                simulator = subprocess.Popen(
                    [
                        sys.executable,
                        "-u",
                        "deploy/demo.py",
                        "virtual-relay",
                        "--run",
                        "--project",
                        demo.project,
                        "--relay-mode",
                        mode,
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
            pair = api("/auth/login", credentials)
            for _ in range(150):
                device = next(
                    d
                    for d in api(
                        "/devices?house_id=e2e_house", token=pair["access_token"]
                    )
                    if d["id"] == "e2e_relay"
                )
                if device["online"] == (mode != "offline"):
                    break
                time.sleep(0.1)
            else:
                raise RuntimeError("Presence prerequisite failed")
            api("/auth/logout", {"refresh_token": pair["refresh_token"]})
            result = subprocess.run(
                [
                    "node",
                    "tests/demo_control_browser.mjs",
                    demo.project,
                    str(web),
                    mode,
                ],
                timeout=90,
            )
            assert result.returncode == 0, "Browser acceptance failed"
            if simulator is not None:
                demo.virtual_relay_stop()
                simulator.communicate(timeout=20)
                simulator = None
        assert sql(
            "SELECT status,count(*) FROM device_commands GROUP BY status ORDER BY status"
        ).splitlines() == ["applied|3", "failed|1", "timed_out|1"]
        assert (
            sql(
                "SELECT count(*) FROM device_commands WHERE status='applied' AND (ack_status IS DISTINCT FROM 'applied' OR state_received_at IS NULL OR reported_on IS DISTINCT FROM requested_on)"
            )
            == "0"
        )
        saved = sql(business)
        demo.dc("stop")
        demo.dc("start", "postgres", "mosquitto", "core", "proxy")
        for _ in range(100):
            try:
                demo.verify()
                break
            except Exception:
                time.sleep(0.2)
        assert sql(business) == saved
        print(
            "PASS SQL applied3/failed1/timed_out1; business state survived restart",
            flush=True,
        )
    finally:
        if simulator is not None:
            demo.virtual_relay_stop()
            simulator.communicate(timeout=20)
        if demo.state is not None:
            demo.dc("down", "--remove-orphans")  # Intentionally NO --volumes.
        after = inventory()
        assert all(
            after["containers"].get(k) == v for k, v in before["containers"].items()
        )
        assert all(
            set(before[k]).issubset(after[k]) for k in ("volumes", "images", "networks")
        )
        print(
            "PASS existing resources preserved; owned volumes/private backups retained: "
            + demo.project,
            flush=True,
        )


if __name__ == "__main__":
    main()
