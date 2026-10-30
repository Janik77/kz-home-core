"""Opt-in Docker demo orchestration, Python 3.12+ standard library only."""

import argparse
import csv
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Only these developer-owned messages may reach the failure output. Never print
# exception text, subprocess args/stdout/stderr, environment or private JSON.
DIAGNOSTICS = {
    "arguments": "Use a kzhome-demo[-suffix] project name and distinct ports from 1024 to 65535.",
    "existing-config": "Cannot load existing demo metadata. Preserve private files and volumes; do not regenerate credentials for an existing database.",
    "incomplete-prepare": "The demo directory exists without state.json. A previous prepare may have stopped early. Preserve it; use a NEW --project kzhome-demo-<suffix> or restore its original complete inputs.",
    "web-checkout": "Check Git is on PATH and --web points to the separate Git checkout root. Run git -C <web> status locally; review ownership/safe.directory errors without disabling Git protections globally.",
    "web-files": "Web must contain tracked package.json, package-lock.json and index.html. A directory existing is insufficient; use the complete reviewed Web checkout.",
    "web-dirty": "Web has tracked changes. Preserve them; use a separate clean reviewed checkout. Untracked/ignored files are not copied.",
    "web-copy": "Cannot snapshot tracked Web files. Check read/write permissions, disk space and source availability; symlinks/path escapes are not accepted.",
    "core-assets": "Required Core demo files are missing or the proxy template changed. Use the complete reviewed Core checkout.",
    "docker-engine": "Run docker info locally. Docker Desktop must be running, accessible to this user and use Linux containers.",
    "compose-version": "Run docker compose version. Compose 2.24.4 or newer is required for !override.",
    "mount-path": "Use a Core checkout path without commas, apostrophes or line breaks (Docker mount/env-file delimiters). Spaces and drive letters are supported.",
    "private-directory": "Cannot create/protect the NEW demo directory. Check disk space and filesystem permissions; do not change existing installation files.",
    "windows-sid": "Cannot identify the Windows account. Run whoami /user locally; a valid account SID is required.",
    "windows-acl": "Cannot restrict the NEW demo directory. Check icacls availability, ownership and Windows ACL support on the drive (normally NTFS); do not bypass this protection.",
    "private-inputs": "Cannot write new demo inputs. Check disk space and write permissions. Preserve any partial directory; no automatic reset is performed.",
    "mqtt-certificates": "Certificate helper failed. Check Docker registry/DNS/proxy access to alpine/openssl, drive sharing/bind-mount permissions and free space. Host OpenSSL is not required.",
    "mqtt-passwords": "Password-file helper failed. Check access to eclipse-mosquitto:2, bind-mount permissions and free space. Never display the password file.",
    "isolation-check": "Compose configuration or resource isolation check failed. Check local config with --quiet and Docker availability; preserve existing resources and never attach non-demo volumes.",
    "operation": "Demo operation failed. Check the guide and local prerequisites; command output and private contents are intentionally withheld.",
    "upgrade": "Upgrade stopped safely. Preserve backups, upgrade.pending.json and all volumes. Run upgrade-check or resume upgrade with the same reviewed source; never reset or regenerate private inputs.",
}


class DemoError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def safe_reason(error):
    if isinstance(error, subprocess.CalledProcessError):
        return f"external command exited with code {int(error.returncode)}"
    if isinstance(error, subprocess.TimeoutExpired):
        return "external command timed out"
    if isinstance(error, FileNotFoundError):
        return "required executable or file not found"
    if isinstance(error, PermissionError):
        return "access denied"
    if isinstance(error, (json.JSONDecodeError, KeyError, TypeError)):
        return "invalid or incomplete configuration"
    if isinstance(error, KeyboardInterrupt):
        return "interrupted"
    if isinstance(error, OSError):
        return "filesystem or operating-system error"
    return "validation or operation failed"


