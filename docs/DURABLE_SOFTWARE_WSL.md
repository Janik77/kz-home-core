# Durable software-only profile for Ubuntu WSL + Docker Desktop

This is a separate opt-in profile beside the disposable smoke launcher in
[UNIFIED_SOFTWARE_MVP.md](UNIFIED_SOFTWARE_MVP.md). It uses the existing Core HTTP
application, PostgreSQL, MQTT TLS transport and Python software Relay, plus the
pinned Web Vite proxy. It is not firmware C execution or physical qualification.

Dependency: Core #36, runtime commit
`6783ce0347004d101ec2ffa5eb60c48946901786`, and Web #15 at
`fac23c233a1c86dad1661a9e2970cfca9450d00d`.
This branch adds lifecycle tooling only; production gates and runtime protocol
are unchanged. Do not merge or deploy this test branch automatically.

## Isolation and prerequisites

Use an ordinary Ubuntu WSL user with already working Docker Desktop WSL
integration, Python 3.12 + venv, Node 24/npm and Git. Use a new checkout and profile
under the Linux home filesystem, not /mnt/c. Do not run on the orchestrator VM,
inside kzhome-local, or in the mentoring demo checkout. No sudo, group/socket
permission changes, extra privileges, existing credentials or device mounts are
needed or permitted by this procedure. If prerequisites are absent, stop.

The tool rejects root, DOCKER_HOST overrides, remote Docker contexts and a daemon
not identified as Docker Desktop. The only native Linux exception is the
disposable GitHub Actions acceptance runner; do not set GITHUB_ACTIONS locally.
It pins the selected Unix-socket context for its operations. It does not switch
contexts, install packages globally, prune resources, use compose or adopt names
from existing installations.

Each profile creates its own random kzsoft-<UUID> container/network/volume names
and verifies its ownership label before stop/start/inspect operations. PostgreSQL
and Mosquitto use a dedicated bridge and random loopback-only published ports.
The bridge retains Docker's standard routing so host port publication works;
no network policy or daemon configuration is changed.
Core and Vite bind loopback. Only the software fixture relay is allowlisted; no
physical device is seeded and broker credentials cannot publish control authority.
Ownership labels are collision protection, not a boundary against a Docker admin.

## Install and initialize once

Run these commands from a new, unused development directory in WSL. Clone paths,
profile path and backup paths must not already contain another installation.

```bash
git clone --branch codex/durable-software-profile https://github.com/Janik77/kz-home-core.git core-durable
git clone https://github.com/Janik77/kz-home-web.git web-durable
git -C web-durable checkout --detach fac23c233a1c86dad1661a9e2970cfca9450d00d
cd core-durable
python3.12 -m venv .venv-durable
.venv-durable/bin/python -m pip install -r requirements-dev.txt
.venv-durable/bin/python -m pip check
npm --prefix ../web-durable ci
mkdir -p "$HOME/.local/share/kzhome-software"
.venv-durable/bin/python -B tools/durable_software.py init --state "$HOME/.local/share/kzhome-software/profile1"
```

Record `git rev-parse HEAD` with your backup and use the reviewed PR commit.
Core runtime directories are checked against the exact base SHA at start. Web
must be at the exact clean pin above, with no .env inputs. Image digests are fixed
in tools/durable_software.py (PostgreSQL 17, Mosquitto 2, OpenSSL helper).
Helpers create new lab-only credentials and a local CA; no installation secrets
are read or modified. The profile is private (directory 0700, private files 0600).

## Start, use, stop, restart

```bash
.venv-durable/bin/python -B tools/durable_software.py start --state "$HOME/.local/share/kzhome-software/profile1" --web-root ../web-durable
```

Keep this foreground process running. Open its printed loopback URL in your
browser; the URL is also saved in profile1/session.json. Login is
owner@example.test; retrieve the generated password locally without posting it:

```bash
.venv-durable/bin/python -c 'import json,pathlib; print(json.loads((pathlib.Path.home()/".local/share/kzhome-software/profile1/profile.json").read_text())["password"])'
```

The Web port is stable for this profile, preserving the browser origin across
ordinary restarts. A port collision refuses startup; it never kills another
process. Use Ctrl-C in the launcher terminal to stop Core, relay and Vite and stop
the owned database/broker containers. Data and profile remain. For a stopped
launcher, these commands inspect or stop residual owned containers:

```bash
.venv-durable/bin/python -B tools/durable_software.py status --state "$HOME/.local/share/kzhome-software/profile1"
.venv-durable/bin/python -B tools/durable_software.py stop --state "$HOME/.local/share/kzhome-software/profile1"
```

An active launcher holds a profile lock: another start, stop or backup is refused.
Stop its foreground process first. Start again with the same command and profile.
After Docker Desktop/WSL restart, manually run start again; no service or automatic
restart policy is installed. Do not use docker compose down -v or docker prune.

