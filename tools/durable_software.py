"""Durable software-only profile. Never deletes data; restore only to a new profile."""

import argparse
import asyncio
from contextlib import asynccontextmanager, contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import socket
import ssl
import subprocess
import sys
import time
from uuid import uuid4

CORE = Path(__file__).resolve().parents[1]
CORE_SHA = "6783ce0347004d101ec2ffa5eb60c48946901786"
WEB_SHA = "fac23c233a1c86dad1661a9e2970cfca9450d00d"
PG = "postgres:17-bookworm@sha256:665dc7b37ac52a17e298511414c21d23ffa4e6a6ef6225b3e5d59c7afb35e994"
MQTT = "eclipse-mosquitto:2@sha256:75d6662dee52c947dc9dd9c29f1d8d7ffef78ddaf38001caa09755681e8bbc98"
OPENSSL = "alpine/openssl:latest@sha256:6821afa2476e11be343f29275f2ca24291720ff0a7148bd131fd8e314ff26d9e"
LABEL = "kzhome.durable-software"
FILES = (
    "profile.json",
    "ca.key",
    "ca.crt",
    "server.key",
    "server.crt",
    "passwd",
    "acl",
    "mosquitto.conf",
)
CERTS = """
set -eu
cd /lab
openssl req -x509 -newkey rsa:2048 -nodes -keyout ca.key -out ca.crt -days 365 -subj /CN=KZ-Software-Only-CA >/dev/null 2>&1
openssl req -newkey rsa:2048 -nodes -keyout server.key -out server.csr -subj /CN=localhost >/dev/null 2>&1
printf 'subjectAltName=DNS:localhost\\nextendedKeyUsage=serverAuth\\n' > extensions
openssl x509 -req -in server.csr -CA ca.crt -CAkey ca.key -CAcreateserial -out server.crt -days 365 -extfile extensions >/dev/null 2>&1
chmod 600 ca.key server.key
"""
BROKER = """listener 8883
allow_anonymous false
password_file /lab/passwd
acl_file /lab/acl
cafile /lab/ca.crt
certfile /lab/server.crt
keyfile /lab/server.key
tls_version tlsv1.2
persistence false
log_dest stdout
"""
ACL = (
    "user core_lab\n"
    + "".join(
        f"topic read kzhome/v1/+/+/{kind}\n"
        for kind in ("ack", "state", "status", "telemetry")
    )
    + """topic write kzhome/v1/fixture_house/fixture_relay/set
user software_lab
topic read kzhome/v1/fixture_house/fixture_relay/set
topic write kzhome/v1/fixture_house/fixture_relay/ack
topic write kzhome/v1/fixture_house/fixture_relay/state
topic write kzhome/v1/fixture_house/fixture_relay/status
"""
)


def private_write(path, data):
    with path.open("xb") as stream:
        os.chmod(path, 0o600)
        stream.write(data if isinstance(data, bytes) else data.encode())


def new_directory(path):
    path = path.expanduser().absolute()
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError("Symlink paths refused")
    if any(c in str(path) for c in (",", "\n", "\r")):
        raise ValueError("Unsupported mount path")
    path.mkdir(mode=0o700, parents=False, exist_ok=False)
    return path


class Docker:
    def __init__(self):
        if os.environ.get("DOCKER_HOST"):
            raise ValueError(
                "DOCKER_HOST override refused; use a local Unix-socket context"
            )
        result = subprocess.run(
            ["docker", "context", "show"], capture_output=True, check=True, text=True
        )
        self.context = result.stdout.strip()
        result = subprocess.run(
            ["docker", "context", "inspect", self.context],
            capture_output=True,
            check=True,
            text=True,
        )
        host = json.loads(result.stdout)[0]["Endpoints"]["docker"]["Host"]
        if not host.startswith("unix://"):
            raise ValueError("Only a local Unix-socket Docker context is supported")
        system = self.text("info", "--format", "{{.OperatingSystem}}")
        if (
            "Docker Desktop" not in system
            and os.environ.get("GITHUB_ACTIONS") != "true"
        ):
            raise ValueError(
                "This mode permits Docker Desktop only outside disposable GitHub CI"
            )

    def run(self, *args, check=True, data=None):
        result = subprocess.run(
            ["docker", "--context", self.context, *args],
            input=data,
            capture_output=True,
            timeout=180,
        )
        if check and result.returncode:
            raise RuntimeError(
                "Owned Docker operation failed; no automatic deletion performed"
            )
        return result

    def text(self, *args):
        return self.run(*args).stdout.decode().strip()


