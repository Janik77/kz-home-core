# Integrated durable software scenes (test only)

This new branch `codex/integrated-scene-recovery` combines:
- Core #39 runtime `f946a07f67f9ccb1e8f62c99860a0842bf3a062b` (including #37/#36).
- Core #38 persistent tooling `4ce83504b80c15e951f011b868d68c5bc5aea178`.
- Unmodified Web #19 `eab81068be474455e1392dd3ed05cf4917abe02c`
  (includes Web #17 `e3f82642c3271c8885f0de8dd3f3e2d3ee6b9dc4`).

Runtime remains exactly Core #39; migration is **0010_scene_runs**. New integration
commits update tooling/pins, documentation and CI acceptance. Source branches and
main are not changed. No merge, deploy or physical qualification is implied.
The draft PR records the final tested commit and fresh CI links.

## Fresh checkout and disposable smoke

Use an ordinary Linux user with Python 3.12/venv, Node 24/npm and already-authorized
Docker. No sudo, extra privileges, production credentials, .env or device mounts.
For a persistent profile use Ubuntu WSL with Docker Desktop as described below;
do not use the orchestrator VM, kzhome-local or mentoring demo.

```bash
git clone --branch codex/integrated-scene-recovery https://github.com/Janik77/kz-home-core.git core-integration
git -C core-integration rev-parse HEAD
git clone https://github.com/Janik77/kz-home-web.git web-integration
git -C web-integration checkout --detach eab81068be474455e1392dd3ed05cf4917abe02c
python3.12 -m venv .venv-integration
.venv-integration/bin/python -m pip install -r core-integration/requirements-dev.txt
.venv-integration/bin/python -m pip check
npm --prefix web-integration ci
docker pull postgres:17-bookworm@sha256:665dc7b37ac52a17e298511414c21d23ffa4e6a6ef6225b3e5d59c7afb35e994
docker pull eclipse-mosquitto:2@sha256:75d6662dee52c947dc9dd9c29f1d8d7ffef78ddaf38001caa09755681e8bbc98
docker pull alpine/openssl:latest@sha256:6821afa2476e11be343f29275f2ca24291720ff0a7148bd131fd8e314ff26d9e
.venv-integration/bin/python -B core-integration/tools/run_software_stack.py --web-root web-integration --smoke-test
```

Record/pin the exact reviewed integration SHA from the PR before use. The smoke
fixture deletes ONLY its own temporary data. It is not the persistent mode.

## Persistent profile

Use a new directory and the same pinned checkouts above:

```bash
mkdir -p "$HOME/.local/share/kzhome-software"
.venv-integration/bin/python -B core-integration/tools/durable_software.py init --state "$HOME/.local/share/kzhome-software/scenes-profile"
.venv-integration/bin/python -B core-integration/tools/durable_software.py start --state "$HOME/.local/share/kzhome-software/scenes-profile" --web-root web-integration
```

Open the printed loopback URL. Login owner@example.test; generated password is in
the private profile.json. Ctrl-C stops owned processes/containers and retains data.
See [DURABLE_SOFTWARE_WSL.md](DURABLE_SOFTWARE_WSL.md) for exact stop, backup,
restore, credential retrieval and protection rules.

There is **no automatic migration** of old draft profiles/backups to this new runtime
pin. Do not edit source pins, directory path/device/inode bindings, database identity
or resource labels to force startup. Restore only a trusted backup from the supported
new combination into a new profile. Preserve old data for separately reviewed migration.

## Scene semantics and evidence

Web #19 exposes confirmed caller-driven scene runs from Core #39. A run snapshots
a scene revision. Each ordered action needs explicit dispatch and applied ACK plus
matching state; later actions cannot progress before prior confirmation.
A scene is succeeded only when all its action receipts are applied.
See [DURABLE_SCENE_RUNS.md](DURABLE_SCENE_RUNS.md) for deadlines, unknown outcomes,
permissions and lack of automatic replay/compensation.

CI on this integration runs Core unit/PostgreSQL/MQTT coverage, Web unit/build and
demo/contract browser suites, the actual Web #19 live browser stack, Core's actual
scene restart/TLS acceptance, and persistent-profile restart/backup/restore acceptance.
The latter verifies scene ON before restart, explicit OFF after restart, exact
per-action receipts and preserved terminal run history after restore. It also checks
missing/foreign volumes, copied profiles and original Docker inventory preservation.

Only the existing Python software Relay is used. Firmware C and physical relays
are not exercised here. No user WSL, multi-day soak or power-loss qualification is
claimed. Browser-only preferences are not DB backups; relay ON/replay memory resets
on restart. Host sleep/network failure may stop the foreground launcher. An old
green dependency run is not evidence for the final integration SHA.


## Web recovery integration review status

This branch is stacked on Core #40 `29e509595d8096f7bdc24a4470b8b0762603e42c`,
with unmodified Web #19 `eab81068be474455e1392dd3ed05cf4917abe02c`.
Independent Web review is pending; integration test success is not final acceptance.
New preparation and recent run-ID history are read-only with respect to MQTT:
only a separately authorized action dispatch may publish. The history belongs to
the current browser context, is not browser-persisted, and does not replace durable
Core receipts. Fresh preparation after unknown/timed-out outcomes requires explicit
acknowledgment; a real revision conflict requires separately reviewing fresh data.
Use a new profile (for example `recovery-profile`); existing Web #18 profile pins
are not migrated or edited. Core runtime and migration remain #39 / `0010_scene_runs`.