def command(args, *, cwd=ROOT, env=None, input_text=None, timeout=900):
    result = subprocess.run(
        args,
        cwd=cwd,
        env=env,
        input=input_text,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=timeout,
    )
    if result.returncode:
        # Never echo subprocess environment, credential-bearing args or output.
        raise subprocess.CalledProcessError(result.returncode, args)
    return result.stdout.rstrip("\r\n")


def private_write(path, text):
    path.write_text(text, encoding="utf-8", newline="\n")
    path.chmod(0o600)


def check_web(web):
    web = web.resolve()
    if web == ROOT or not web.is_dir():
        raise DemoError("web-checkout")
    required = {"package.json", "package-lock.json", "index.html"}
    if not all((web / name).is_file() for name in required):
        raise DemoError("web-files")
    root = command(["git", "rev-parse", "--show-toplevel"], cwd=web)
    if Path(root).resolve() != web:
        raise DemoError("web-checkout")
    if command(["git", "status", "--porcelain", "--untracked-files=no"], cwd=web):
        raise DemoError("web-dirty")
    # NUL-delimited names avoid Git quoting non-ASCII Windows filenames.
    filenames = command(["git", "ls-files", "-z"], cwd=web).split("\0")
    if not required.issubset(filenames):
        raise DemoError("web-files")
    return [name for name in filenames if name]


def stage_web(web, target):
    web = web.resolve()
    for filename in check_web(web):
        source = web / filename
        if not source.resolve().is_relative_to(web) or source.is_symlink():
            raise DemoError("web-copy")
        if any(part.startswith(".env") for part in Path(filename).parts):
            continue
        destination = target / filename
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
    shutil.copyfile(ROOT / "deploy/demo-web.Dockerfile", target / "Dockerfile")
    return command(["git", "rev-parse", "HEAD"], cwd=web)


def project_path(project):
    if not re.fullmatch(r"kzhome-demo(?:-[a-z0-9]+)*", project):
        raise ValueError(
            "Only kzhome-demo or kzhome-demo-<suffix> project names allowed"
        )
    path = ROOT / "deploy/local/demo" / project
    if not path.resolve().is_relative_to((ROOT / "deploy/local/demo").resolve()):
        raise ValueError("Invalid demo path")
    return path


def proxy_config(source):
    if (
        "listen 443 ssl;" not in source
        or "proxy_pass http://kzhome_core/;" not in source
    ):
        raise ValueError(
            "Review changed ARM64 proxy template before generating demo config"
        )
    lines = [
        line for line in source.splitlines() if not line.strip().startswith("ssl_")
    ]
    return (
        "\n".join(lines)
        .replace("listen 443 ssl;", "listen 8080;")
        .replace("X-Forwarded-Proto https;", "X-Forwarded-Proto http;")
        + "\n"
    )