class Profile:
    def __init__(self, directory, docker=None):
        self.path = Path(directory).expanduser().absolute()
        if any(p.is_symlink() for p in (self.path, *self.path.parents)):
            raise ValueError("Symlink profile refused")
        self.state = json.loads((self.path / "profile.json").read_text())
        self.owner = self.state["owner"]
        if not re.fullmatch(r"[a-f0-9]{32}", self.owner):
            raise ValueError("Invalid profile identity")
        if self.state["core_sha"] != CORE_SHA or self.state["web_sha"] != WEB_SHA:
            raise ValueError("Unsupported profile source version")
        self.name = "kzsoft-" + self.owner
        self.docker = docker or Docker()

    @property
    def labels(self):
        return ["--label", LABEL + "=" + self.owner]

    def owned(self, kind, name):
        result = self.docker.run(kind, "inspect", name, check=False)
        if result.returncode:
            # Distinguish absence from daemon/permission failure.
            names = self.docker.text(
                kind,
                "ls",
                "--format",
                "{{.Name}}" if kind != "container" else "{{.Names}}",
            )
            if name in names.splitlines():
                raise RuntimeError("Cannot inspect owned resource")
            return None
        item = json.loads(result.stdout)[0]
        labels = (
            item.get("Labels") if kind != "container" else item["Config"].get("Labels")
        )
        if (labels or {}).get(LABEL) != self.owner:
            raise ValueError("Refusing foreign resource")
        return item

    def ensure_storage(self):
        for kind, name, extra in (
            ("network", self.name, ["--internal"]),
            ("volume", self.name + "-pgdata", []),
        ):
            if not self.owned(kind, name):
                self.docker.run(kind, "create", *extra, *self.labels, name)

    def pg(self):
        self.ensure_storage()
        name = self.name + "-pg"
        if not self.owned("container", name):
            self.docker.run(
                "run",
                "-d",
                "--name",
                name,
                *self.labels,
                "--network",
                self.name,
                "--mount",
                f"type=volume,source={self.name}-pgdata,target=/var/lib/postgresql/data",
                "-p",
                "127.0.0.1::5432",
                "-e",
                "POSTGRES_USER=lab",
                "-e",
                "POSTGRES_DB=lab",
                "-e",
                "POSTGRES_PASSWORD=" + self.state["mqtt_password"],
                PG,
            )
        else:
            self.docker.run("start", name)
        for _ in range(120):
            if (
                self.docker.run(
                    "exec",
                    name,
                    "pg_isready",
                    "-h",
                    "127.0.0.1",
                    "-U",
                    "lab",
                    "-d",
                    "lab",
                    check=False,
                ).returncode
                == 0
            ):
                break
            time.sleep(0.25)
        else:
            raise RuntimeError("Owned PostgreSQL readiness timeout")
        port = int(self.docker.text("port", name, "5432/tcp").rsplit(":", 1)[1])
        return f"postgresql+psycopg://lab:{self.state['mqtt_password']}@127.0.0.1:{port}/lab"

    def mqtt(self):
        name = self.name + "-mqtt"
        if not self.owned("container", name):
            self.docker.run(
                "run",
                "-d",
                "--name",
                name,
                *self.labels,
                "--user",
                f"{os.getuid()}:{os.getgid()}",
                "--network",
                self.name,
                "--read-only",
                "--tmpfs",
                "/tmp",
                "--mount",
                f"type=bind,source={self.path},target=/lab,readonly",
                "-p",
                "127.0.0.1::8883",
                MQTT,
                "mosquitto",
                "-c",
                "/lab/mosquitto.conf",
            )
        else:
            self.docker.run("start", name)
        port = int(self.docker.text("port", name, "8883/tcp").rsplit(":", 1)[1])
        context = ssl.create_default_context(cafile=str(self.path / "ca.crt"))
        for _ in range(100):
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=1) as sock:
                    with context.wrap_socket(sock, server_hostname="localhost"):
                        return port
            except OSError:
                time.sleep(0.1)
        raise RuntimeError("Owned broker TLS readiness timeout")

    def stop(self):
        for suffix in ("-mqtt", "-pg"):
            name = self.name + suffix
            if self.owned("container", name):
                self.docker.run("stop", "--time", "30", name)

    @contextmanager
    def lock(self):
        with (self.path / ".lock").open("a") as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError(
                    "Profile active; stop its foreground launcher with Ctrl-C first"
                ) from None
            yield

    def backup(self, destination):
        # Caller holds lock: app/relay/Vite are stopped. Quiesce only owned containers.
        if not (self.path / "seeded").is_file():
            raise ValueError("Start and initialize this profile before backup")
        self.stop()
        target = new_directory(destination)
        try:
            self.pg()
            result = self.docker.run(
                "exec",
                self.name + "-pg",
                "pg_dump",
                "-U",
                "lab",
                "-d",
                "lab",
                "-Fc",
                "--no-owner",
                "--no-acl",
            )
            private_write(target / "database.dump", result.stdout)
            for name in FILES:
                source = self.path / name
                if source.is_symlink():
                    raise ValueError("Symlink backup input refused")
                private_write(target / name, source.read_bytes())
            hashes = {
                name: hashlib.sha256((target / name).read_bytes()).hexdigest()
                for name in (*FILES, "database.dump")
            }
            private_write(
                target / "manifest.json", json.dumps({"format": 1, "sha256": hashes})
            )
        finally:
            self.stop()
        return target


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def initialize(directory):
    docker = Docker()
    path = new_directory(directory)
    state = dict(
        owner=uuid4().hex,
        core_sha=CORE_SHA,
        web_sha=WEB_SHA,
        web_port=free_port(),
        mqtt_password=secrets.token_urlsafe(32),
        jwt_secret=secrets.token_urlsafe(48),
        password=secrets.token_urlsafe(32),
    )
    private_write(path / "profile.json", json.dumps(state))
    profile = Profile(path, docker)
    mount = f"type=bind,source={path},target=/lab"
    user = f"{os.getuid()}:{os.getgid()}"
    docker.run(
        "run",
        "--rm",
        *profile.labels,
        "--network",
        "none",
        "--user",
        user,
        "--mount",
        mount,
        "--entrypoint",
        "sh",
        OPENSSL,
        "-c",
        CERTS,
    )
    private_write(
        path / "passwd",
        "\n".join(
            name + ":" + state["mqtt_password"] for name in ("core_lab", "software_lab")
        )
        + "\n",
    )
    docker.run(
        "run",
        "--rm",
        *profile.labels,
        "--network",
        "none",
        "--user",
        user,
        "--mount",
        mount,
        "--entrypoint",
        "mosquitto_passwd",
        MQTT,
        "-U",
        "/lab/passwd",
    )
    private_write(path / "acl", ACL)
    private_write(path / "mosquitto.conf", BROKER)
    private_write(path / "initialized", "1")
    return profile


