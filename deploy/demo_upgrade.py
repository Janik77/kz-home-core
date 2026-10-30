"""Explicit forward-only upgrade of a prepared demo; never volume deletion/reset."""

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
from contextlib import contextmanager
from uuid import uuid4

from deploy.demo import DemoError, ROOT, check_web, command, private_write, stage_web

PRIVATE = (
    "user.json",
    "simulation.json",
    "proxy.conf",
    "mqtt/ca.crt",
    "mqtt/ca.key",
    "mqtt/server.crt",
    "mqtt/server.key",
    "mqtt/passwords",
    "mqtt/acl",
)


@contextmanager
def operation_lock(demo):
    """OS lock releases on process exit, including interrupted upgrades."""
    with (demo.path / "upgrade.lock").open("a+b") as lock:
        lock.seek(0)
        lock.write(b"0")
        lock.flush()
        lock.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise DemoError("upgrade") from None
        try:
            yield
        finally:
            lock.seek(0)
            if os.name == "nt":
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def image_ids(values):
    return {
        values[key]: json.loads(command(["docker", "image", "inspect", values[key]]))[
            0
        ]["Id"]
        for key in ("DEMO_CORE_IMAGE", "DEMO_WEB_IMAGE")
    }


def atomic(path, text):
    temporary = path.with_name(path.name + ".new")
    private_write(temporary, text)
    os.replace(temporary, path)


def source_id(web):
    # Include uncommitted runtime tooling/source in provenance for development acceptance.
    names = command(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"]
    ).split("\0")
    hashed = hashlib.sha256()
    for name in sorted(set(names)):
        if name and (
            name.startswith(("app/", "simulator/", "deploy/", "alembic/"))
            or name
            in (
                "Dockerfile",
                "requirements.txt",
                "alembic.ini",
                "compose.production.yaml",
                "compose.demo.yaml",
                "compose.arm64.yaml",
            )
        ):
            path = ROOT / name
            if path.is_file():
                hashed.update(name.encode())
                hashed.update(path.read_bytes())
    return {
        "core_tree": hashed.hexdigest(),
        "web_revision": command(["git", "rev-parse", "HEAD"], cwd=web),
    }


def check(demo, web):
    demo.step = "upgrade"
    demo.guard()
    check_web(web)
    # Detect incompatible source layout before stopping anything; no role policy copy.
    if (
        not (web / "src/data/core/permissions.ts").is_file()
        or not (web / "src/data/core/relay-control.ts").is_file()
    ):
        raise DemoError("upgrade")
    for name in (*PRIVATE, "state.json", "compose.env"):
        path = demo.path / name
        if (
            not path.is_file()
            or path.is_symlink()
            or not path.resolve().is_relative_to(demo.path.resolve())
        ):
            raise DemoError("upgrade")
    config = json.loads(demo.dc("config", "--format", "json"))
    password = demo.state.get("application_password", "")
    import re

    if not re.fullmatch(r"[a-f0-9]{64}", password):
        raise DemoError("upgrade")
    expected_db = f"postgresql+psycopg://kzhome:{password}@postgres:5432/kzhome"
    core_env = config["services"]["core"]["environment"]
    if (
        core_env.get("DATABASE_URL") != expected_db
        or core_env.get("MQTT_TLS_ENABLED") != "true"
        or core_env.get("MQTT_HOST") != "mosquitto"
        or core_env.get("APP_ENV") != "production"
    ):
        raise DemoError("upgrade")
    if Path(demo.state["env"]["DEMO_INPUT_DIR"]).resolve() != demo.path.resolve():
        raise DemoError("upgrade")
    if set(config["services"]) != {"core", "proxy", "postgres", "mosquitto"}:
        raise DemoError("upgrade")
    if any(
        v.get("external") or v["name"] != demo.project + "_" + key
        for key, v in config["networks"].items()
    ):
        raise DemoError("upgrade")
    if any(
        s.get("container_name") or s.get("privileged")
        for s in config["services"].values()
    ):
        raise DemoError("upgrade")
    # No infrastructure image/major-version replacement or foreign containers.
    for service in ("core", "proxy", "postgres", "mosquitto"):
        identifier = demo.dc("ps", "-a", "-q", service)
        if not identifier or "\n" in identifier:
            raise DemoError("upgrade")
        item = json.loads(command(["docker", "inspect", identifier]))[0]
        labels = item["Config"]["Labels"]
        current_env = dict(entry.split("=", 1) for entry in item["Config"]["Env"])
        if service == "core" and any(
            current_env.get(key) != demo.state["env"].get(key)
            for key in ("DATABASE_URL", "AUTH_JWT_SECRET", "MQTT_PASSWORD")
        ):
            raise DemoError("upgrade")
        if (
            labels.get("com.docker.compose.project") != demo.project
            or labels.get("com.docker.compose.service") != service
        ):
            raise DemoError("upgrade")
        if service in ("postgres", "mosquitto"):
            image = json.loads(
                command(
                    ["docker", "image", "inspect", config["services"][service]["image"]]
                )
            )[0]["Id"]
            if item["Image"] != image or not item["State"]["Running"]:
                raise DemoError("upgrade")
            for mount in item["Mounts"]:
                if mount["Type"] == "volume" and not mount["Name"].startswith(
                    demo.project + "_"
                ):
                    # Mosquitto's image declares an implicit anonymous log volume.
                    # Infrastructure is never recreated by upgrade; permit only
                    # that exact Docker-generated mount, not arbitrary named data.
                    import re

                    if not (
                        service == "mosquitto"
                        and mount["Destination"] == "/mosquitto/log"
                        and re.fullmatch(r"[a-f0-9]{64}", mount["Name"])
                    ):
                        raise DemoError("upgrade")
    revision = demo.dc(
        "exec",
        "-T",
        "postgres",
        "psql",
        "-U",
        "postgres",
        "-d",
        "kzhome",
        "-Atc",
        "SELECT version_num FROM alembic_version",
    )
    if revision not in ("0005_device_onboarding", "0006_command_lifecycle"):
        raise DemoError("upgrade")
    return source_id(web)