class Demo:
    def __init__(self, project):
        self.project = project
        self.path = project_path(project)
        self.state = None
        self.env = None
        self.step = "operation"

    def preparing(self, step):
        self.step = step
        print(f"STEP [{step}]", flush=True)

    def load(self):
        previous_step = self.step
        self.step = "existing-config"
        if self.path.is_dir() and not (self.path / "state.json").exists():
            raise DemoError("incomplete-prepare")
        self.state = json.loads((self.path / "state.json").read_text(encoding="utf-8"))
        if self.state["project"] != self.project or self.state.get("version") != 14:
            raise ValueError("Wrong demo installation metadata")
        self.env = {
            **os.environ,
            **self.state["env"],
            "COMPOSE_PROJECT_NAME": self.project,
        }
        # These must never come from a developer's unrelated process environment.
        self.env.pop("COMPOSE_FILE", None)
        self.env.pop("COMPOSE_PROFILES", None)
        self.step = previous_step

    def dc(self, *args, input_text=None):
        return command(self.argv(*args), env=self.env, input_text=input_text)

    def argv(self, *args):
        if self.state is None:
            self.load()
        files = ["compose.production.yaml"]
        if self.state["env"]["DEMO_PLATFORM"] == "linux/arm64":
            files.append("compose.arm64.yaml")
        files.append("compose.demo.yaml")
        argv = [
            "docker",
            "compose",
            "-p",
            self.project,
            "--env-file",
            str(self.path / "compose.env"),
        ]
        for filename in files:
            argv += ["-f", filename]
        return [*argv, *args]

    def guard(self):
        """Never attach an existing non-demo volume or fixed-name container."""
        previous_step = self.step
        self.step = "isolation-check"
        config = json.loads(self.dc("config", "--format", "json"))
        for key, volume in config["volumes"].items():
            expected = self.project + "_" + key
            if volume.get("external") or volume["name"] != expected:
                raise ValueError("Demo volume isolation failed")
            result = subprocess.run(
                ["docker", "volume", "inspect", expected],
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                labels = json.loads(result.stdout)[0].get("Labels") or {}
                if labels.get("com.docker.compose.project") != self.project:
                    raise ValueError("Existing volume is not owned by this demo")
        for name in ("postgres", "mosquitto"):
            if config["services"][name].get("ports"):
                raise ValueError("Database/broker must not be published")
        for name in ("core", "proxy"):
            if any(
                p["host_ip"] != "127.0.0.1" for p in config["services"][name]["ports"]
            ):
                raise ValueError("Demo must remain loopback-only")
        self.step = previous_step

    def prepare(self, web, platform, api_port, web_port):
        if self.path.exists():
            self.load()  # No rotation, overwrite, recopy or automatic partial-reset.
            self.guard()
            print(
                "Existing demo configuration preserved; use update to refresh sources."
            )
            return
        web = web.resolve()
        self.preparing("web-checkout")
        check_web(web)
        self.preparing("core-assets")
        required = [
            "compose.production.yaml",
            "compose.demo.yaml",
            "deploy/demo-web.Dockerfile",
            "deploy/mosquitto/acl",
            "deploy/mosquitto/start.sh",
            "deploy/mosquitto/mosquitto.conf",
            "deploy/nginx/arm64.conf",
        ]
        if platform == "linux/arm64":
            required.append("compose.arm64.yaml")
        if not all((ROOT / name).is_file() for name in required):
            raise DemoError("core-assets")
        proxy = proxy_config(
            (ROOT / "deploy/nginx/arm64.conf").read_text(encoding="utf-8")
        )
        if any(char in str(self.path) for char in ",\r\n'"):
            raise DemoError("mount-path")
        self.preparing("docker-engine")
        if (
            command(["docker", "info", "--format", "{{.OSType}}"], timeout=30)
            != "linux"
        ):
            raise DemoError("docker-engine")
        self.preparing("compose-version")
        version = command(["docker", "compose", "version", "--short"], timeout=30)
        match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)(?:[-+].*)?", version)
        if match is None or tuple(map(int, match.groups())) < (2, 24, 4):
            raise DemoError("compose-version")
        # Record only already-read tracked source. No .git, ignored env, dist or dependencies.
        self.preparing("private-directory")
        self.path.mkdir(parents=True, mode=0o700)
        self.path.chmod(0o700)
        if os.name == "nt":
            self.preparing("windows-sid")
            sid_rows = command(["whoami", "/user", "/fo", "csv", "/nh"]).splitlines()
            sid = next(csv.reader(sid_rows))[1]
            if not re.fullmatch(r"S-1-(?:\d+-)+\d+", sid):
                raise DemoError("windows-sid")
            self.preparing("windows-acl")
            command(
                [
                    "icacls",
                    str(self.path),
                    "/inheritance:r",
                    "/grant:r",
                    f"*{sid}:(OI)(CI)F",
                    "*S-1-5-18:(OI)(CI)F",
                ]
            )
        inputs = self.path / "mqtt"
        self.preparing("web-copy")
        inputs.mkdir()
        revision = stage_web(web, self.path / "web")
        (self.path / "proxy.conf").write_text(proxy, encoding="utf-8", newline="\n")

        self.preparing("private-inputs")

        def secret():
            return secrets.token_hex(32)

        app_password = secret()
        core_password, relay_password, motion_password = secret(), secret(), secret()
        credentials = {
            "email": "demo-" + uuid4().hex + "@example.invalid",
            "password": secret(),
        }
        private_write(self.path / "user.json", json.dumps(credentials))
        private_write(
            self.path / "simulation.json",
            json.dumps({"relay": relay_password, "motion": motion_password}),
        )
        values = {
            "POSTGRES_PASSWORD": secret(),
            "DATABASE_URL": f"postgresql+psycopg://kzhome:{app_password}@postgres:5432/kzhome",
            "AUTH_JWT_SECRET": secret(),
            "MQTT_PASSWORD": core_password,
            "MQTT_CLIENT_ID": self.project + "-core",
            "TZ": "UTC",
            "MQTT_ACL_FILE": str(inputs / "acl").replace("\\", "/"),
            "DEMO_INPUT_DIR": str(self.path).replace("\\", "/"),
            "DEMO_WEB_CONTEXT": str(self.path / "web").replace("\\", "/"),
            "DEMO_PLATFORM": platform,
            "DEMO_CORE_IMAGE": "kz-home-core:" + self.project + "-v0.14",
            "DEMO_WEB_IMAGE": "kz-home-web:" + self.project + "-v0.14",
            "DEMO_API_PORT": str(api_port),
            "DEMO_WEB_PORT": str(web_port),
            "HTTPS_BIND_IP": "127.0.0.1",
            "CORE_ARM64_IMAGE": "kz-home-core:" + self.project + "-v0.14",
        }
        private_write(
            self.path / "compose.env",
            "\n".join(f"{k}='{v}'" for k, v in values.items()) + "\n",
        )
        private_write(
            inputs / "passwords",
            f"kzhome-core:{core_password}\nrelay-simulator:{relay_password}\nmotion-simulator:{motion_password}\n",
        )
        shutil.copyfile(ROOT / "deploy/mosquitto/acl", inputs / "acl")
        (inputs / "server.ext").write_text(
            "subjectAltName=DNS:mosquitto\nbasicConstraints=CA:FALSE\nkeyUsage=digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\n"
        )
        mount = f"type=bind,source={inputs},target=/bootstrap"
        self.preparing("mqtt-certificates")
        command(
            [
                "docker",
                "run",
                "--rm",
                "--network",
                "none",
                "--mount",
                mount,
                "--entrypoint",
                "/bin/sh",
                "alpine/openssl",
                "-c",
                "umask 077; openssl req -x509 -newkey rsa:2048 -nodes -days 365 -subj /CN=KZHome-Demo-CA -keyout /bootstrap/ca.key -out /bootstrap/ca.crt && "
                "openssl req -newkey rsa:2048 -nodes -subj /CN=mosquitto -keyout /bootstrap/server.key -out /bootstrap/server.csr && "
                "openssl x509 -req -in /bootstrap/server.csr -CA /bootstrap/ca.crt -CAkey /bootstrap/ca.key -CAcreateserial -days 365 -extfile /bootstrap/server.ext -out /bootstrap/server.crt && chmod 644 /bootstrap/ca.crt /bootstrap/server.crt",
            ]
        )
        self.preparing("mqtt-passwords")
        command(
            [
                "docker",
                "run",
                "--rm",
                "--network",
                "none",
                "--user",
                "0:0",
                "--mount",
                mount,
                "--entrypoint",
                "mosquitto_passwd",
                "eclipse-mosquitto:2",
                "-U",
                "/bootstrap/passwords",
            ]
        )
        self.preparing("private-inputs")
        state = {
            "version": 14,
            "project": self.project,
            "env": values,
            "application_password": app_password,
            "web_source": str(web),
            "web_revision": revision,
        }
        private_write(self.path / "state.json", json.dumps(state))
        self.load()
        self.guard()
        print(
            "Prepared private inputs and read-only Web source snapshot. Secrets were not printed."
        )

    def bootstrap(self):
        self.guard()
        result = self.dc(
            "run",
            "--rm",
            "--no-deps",
            "-T",
            "core",
            "python",
            "-m",
            "app.demo",
            "bootstrap",
            input_text=(self.path / "user.json").read_text(),
        )
        print(result)

    def start(self, *, build=False):
        self.guard()
        if build:
            if self.dc("ps", "-a", "-q", "core"):
                raise DemoError("upgrade")
            print(
                "Building Core and the staged Web source; this can take several minutes."
            )
            self.dc("build", "core", "proxy")
        # A repeat start must not migrate underneath the running API.
        self.dc("stop", "proxy", "core")
        self.dc("up", "-d", "--wait", "postgres", "mosquitto")
        exists = self.dc(
            "exec",
            "-T",
            "postgres",
            "psql",
            "-U",
            "postgres",
            "-d",
            "kzhome",
            "-Atc",
            "SELECT 1 FROM pg_roles WHERE rolname='kzhome'",
        )
        if exists != "1":
            password = self.state["application_password"]
            if not re.fullmatch("[a-f0-9]{64}", password):
                raise ValueError("Invalid generated database credential")
            self.dc(
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
                input_text=f"CREATE ROLE kzhome LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD '{password}';\nALTER DATABASE kzhome OWNER TO kzhome;\n",
            )
        self.dc(
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
        self.bootstrap()
        self.dc("up", "-d", "--wait", "core", "proxy")
        self.verify()

    def update(self):
        raise DemoError("upgrade")  # Legacy unsafe update is superseded explicitly.

    def upgrade_check(self, web):
        from deploy.demo_upgrade import check

        return check(self, web)

    def backup(self):
        from deploy.demo_upgrade import backup, operation_lock

        with operation_lock(self):
            return backup(self)

    def upgrade(self, web):
        from deploy.demo_upgrade import upgrade

        return upgrade(self, web)

    def virtual_relay(self, mode):
        self.guard()
        name = self.project + "-virtual-relay"
        result = subprocess.run(
            ["docker", "container", "inspect", name], capture_output=True
        )
        if result.returncode == 0:
            raise DemoError("upgrade")  # Never silently replace a running simulator.
        secret = json.loads((self.path / "simulation.json").read_text())["relay"]
        config = json.dumps({"password": secret, "mode": mode})
        print(
            "Virtual relay running in foreground; use virtual-relay-stop from another terminal. Software only.",
            flush=True,
        )
        command(
            [
                "docker",
                "run",
                "--rm",
                "-i",
                "--init",
                "--name",
                name,
                "--label",
                "kzhome.demo.virtual=" + self.project,
                "--network",
                self.project + "_backend",
                "-e",
                "KZHOME_DEMO_ENABLED=1",
                "--mount",
                f"type=bind,source={self.path / 'mqtt/ca.crt'},target=/run/mqtt/ca.crt,readonly",
                self.state["env"]["DEMO_CORE_IMAGE"],
                "python",
                "-m",
                "app.demo_relay",
            ],
            input_text=config,
            timeout=86400,
        )

    def virtual_relay_stop(self):
        name = self.project + "-virtual-relay"
        item = json.loads(command(["docker", "inspect", name]))[0]
        if item["Config"]["Labels"].get("kzhome.demo.virtual") != self.project:
            raise DemoError("upgrade")
        command(["docker", "stop", "--time", "10", name])

    def verify(self):
        self.guard()
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        base = "http://127.0.0.1:" + self.state["env"]["DEMO_WEB_PORT"]
        for path in ("/", "/rooms/demo", "/core-api/health", "/core-api/ready"):
            with opener.open(base + path, timeout=10) as response:
                if response.status != 200:
                    raise ValueError("Demo HTTP verification failed")
        print(self.dc("ps", "-a"))
        print("PASS: Web routing and Core health/readiness. Open " + base)

    def simulate(self, scenario):
        self.guard()
        args = [
            "docker",
            "run",
            "--rm",
            "-i",
            "--init",
            "--no-healthcheck",
            "--network",
            self.project + "_backend",
            "-e",
            "KZHOME_DEMO_ENABLED=1",
        ]
        for source, target in (("mqtt/ca.crt", "/run/mqtt/ca.crt"),):
            args += [
                "--mount",
                f"type=bind,source={self.path / source},target={target},readonly",
            ]
        args += [
            self.state["env"]["DEMO_CORE_IMAGE"],
            "python",
            "-m",
            "app.demo",
            scenario,
        ]
        credentials = json.loads((self.path / "user.json").read_text())
        credentials["simulation"] = json.loads(
            (self.path / "simulation.json").read_text()
        )
        print(command(args, timeout=180, input_text=json.dumps(credentials)))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        choices=(
            "prepare",
            "start",
            "stop",
            "restart",
            "update",
            "bootstrap",
            "simulate",
            "verify",
            "credentials",
            "upgrade-check",
            "backup",
            "upgrade",
            "virtual-relay",
            "virtual-relay-stop",
        ),
    )
    parser.add_argument("--project", default="kzhome-demo")
    parser.add_argument("--web", type=Path, default=ROOT.parent / "kz-home-web")
    parser.add_argument("--arm64", action="store_true")
    parser.add_argument("--api-port", type=int, default=18080)
    parser.add_argument("--web-port", type=int, default=18081)
    parser.add_argument("--scenario", choices=("relay", "automation"), default="relay")
    parser.add_argument(
        "--relay-mode", choices=("apply", "fail", "timeout"), default="apply"
    )
    parser.add_argument(
        "--run",
        action="store_true",
        help="explicit opt-in for local Docker demo operations",
    )
    args = parser.parse_args()
    if not args.run:
        parser.error("Explicit --run required")
    demo = None
    try:
        demo = Demo(args.project)
        if args.action == "prepare":
            if (
                not 1024 <= args.api_port <= 65535
                or not 1024 <= args.web_port <= 65535
                or args.api_port == args.web_port
            ):
                raise DemoError("arguments")
            platform = "linux/arm64" if args.arm64 else "linux/amd64"
            demo.prepare(args.web, platform, args.api_port, args.web_port)
        else:
            demo.load()
            demo.guard()
            if args.action == "credentials":
                if not sys.stdout.isatty():
                    raise ValueError(
                        "Credentials can only be shown deliberately in an interactive terminal"
                    )
                credentials = json.loads((demo.path / "user.json").read_text())
                print(
                    "DEMO ONLY email: "
                    + credentials["email"]
                    + "\nDEMO ONLY password: "
                    + credentials["password"]
                )
            elif args.action == "start":
                demo.start(build=True)
            elif args.action == "stop":
                demo.dc("stop")
                print("Stopped; containers, secrets and persistent volumes preserved.")
            elif args.action == "restart":
                demo.dc("stop")
                demo.start()
            elif args.action == "update":
                demo.update()
            elif args.action == "upgrade-check":
                demo.upgrade_check(args.web)
                print("PASS: upgrade prerequisites; no deployment changes")
            elif args.action == "backup":
                demo.backup()
                print(
                    "PASS: private backup retained under this project's backups directory"
                )
            elif args.action == "upgrade":
                demo.upgrade(args.web)
            elif args.action == "virtual-relay":
                demo.virtual_relay(args.relay_mode)
            elif args.action == "virtual-relay-stop":
                demo.virtual_relay_stop()
            elif args.action == "bootstrap":
                demo.bootstrap()
            elif args.action == "verify":
                demo.verify()
            else:
                demo.simulate(args.scenario)
    except (Exception, KeyboardInterrupt) as error:
        code = (
            error.code
            if isinstance(error, DemoError)
            else (demo.step if demo is not None else "arguments")
        )
        # A step can only select a fixed message; never render dynamic error text.
        hint = DIAGNOSTICS.get(code, DIAGNOSTICS["operation"])
        if code not in DIAGNOSTICS:
            code = "operation"
        print(
            f"FAIL [{code}]: {hint} Reason: {safe_reason(error)}. "
            "No automatic reset performed."
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