def restore(source, destination):
    source = Path(source).expanduser().absolute()
    if any(p.is_symlink() for p in (source, *source.parents, source / "manifest.json")):
        raise ValueError("Symlink backup refused")
    manifest = json.loads((source / "manifest.json").read_text())
    expected = set((*FILES, "database.dump"))
    if manifest.get("format") != 1 or set(manifest.get("sha256", {})) != expected:
        raise ValueError("Invalid backup manifest")
    contents = {}
    for name in expected:
        item = source / name
        if item.is_symlink() or not item.is_file():
            raise ValueError("Invalid backup entry")
        contents[name] = item.read_bytes()
        if hashlib.sha256(contents[name]).hexdigest() != manifest["sha256"][name]:
            raise ValueError("Backup checksum mismatch")
    saved = json.loads(contents["profile.json"])
    if saved["core_sha"] != CORE_SHA or saved["web_sha"] != WEB_SHA:
        raise ValueError("Backup requires another reviewed version")
    docker = Docker()
    target = new_directory(destination)  # Never restore into any existing profile.
    saved["owner"] = uuid4().hex  # Never adopt the source's containers/network/volume.
    saved["web_port"] = free_port()
    contents["profile.json"] = json.dumps(saved).encode()
    for name in FILES:
        private_write(target / name, contents[name])
    profile = Profile(target, docker)
    try:
        profile.pg()
        tables = docker.text(
            "exec",
            profile.name + "-pg",
            "psql",
            "-U",
            "lab",
            "-d",
            "lab",
            "-Atc",
            "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'",
        )
        if tables != "0":
            raise ValueError("Restore target database is not empty")
        docker.run(
            "exec",
            "-i",
            profile.name + "-pg",
            "pg_restore",
            "-U",
            "lab",
            "-d",
            "lab",
            "--exit-on-error",
            "--no-owner",
            "--no-acl",
            data=contents["database.dump"],
        )
        private_write(
            target / "initialized", "1"
        )  # Incomplete restore cannot be started.
        private_write(target / "seeded", "1")
    finally:
        profile.stop()
    return profile


