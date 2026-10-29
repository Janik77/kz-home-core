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


def command(args, *, cwd=ROOT, env=None, input_text=None, timeout=900):
    result = subprocess.run(
        args,
        cwd=cwd,
        env=env,
        input=input_text,
        text=True,
        capture_output=True,
        timeout=timeout,
    )
    if result.returncode:
        # Never echo subprocess environment, credential-bearing args or output.
        raise RuntimeError(
            "Command failed; check Docker, port availability and prerequisites (details withheld)"
        )
    return result.stdout.strip()


def private_write(path, text):
    path.write_text(text, encoding="utf-8", newline="\n")
    path.chmod(0o600)


def stage_web(web, target):
    web = web.resolve()
    if web == ROOT or not (web / "package-lock.json").is_file():
        raise ValueError("Provide the separate kz-home-web checkout")
    if command(["git", "status", "--porcelain", "--untracked-files=no"], cwd=web):
        raise ValueError("Web tracked working tree must be clean")
    for filename in command(["git", "ls-files"], cwd=web).splitlines():
        source = web / filename
        if not source.resolve().is_relative_to(web) or source.is_symlink():
            raise ValueError("Web source symlinks are not accepted")
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

    def load(self):
        self.state = json.loads((self.path / "state.json").read_text())
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

    def dc(self, *args, input_text=None):
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
        return command([*argv, *args], env=self.env, input_text=input_text)

    def guard(self):
        """Never attach an existing non-demo volume or fixed-name container."""
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

    def prepare(self, web, platform, api_port, web_port):
        if self.path.exists():
            self.load()  # No rotation, overwrite, recopy or automatic partial-reset.
            self.guard()
            print(
                "Existing demo configuration preserved; use update to refresh sources."
            )
            return
        web = web.resolve()
        if web == ROOT or not (web / "package-lock.json").is_file():
            raise ValueError("Provide the separate kz-home-web checkout")
        if command(["git", "status", "--porcelain", "--untracked-files=no"], cwd=web):
            raise ValueError("Web tracked working tree must be clean")
        # Record only already-read tracked source. No .git, ignored env, dist or dependencies.
        self.path.mkdir(parents=True, mode=0o700)
        self.path.chmod(0o700)
        if os.name == "nt":
            sid_rows = command(["whoami", "/user", "/fo", "csv", "/nh"]).splitlines()
            sid = next(csv.reader(sid_rows))[1]
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
        inputs.mkdir()
        revision = stage_web(web, self.path / "web")
        (self.path / "proxy.conf").write_text(
            proxy_config((ROOT / "deploy/nginx/arm64.conf").read_text()), newline="\n"
        )

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
        self.guard()
        # Stage a fresh context rather than deleting/copying over the old snapshot.
        context = self.path / ("web-" + uuid4().hex)
        revision = stage_web(Path(self.state["web_source"]), context)
        self.state["web_revision"] = revision
        self.state["env"]["DEMO_WEB_CONTEXT"] = str(context).replace("\\", "/")
        private_write(self.path / "state.json", json.dumps(self.state))
        private_write(
            self.path / "compose.env",
            "\n".join(f"{key}='{value}'" for key, value in self.state["env"].items())
            + "\n",
        )
        self.load()
        self.start(build=True)

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
        ),
    )
    parser.add_argument("--project", default="kzhome-demo")
    parser.add_argument("--web", type=Path, default=ROOT.parent / "kz-home-web")
    parser.add_argument("--arm64", action="store_true")
    parser.add_argument("--api-port", type=int, default=18080)
    parser.add_argument("--web-port", type=int, default=18081)
    parser.add_argument("--scenario", choices=("relay", "automation"), default="relay")
    parser.add_argument(
        "--run",
        action="store_true",
        help="explicit opt-in for local Docker demo operations",
    )
    args = parser.parse_args()
    if not args.run:
        parser.error("Explicit --run required")
    try:
        demo = Demo(args.project)
        if args.action == "prepare":
            if (
                not 1024 <= args.api_port <= 65535
                or not 1024 <= args.web_port <= 65535
                or args.api_port == args.web_port
            ):
                raise ValueError("Use distinct unprivileged local ports")
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
            elif args.action == "bootstrap":
                demo.bootstrap()
            elif args.action == "verify":
                demo.verify()
            else:
                demo.simulate(args.scenario)
    except (Exception, KeyboardInterrupt):
        print(
            "FAIL: demo operation stopped. Check documented prerequisites/ports and private installation files; no automatic reset performed."
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