def backup(demo, *, restart=True):
    demo.step = "upgrade"
    demo.guard()
    directory = demo.path / "backups" / uuid4().hex
    directory.mkdir(parents=True, mode=0o700)
    demo.dc("stop", "proxy", "core")
    try:
        for name in (*PRIVATE, "state.json", "compose.env"):
            target = directory / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(demo.path / name, target)
            target.chmod(0o600)
        # Binary custom-format dump, never decoded or printed. No passwords in argv.
        with (directory / "database.dump").open("xb") as output:
            result = subprocess.run(
                demo.argv(
                    "exec",
                    "-T",
                    "postgres",
                    "pg_dump",
                    "-U",
                    "postgres",
                    "-d",
                    "kzhome",
                    "-Fc",
                ),
                cwd=ROOT,
                env=demo.env,
                stdout=output,
                stderr=subprocess.PIPE,
                timeout=120,
            )
        if result.returncode or not (directory / "database.dump").stat().st_size:
            raise DemoError("upgrade")
        (directory / "database.dump").chmod(0o600)
        listing = subprocess.run(
            demo.argv("exec", "-T", "postgres", "pg_restore", "--list"),
            cwd=ROOT,
            env=demo.env,
            input=(directory / "database.dump").read_bytes(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=120,
        )
        if listing.returncode:
            raise DemoError("upgrade")
        manifest = {
            "project": demo.project,
            "files": {
                str(p.relative_to(directory)): digest(p)
                for p in directory.rglob("*")
                if p.is_file()
            },
        }
        private_write(directory / "manifest.json", json.dumps(manifest))
        return directory
    finally:
        if restart:
            demo.dc(
                "start", "core", "proxy"
            )  # Start original containers, never recreate.


def upgrade(demo, web):
    with operation_lock(demo):
        return apply_upgrade(demo, web)


def apply_upgrade(demo, web):
    identity = check(demo, web)
    pending = demo.path / "upgrade.pending.json"
    if pending.exists():
        plan = json.loads(pending.read_text())
        if plan["project"] != demo.project or plan["identity"] != identity:
            raise DemoError("upgrade")
    else:
        if demo.state.get("upgrade_identity") == identity:
            demo.verify()
            print("PASS: same source already upgraded; no migration or recreation")
            return
        previous = json.loads(json.dumps(demo.state))
        suffix = uuid4().hex
        target = json.loads(json.dumps(previous))
        context = demo.path / ("web-upgrade-" + suffix)
        stage_web(web, context)
        target["web_revision"] = identity["web_revision"]
        target["web_source"] = str(web.resolve())
        target["upgrade_identity"] = identity
        target["env"].update(
            DEMO_WEB_CONTEXT=str(context).replace("\\", "/"),
            DEMO_CORE_IMAGE="kz-home-core:" + demo.project + "-upgrade-" + suffix,
            DEMO_WEB_IMAGE="kz-home-web:" + demo.project + "-upgrade-" + suffix,
        )
        target["env"]["CORE_ARM64_IMAGE"] = target["env"]["DEMO_CORE_IMAGE"]
        demo.env.update(target["env"])
        demo.dc(
            "build", "core", "proxy"
        )  # No installed containers or metadata changed yet.
        plan = {
            "project": demo.project,
            "identity": identity,
            "target": target,
            "previous": previous,
            "phase": "built",
            "images": image_ids(target["env"]),
        }
        atomic(pending, json.dumps(plan))
    if image_ids(plan["target"]["env"]) != plan["images"]:
        raise DemoError("upgrade")
    if plan["phase"] == "built":
        demo.load()
        directory = backup(demo, restart=False)
        plan.update(
            phase="backed-up",
            backup=str(directory),
            private={name: digest(demo.path / name) for name in PRIVATE},
        )
        atomic(pending, json.dumps(plan))
    directory = Path(plan["backup"]).resolve()
    if not directory.is_relative_to((demo.path / "backups").resolve()):
        raise DemoError("upgrade")
    manifest = json.loads((directory / "manifest.json").read_text())
    if manifest["project"] != demo.project or any(
        digest(directory / name) != value for name, value in manifest["files"].items()
    ):
        raise DemoError("upgrade")
    if any(
        digest(demo.path / name) != value for name, value in plan["private"].items()
    ):
        raise DemoError("upgrade")
    demo.dc("stop", "proxy", "core")
    demo.env.update(plan["target"]["env"])
    # Explicit forward-only migration, repeatable after interruption. Never bootstrap/reset.
    demo.dc(
        "run",
        "--rm",
        "--no-deps",
        "-T",
        "core",
        "python",
        "-m",
        "alembic",
        "upgrade",
        "head",
    )
    atomic(demo.path / "state.json", json.dumps(plan["target"]))
    atomic(
        demo.path / "compose.env",
        "\n".join(f"{k}='{v}'" for k, v in plan["target"]["env"].items()) + "\n",
    )
    demo.load()
    demo.dc("up", "-d", "--no-deps", "--pull", "never", "--wait", "core", "proxy")
    demo.verify()
    history = demo.path / "upgrade-history"
    history.mkdir(exist_ok=True)
    os.replace(pending, history / (uuid4().hex + ".json"))
    print("PASS: forward upgrade verified; backups and previous image tags retained")