def validate_core():
    roots = ["app", "simulator", "alembic", "alembic.ini", "requirements.txt"]
    subprocess.run(
        ["git", "-C", str(CORE), "diff", "--quiet", CORE_SHA, "--", *roots], check=True
    )
    result = subprocess.run(
        ["git", "-C", str(CORE), "status", "--porcelain", "--", *roots],
        capture_output=True,
        check=True,
        text=True,
    )
    if result.stdout.strip():
        raise ValueError("Core runtime must match the pinned source")


@asynccontextmanager
async def durable_stack(profile, core):
    from alembic import command
    from alembic.config import Config as AlembicConfig
    from alembic.script import ScriptDirectory
    from sqlalchemy import create_engine, select, text
    from sqlalchemy.orm import Session
    from app.models import UserORM
    from app.core import Settings
    from app.repositories import UserRepository
    from app.services.auth_service import UserService
    from app.transports.mqtt_client import AiomqttClient
    from simulator.e2e_acceptance import API
    from simulator.mqtt_relay import Config
    import uvicorn

    engine = server = task = sock = None
    try:
        url = profile.pg()
        mqtt_port = profile.mqtt()
        engine = create_engine(url)
        cfg = AlembicConfig(str(core / "alembic.ini"))
        cfg.set_main_option("script_location", str(core / "alembic"))
        migration = ScriptDirectory.from_config(cfg).get_current_head()
        with engine.begin() as connection:
            cfg.attributes["connection"] = connection
            command.upgrade(cfg, "head")
        with Session(engine) as session:
            user = session.scalar(
                select(UserORM).where(UserORM.email == "owner@example.test")
            )
            if user is None:
                if session.scalar(select(UserORM.id).limit(1)) is not None:
                    raise ValueError("Unexpected database inventory; refusing reseed")
                UserService(UserRepository(session)).create(
                    "owner@example.test", profile.state["password"]
                )
        options = dict(
            database_url=url,
            app_env="production",
            mqtt_enabled=True,
            mqtt_host="localhost",
            mqtt_port=mqtt_port,
            mqtt_tls_enabled=True,
            mqtt_client_id=profile.name + "-core",
            mqtt_username="core_lab",
            mqtt_password=profile.state["mqtt_password"],
            auth_jwt_secret=profile.state["jwt_secret"],
            mqtt_software_devices=(("fixture_house", "fixture_relay"),),
        )
        # Avoid module-level create_app reading any installation .env.
        from unittest.mock import patch

        with patch.dict(
            os.environ,
            {
                "APP_ENV": "production",
                "DATABASE_URL": url,
                "MQTT_ENABLED": "true",
                "MQTT_TLS_ENABLED": "true",
                "MQTT_HOST": "localhost",
                "MQTT_PORT": str(mqtt_port),
                "MQTT_CLIENT_ID": "unused-import",
                "AUTH_JWT_SECRET": profile.state["jwt_secret"],
            },
            clear=True,
        ):
            from app.main import create_app
        wire = AiomqttClient(
            host="localhost",
            port=mqtt_port,
            username="core_lab",
            password=profile.state["mqtt_password"],
            tls_enabled=True,
            keepalive=15,
            client_id=profile.name + "-core",
        )
        wire._options["tls_context"] = ssl.create_default_context(
            cafile=str(profile.path / "ca.crt")
        )
        app = create_app(Settings(**options), run_simulator=False, mqtt_client=wire)
        server = uvicorn.Server(
            uvicorn.Config(
                app,
                log_level="warning",
                access_log=False,
                lifespan="on",
                loop="asyncio",
            )
        )
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        task = asyncio.create_task(server.serve(sockets=[sock]))
        async with asyncio.timeout(30):
            while not server.started or not app.state.mqtt_gateway.connected:
                if task.done():
                    await task
                    raise RuntimeError("Core startup stopped")
                await asyncio.sleep(0.05)
        config = Config(
            "localhost",
            mqtt_port,
            "software_lab",
            profile.state["mqtt_password"],
            str(profile.path / "ca.crt"),
            "fixture_house",
            "fixture_relay",
            profile.name + "-relay",
        )

        def sql(query):
            with engine.begin() as connection:
                return str(connection.scalar(text(query)))

        if sql("SELECT count(*) FROM physical_devices") != "0":
            raise ValueError(
                "Durable software profile contains physical inventory; refusing"
            )
        yield (
            config,
            {"password": profile.state["password"]},
            {},
            API(f"http://127.0.0.1:{port}"),
            sql,
            migration,
        )
    finally:
        try:
            if server:
                server.should_exit = True
            if task:
                await asyncio.wait_for(task, 15)
        finally:
            if sock:
                sock.close()
            if engine:
                engine.dispose()
            profile.stop()


