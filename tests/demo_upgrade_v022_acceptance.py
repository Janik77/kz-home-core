"""Opt-in v0.20 -> v0.22 drill. Own fresh worktree/data only; never mentor2.

Uses the real demo helper, PostgreSQL, Mosquitto TLS and compiled Web v0.19.
No helper/runtime patching; only test interruption after a committed migration.
"""

import argparse
import json
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from uuid import uuid4
import zipfile

ROOT = Path(__file__).resolve().parents[1]
OLD = "2930ee9"
TARGET = "e73180a20468dda1b2da318cc26411bee340c4e1"
WEB = "809266f5c0e44f2a2b591c494c62b692429fbce0"


def run(argv, *, cwd=ROOT, **kwargs):
    result = subprocess.run(argv, cwd=cwd, capture_output=True, timeout=1200, **kwargs)
    if result.returncode:
        raise RuntimeError("Acceptance external operation failed (details withheld)")
    return result.stdout


def port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def worker(args):
    checkout = args.checkout.resolve()
    sys.path.insert(0, str(checkout))
    from deploy.demo import Demo, command, private_write
    from deploy.demo_upgrade import PRIVATE, digest

    demo = Demo(args.project)
    if args.phase == "old":
        demo.prepare(args.web, "linux/amd64", port(), port())
        demo.start(build=True)
    else:
        demo.load()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    base = "http://127.0.0.1:" + demo.state["env"]["DEMO_WEB_PORT"] + "/core-api"
    account = json.loads((demo.path / "user.json").read_text())
    relay = None

    def api(path, body=None, token=None, headers=None):
        req = urllib.request.Request(
            base + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers={
                "Content-Type": "application/json",
                **({"Authorization": "Bearer " + token} if token else {}),
                **(headers or {}),
            },
        )
        with opener.open(req, timeout=10) as response:
            return json.load(response) if response.status != 204 else None

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
            "-v",
            "ON_ERROR_STOP=1",
            "-Atc",
            query,
        )

    tables = (
        "users",
        "house_memberships",
        "houses",
        "floors",
        "rooms",
        "devices",
        "scenes",
        "automations",
        "device_commands",
    )

    def business():
        return {
            table: sql(
                f"SELECT COALESCE(jsonb_agg(to_jsonb(t) ORDER BY {('command_id' if table == 'device_commands' else 'id')}),'[]'::jsonb)::text FROM {table} t"
            )
            for table in tables
        }

    def infra():
        result = {}
        for service in ("postgres", "mosquitto"):
            identifier = demo.dc("ps", "-aq", service)
            item = json.loads(command(["docker", "inspect", identifier]))[0]
            result[service] = {
                "id": identifier,
                "image": item["Image"],
                "started": item["State"]["StartedAt"],
                "mounts": [
                    [m["Type"], m.get("Name"), m["Source"], m["Destination"]]
                    for m in item["Mounts"]
                ],
            }
        return result

    def state():
        return {
            "data": business(),
            "private": {name: digest(demo.path / name) for name in PRIVATE},
            "infra": infra(),
            "env": demo.state["env"],
            "web_revision": demo.state["web_revision"],
        }

    token = api("/auth/login", account)["access_token"]

    def device():
        return next(
            d
            for d in api("/devices?house_id=e2e_house", token=token)
            if d["id"] == "e2e_relay"
        )

    def simulator(mode):
        nonlocal relay
        relay = subprocess.Popen(
            [
                sys.executable,
                "-u",
                str(checkout / "deploy/demo.py"),
                "virtual-relay",
                "--run",
                "--project",
                demo.project,
                "--relay-mode",
                mode,
            ],
            cwd=checkout,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for _ in range(200):
            if device()["online"]:
                return
            if relay.poll() is not None:
                raise RuntimeError("Synthetic relay startup failed")
            time.sleep(0.1)
        raise RuntimeError("Synthetic relay presence missing")

    def stop_relay():
        nonlocal relay
        if relay is not None:
            demo.virtual_relay_stop()
            relay.communicate(timeout=20)
            relay = None
            for _ in range(100):
                if not device()["online"]:
                    return
                time.sleep(0.1)
            raise RuntimeError("Graceful offline missing")

    def submit(on, expected=None):
        receipt = api(
            "/v1/devices/e2e_relay/commands",
            {"on": on},
            token,
            {"Idempotency-Key": uuid4().hex},
        )
        if expected:
            for _ in range(350):
                receipt = api(
                    "/v1/houses/e2e_house/commands/" + receipt["command_id"],
                    token=token,
                )
                if receipt["status"] not in ("queued", "dispatched"):
                    break
                time.sleep(0.1)
            assert receipt["status"] == expected
        return receipt

    try:
        if args.phase == "old":
            assert (
                sql("SELECT version_num FROM alembic_version")
                == "0006_command_lifecycle"
            )
            assert demo.state["web_revision"] == WEB
            api(
                "/rooms",
                {
                    "id": "preserved_room",
                    "floor_id": "e2e_floor",
                    "name": "Synthetic preserved room",
                },
                token,
            )
            api(
                "/devices",
                {
                    "id": "preserved_device",
                    "room_id": "preserved_room",
                    "name": "Synthetic preserved device",
                    "type": "relay",
                    "online": False,
                    "capabilities": ["on_off"],
                    "state": {"on": False},
                },
                token,
            )
            sql(
                "INSERT INTO users(id,email,password_hash,is_active,is_superuser,created_at,updated_at) "
                "SELECT 'audit-user','audit-user@example.invalid',password_hash,true,false,now(),now() FROM users LIMIT 1; "
                "INSERT INTO house_memberships(id,user_id,house_id,role,created_at,updated_at) "
                "VALUES ('audit-membership','audit-user','e2e_house','resident',now(),now())"
            )
            simulator("apply")
            submit(True, "applied")
            submit(False, "applied")
            stop_relay()
            simulator("fail")
            submit(True, "failed")
            stop_relay()
            simulator("timeout")
            pending = submit(True)
            stop_relay()
            snapshot = state()
            snapshot["pending"] = pending
            private_write(demo.path / "audit-snapshot.json", json.dumps(snapshot))
            print(
                "PASS v0.20 running: synthetic users/devices, applied/failed history and pending deadline; Web v0.19, old ACL",
                flush=True,
            )
            return

        snapshot = json.loads((demo.path / "audit-snapshot.json").read_text())
        # Source files changed in place; existing prepared files/ports/volumes did not.
        assert snapshot["private"] == {
            name: digest(demo.path / name) for name in PRIVATE
        }
        assert "/control/" not in (demo.path / "mqtt/acl").read_text()
        demo.upgrade_check(args.web)
        directory = demo.backup()
        assert directory.is_relative_to(demo.path / "backups")
        assert infra() == snapshot["infra"]
        dc = demo.dc
        interrupted = False

        def lose_migration_response(*command_args, **kwargs):
            nonlocal interrupted
            output = dc(*command_args, **kwargs)
            if "alembic" in command_args and not interrupted:
                interrupted = True
                raise RuntimeError("Synthetic response loss after committed migration")
            return output

        demo.dc = lose_migration_response
        try:
            demo.upgrade(args.web)
        except RuntimeError:
            assert interrupted and (demo.path / "upgrade.pending.json").exists()
        else:
            raise AssertionError("Recovery injection was not reached")
        demo.dc = dc
        assert (
            sql("SELECT version_num FROM alembic_version") == "0007_control_authority"
        )
        demo.upgrade(args.web)
        assert business() == snapshot["data"]
        assert infra() == snapshot["infra"]
        assert snapshot["private"] == {
            name: digest(demo.path / name) for name in PRIVATE
        }
        mutable = {
            "DEMO_WEB_CONTEXT",
            "DEMO_CORE_IMAGE",
            "DEMO_WEB_IMAGE",
            "CORE_ARM64_IMAGE",
        }
        assert {k: v for k, v in demo.state["env"].items() if k not in mutable} == {
            k: v for k, v in snapshot["env"].items() if k not in mutable
        }
        assert demo.state["web_revision"] == snapshot["web_revision"] == WEB
        assert sql("SELECT count(*) FROM command_control") == "0"
        saved = state()
        metadata = {
            name: (demo.path / name).read_bytes()
            for name in ("state.json", "compose.env")
        }
        demo.upgrade(args.web)
        demo.verify()
        assert state() == saved and metadata == {
            name: (demo.path / name).read_bytes() for name in metadata
        }
        print(
            "PASS 0006->0007 migration/recovery/repeat, all business/ledger fields, credential/cert/ACL hashes, infra IDs/mounts/ports unchanged",
            flush=True,
        )

        # Backup is truly restorable into a SECOND owned database; never clean live DB.
        sql("CREATE DATABASE audit_restore OWNER kzhome")
        dump = (directory / "database.dump").read_bytes()
        result = subprocess.run(
            demo.argv(
                "exec",
                "-T",
                "postgres",
                "pg_restore",
                "-U",
                "postgres",
                "-d",
                "audit_restore",
                "--exit-on-error",
            ),
            cwd=checkout,
            env=demo.env,
            input=dump,
            capture_output=True,
            timeout=120,
        )
        assert result.returncode == 0, "Synthetic restore failed (details withheld)"
        restored = demo.dc(
            "exec",
            "-T",
            "postgres",
            "psql",
            "-U",
            "postgres",
            "-d",
            "audit_restore",
            "-Atc",
            "SELECT count(*) FROM users; SELECT count(*) FROM devices; SELECT count(*) FROM device_commands; SELECT version_num FROM alembic_version",
        )
        assert restored.splitlines() == ["2", "4", "4", "0006_command_lifecycle"]
        print(
            "PASS preliminary pg_dump restored into separate synthetic DB (0006); not a full MQTT/host rollback",
            flush=True,
        )
        pending = snapshot["pending"]
        receipt = api(
            "/v1/houses/e2e_house/commands/" + pending["command_id"], token=token
        )
        assert (
            receipt["expires_at"] == pending["expires_at"]
            and receipt["status"] == "timed_out"
        )
        assert (
            "device.control"
            in api("/v1/houses/e2e_house/me/permissions", token=token)["permissions"]
        )

        browser = (
            (ROOT / "tests/demo_control_browser.mjs")
            .read_text(encoding="utf-8")
            .replace("v020[a-f0-9]+", "v022[a-f0-9]+")
        )
        for mode in ("apply", "fail", "timeout", "offline"):
            if mode != "offline":
                simulator(mode)
            result = subprocess.run(
                [
                    "node",
                    "--input-type=module",
                    "-e",
                    "process.argv.splice(1,0,'audit-browser.mjs');\n" + browser,
                    demo.project,
                    str(args.web),
                    mode,
                ],
                cwd=checkout,
                timeout=90,
            )
            assert result.returncode == 0, "Compiled browser check failed"
            stop_relay()
        assert sql(
            "SELECT status,count(*) FROM device_commands GROUP BY status ORDER BY status"
        ).splitlines() == ["applied|5", "failed|2", "timed_out|2"]
        assert (
            sql(
                "SELECT count(*) FROM device_commands WHERE status='applied' AND (ack_status IS DISTINCT FROM 'applied' OR state_received_at IS NULL OR reported_on IS DISTINCT FROM requested_on)"
            )
            == "0"
        )
        assert sql("SELECT count(*) FROM command_control") == "0"
        saved = business()
        demo.dc("stop")
        demo.dc("start", "postgres", "mosquitto", "core", "proxy")
        for _ in range(150):
            try:
                demo.verify()
                break
            except Exception:
                time.sleep(0.2)
        else:
            raise RuntimeError("Owned restart did not recover")
        assert business() == saved
        print(
            "PASS compiled Web v0.19 apply/fail/real timeout/offline; MQTT TLS old ACL; saved deadline; service restart persistence",
            flush=True,
        )
    finally:
        stop_relay()


def parent(args):
    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(ROOT / "tests"))
    from test_house_permissions_live import inventory

    before = inventory()
    project = "kzhome-demo-check-v022" + uuid4().hex[:10]
    folder = (ROOT / "deploy/local/v022-audit" / project).resolve()
    checkout = folder / "source"
    assert folder.is_relative_to((ROOT / "deploy/local/v022-audit").resolve())
    folder.mkdir(parents=True)
    active = False
    try:
        assert (
            run(["git", "-C", str(args.web), "rev-parse", "HEAD"]).decode().strip()
            == WEB
        )
        assert not run(["git", "-C", str(args.web), "status", "--porcelain"])
        run(["git", "worktree", "add", "--detach", str(checkout), OLD])
        active = True
        print("Owned synthetic upgrade project: " + project, flush=True)

        def phase(name):
            result = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    "-u",
                    str(Path(__file__).resolve()),
                    "--phase",
                    name,
                    "--checkout",
                    str(checkout),
                    "--project",
                    project,
                    "--web",
                    str(args.web),
                ],
                cwd=ROOT,
                timeout=1800,
            )
            assert result.returncode == 0, "Synthetic upgrade phase failed"

        phase("old")
        archive = folder / "target.zip"
        run(["git", "archive", "--format=zip", "--output", str(archive), TARGET])
        with zipfile.ZipFile(archive) as package:
            assert all(
                (checkout / n).resolve().is_relative_to(checkout.resolve())
                for n in package.namelist()
            )
            package.extractall(checkout)
        phase("target")
        print(
            "PASS actual source-in-place Core v0.20 -> v0.22, no installed demo changed",
            flush=True,
        )
    finally:
        # Cleanup only exact project-owned containers/volumes/networks/images.
        names = (
            run(
                [
                    "docker",
                    "ps",
                    "-aq",
                    "--filter",
                    "label=com.docker.compose.project=" + project,
                ]
            )
            .decode()
            .split()
        )
        extra = (
            run(
                [
                    "docker",
                    "ps",
                    "-aq",
                    "--filter",
                    "label=kzhome.demo.virtual=" + project,
                ]
            )
            .decode()
            .split()
        )
        for identifier in set(names + extra):
            run(["docker", "rm", "-f", "-v", identifier])
        for identifier in (
            run(
                [
                    "docker",
                    "network",
                    "ls",
                    "-q",
                    "--filter",
                    "label=com.docker.compose.project=" + project,
                ]
            )
            .decode()
            .split()
        ):
            run(["docker", "network", "rm", identifier])
        for identifier in (
            run(
                [
                    "docker",
                    "volume",
                    "ls",
                    "-q",
                    "--filter",
                    "label=com.docker.compose.project=" + project,
                ]
            )
            .decode()
            .split()
        ):
            run(["docker", "volume", "rm", identifier])
        for repository in ("kz-home-core", "kz-home-web"):
            tags = (
                run(
                    [
                        "docker",
                        "image",
                        "ls",
                        "--format",
                        "{{.Repository}}:{{.Tag}}",
                        repository,
                    ]
                )
                .decode()
                .split()
            )
            for tag in tags:
                if tag.startswith(repository + ":" + project + "-"):
                    run(["docker", "image", "rm", tag])
        if active:
            assert checkout.resolve().is_relative_to(folder) and folder.name == project
            run(["git", "worktree", "remove", "--force", str(checkout)])
        if folder.exists():
            assert folder.resolve().is_relative_to(
                (ROOT / "deploy/local/v022-audit").resolve()
            )
            shutil.rmtree(folder)
        after = inventory()
        assert all(
            after["containers"].get(k) == v for k, v in before["containers"].items()
        )
        assert all(
            set(before[k]).issubset(after[k]) for k in ("volumes", "networks", "images")
        )
        print(
            "PASS foreign resources unchanged; owned synthetic data/backups/volumes/images/worktree removed",
            flush=True,
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--web", type=Path, required=True)
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--phase", choices=("old", "target"))
    parser.add_argument("--checkout", type=Path)
    parser.add_argument("--project")
    args = parser.parse_args()
    args.web = args.web.resolve()
    if args.phase:
        assert args.project.startswith("kzhome-demo-check-v022")
        worker(args)
    else:
        if not args.run:
            parser.error("Explicit --run required; own synthetic Docker fixture only")
        parent(args)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        import traceback

        for frame in traceback.extract_tb(error.__traceback__)[-3:]:
            print(
                f"Audit location: {Path(frame.filename).name}:{frame.lineno} {frame.name}",
                flush=True,
            )
        print("FAIL synthetic upgrade audit; private details withheld", flush=True)
        raise SystemExit(1) from None