House settings, rooms, devices, scenes and scene revisions live in the named
PostgreSQL volume and survive restart. Existing records are not reseeded after
first initialization. Browser-only preferences/localStorage are not database
records and are not included in the backup; retain your browser profile for them.
The Relay state and replay cache are in memory; restart announces software OFF.
MQTT retained/runtime state is not durable. This is not actuator restart safety.

## Backup (stopped profile)

First Ctrl-C and wait for the launcher to finish. Use a new backup destination:

```bash
mkdir -p "$HOME/.local/share/kzhome-software/backups"
.venv-durable/bin/python -B tools/durable_software.py backup --state "$HOME/.local/share/kzhome-software/profile1" --destination "$HOME/.local/share/kzhome-software/backups/backup-001"
```

Backup briefly starts only this profile's database, takes a consistent pg_dump
custom archive, copies the required profile/TLS/broker inputs, writes a SHA-256
manifest last and stops the database in finally. It neither copies a live volume
nor deletes anything. Existing backup destinations are refused. Partial backups
lack a valid manifest and must not be restored.

The backup contains lab passwords, JWT material, password hashes and private CA
keys: keep it private, never commit/upload it as CI evidence. SHA-256 detects
accidental corruption; it is not authentication or encryption. Restore only your
own trusted backup. Filesystem and Docker Desktop disk loss are different risks:
the live installation requires both the profile directory and named volume;
the complete backup is independently restorable. Copying it off-machine requires
your own authorized private backup destination.

## Restore into a NEW isolated profile

```bash
.venv-durable/bin/python -B tools/durable_software.py restore --backup "$HOME/.local/share/kzhome-software/backups/backup-001" --state "$HOME/.local/share/kzhome-software/profile-restored"
.venv-durable/bin/python -B tools/durable_software.py start --state "$HOME/.local/share/kzhome-software/profile-restored" --web-root ../web-durable
```

Restore verifies the exact manifest whitelist, hashes and source pins before
creating the target. It refuses existing paths and symlinks, creates a new owner,
network and empty named volume, and uses pg_restore --exit-on-error. Original
profile/volume remain untouched. Restored data and credentials are the same;
the new profile gets its own Web port, so browser-local preferences do not
automatically migrate. Incomplete restore is not marked initialized and cannot
start. Preserve it for diagnosis; there is no automatic reset or public delete
command. Upgrades/downgrades and cross-version restore are not covered.

## Evidence and limits

The dedicated Durable software profile CI runs actual Uvicorn HTTP through Vite,
PostgreSQL and MQTT TLS with the existing software Relay. It checks ON/OFF linked
ACK/state receipts, a periodic heartbeat after 65 seconds, modified house settings,
new room, revised scene across stop/start, pg_dump/restore into another profile,
source independence, active-profile backup rejection, existing-target rejection,
corruption rejection and zero physical inventory. Cleanup is restricted to fresh
test profiles under its own temporary root with ownership checks, then verifies
the original container/volume/network/image inventory is unchanged. Private
profiles/backups are not uploaded.

A passing short acceptance is not a multi-day soak test, browser settings backup,
a Docker Desktop power-loss test or validation on the user's WSL machine.
Existing unified software CI retains the disposable smoke and real browser suite.
The TLS certificate lasts 365 days; renewal is not automated. Network failure and
host sleep may terminate the foreground launcher: restart it with the same
profile; do not reset data. This mode does not operate real relays, run the C
worker, grant control authority, change production, main or the orchestrator VM.


## Fail-closed storage identity and copied directories

Only init and restore allocate a new database volume. Opening an initialized
profile (including start and backup) requires its existing labelled volume and
network. If storage is lost, no replacement volume is created and no backup
destination is created. Recover with a previously verified backup into a NEW
profile. A missing volume is data loss, not an empty new home.

Profiles record the PostgreSQL cluster system identifier. Start verifies it;
seeded starts and backups also require the exact pinned Alembic revision
0009_definition_revisions. Backup validates these before creating its destination.
Restore records the newly created cluster identity and validates the restored
migration before marking initialization complete.

The profile JSON binds the absolute directory path and filesystem device/inode,
and every Docker resource carries a hash of that binding. A replacement directory
at the original pathname is also refused. Copying a profile directory is not a supported
clone or relocation: the unchanged copy is refused before Docker access; editing
its path and directory-identity fields still fails the resource label check. Use backup/restore, which
assigns a new owner/path and new resources. These checks prevent accidental
adoption, not a malicious administrator with Docker/file write access.

Profiles created by the earlier draft before these identity fields/labels are
refused. Do not edit JSON/labels to bypass the checks, reset or delete their data.
Preserve such profiles for reviewed migration; automatic in-place upgrade of
old draft profiles is intentionally not implemented.

Mosquitto receives only six read-only file mounts: public CA certificate,
server certificate/key, password hashes, ACL and broker configuration. It cannot
see profile.json/JWT, the CA private key, backups or the entire profile directory.
The server TLS private key remains necessary for the TLS listener.