async def provision_once(profile, api, ids, seed):
    import httpx

    login = await api.request(
        "POST",
        "/auth/login",
        {"email": "owner@example.test", "password": seed["password"]},
    )
    api.token = login["access_token"]
    if (profile.path / "seeded").exists():
        return
    records = (
        ("/houses", {"id": "fixture_house", "name": "Software home"}),
        (
            "/floors",
            {
                "id": "fixture_floor",
                "house_id": "fixture_house",
                "name": "Floor",
                "order": 0,
            },
        ),
        ("/rooms", {"id": "fixture_room", "floor_id": "fixture_floor", "name": "Room"}),
        (
            "/devices",
            {
                "id": "fixture_relay",
                "room_id": "fixture_room",
                "name": "Software relay",
                "type": "relay",
                "capabilities": ["on_off"],
                "state": {},
                "online": False,
            },
        ),
    )
    async with httpx.AsyncClient(
        base_url=api.base,
        trust_env=False,
        headers={"Authorization": "Bearer " + api.token},
    ) as http:
        for path, body in records:
            response = await http.get(path + "/" + body["id"])
            if response.status_code == 404:
                response = await http.post(path, json=body)
            response.raise_for_status()
    private_write(profile.path / "seeded", "1")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action", choices=("init", "start", "stop", "backup", "restore", "status")
    )
    parser.add_argument("--state", required=True, type=Path)
    parser.add_argument("--web-root", type=Path)
    parser.add_argument("--destination", type=Path)
    parser.add_argument("--backup", type=Path)
    args = parser.parse_args()
    if os.name != "posix" or os.getuid() == 0:
        parser.error(
            "Use an ordinary Linux/WSL user with existing Docker access; do not elevate"
        )
    try:
        if args.action == "init":
            initialize(args.state)
            print(
                "Created private software profile. Login is owner@example.test; password is in profile.json."
            )
            return
        if args.action == "restore":
            if args.backup is None:
                parser.error("--backup required")
            restore(args.backup, args.state)
            print("Restored into NEW stopped profile; source untouched.")
            return
        profile = Profile(args.state)
        with profile.lock():
            if not (profile.path / "initialized").is_file():
                raise ValueError(
                    "Incomplete init/restore; preserve data, do not start or overwrite"
                )
            if args.action == "stop":
                profile.stop()
            elif args.action == "backup":
                if args.destination is None:
                    parser.error("--destination required")
                profile.backup(args.destination)
                print(
                    "Private verified-manifest backup created; profile remains stopped."
                )
            elif args.action == "status":
                for suffix in ("-pg", "-mqtt"):
                    item = profile.owned("container", profile.name + suffix)
                    print(
                        suffix[1:],
                        "absent" if item is None else item["State"]["Status"],
                    )
            else:
                if args.web_root is None:
                    parser.error("--web-root required")
                validate_core()
                sys.path[:0] = [str(CORE), str(args.web_root.resolve() / "e2e")]
                from run_software_stack import run, validate_web

                web = validate_web(args.web_root)

                # An async context manager factory itself is synchronous.
                def stack_factory(core):
                    return durable_stack(profile, core)

                def ready(url):
                    path = profile.path / "session.json"
                    path.write_text(json.dumps({"url": url}), encoding="utf-8")
                    path.chmod(0o600)

                try:
                    asyncio.run(
                        run(
                            web,
                            False,
                            stack_factory=stack_factory,
                            provisioner=lambda api, ids, seed: provision_once(
                                profile, api, ids, seed
                            ),
                            on_ready=ready,
                            web_port=profile.state["web_port"],
                            login_hint="Login owner@example.test; private password in profile.json. Ctrl-C stops; data is preserved.",
                        )
                    )
                except KeyboardInterrupt:
                    print(
                        "Stopped own software processes; PostgreSQL volume and private profile preserved."
                    )
    except Exception:
        print(
            "Operation refused or failed. Preserve profile/backup and owned volume; no automatic reset or deletion.",
            file=sys.stderr,
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
