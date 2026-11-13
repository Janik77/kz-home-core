# Unified KZ Home software integration (test only)

This separate branch combines reviewed Core features without modifying main:

| Source | Exact commit | Included |
| --- | --- | --- |
| main | 2359807c2781ce956ed43bc603772b66dd27955b | Original baseline, still unchanged when assembled |
| #32 | 159eb98f2349b1aa2b8afd313c3a3f1fbf9b6bd2 | CI and migration tests |
| #33 | bbb9d7af8647ecc727b9c8201a936f66fa99a91e | MQTT SUBACK readiness validation |
| #35 | 9a6283c260c92acfe0447b0c425668cbb1a64002 | Atomic definition revisions, migration 0009 |
| #34 | 9f4cddadcded75d8613d5ec9f4b5e6b342ae066d | Real Mosquitto SUBACK/TLS tests and broker CI job |
| Web #14 | fac23c233a1c86dad1661a9e2970cfca9450d00d | Existing UI, revisions, unsaved drafts, isolated live fixture |

The workflow conflict between #35's PostgreSQL additions and #34's broker job
was resolved by preserving both. Historical Core #14 is not included. Earlier
per-PR documents remain historical evidence; current integration CI and its
exact head are recorded in the new draft PR. No old green run is claimed as
validation of this combined head. Firmware is not required to run this software
stack; its physical qualification gates remain closed.

## Reproducible Linux setup

Use a machine with an already-authorized Docker daemon, Python 3.12, Node 24,
git and native build prerequisites. Do not add users to groups, change daemon
permissions, install privileged helpers, or point at an installed home system.
Use fresh disposable checkouts (no `.env`, credentials or mounted live data):

```sh
git clone https://github.com/Janik77/kz-home-core.git core-integration
git -C core-integration fetch origin codex/unified-software-mvp
git -C core-integration checkout --detach FETCH_HEAD
git -C core-integration rev-parse HEAD
# Record this SHA and compare it with the exact successful head in the draft PR.
# For a durable reproduction, checkout that full SHA instead of the branch tip.
git clone https://github.com/Janik77/kz-home-web.git web-integration
git -C web-integration checkout --detach fac23c233a1c86dad1661a9e2970cfca9450d00d
python3.12 -m venv .venv-integration
.venv-integration/bin/python -m pip install -r core-integration/requirements-dev.txt
.venv-integration/bin/python -m pip check
npm --prefix web-integration ci
docker pull postgres:17-bookworm@sha256:665dc7b37ac52a17e298511414c21d23ffa4e6a6ef6225b3e5d59c7afb35e994
docker pull eclipse-mosquitto:2@sha256:75d6662dee52c947dc9dd9c29f1d8d7ffef78ddaf38001caa09755681e8bbc98
docker pull alpine/openssl:latest@sha256:6821afa2476e11be343f29275f2ca24291720ff0a7148bd131fd8e314ff26d9e
.venv-integration/bin/python -B core-integration/tools/run_software_stack.py --web-root web-integration
```

Open the printed `http://127.0.0.1:<port>` Web URL and use the temporary
owner@example.test login printed only in interactive mode. Data is synthetic,
in memory/tmpfs, and is discarded at shutdown. Ctrl-C stops the owned Web/Core
and relay processes; the fixture removes only ownership-labelled containers,
network and its allocated temporary directory. Do not use docker prune or remove
unrelated resources. The launcher compares the entire existing Docker inventory
before and after, using the pinned Web fixture's verifier.

The launcher requires the exact clean Web SHA above, rejects Web `.env*` inputs
(except tracked `.env.example`), and sets an explicit local proxy/real Core mode.
It generates ephemeral DB/MQTT/JWT credentials and CA within the fixture, not
repository secrets. No production URL is accepted. Core uses its standard
production-mode validation to require PostgreSQL and TLS; this is not a
production deployment. Listeners bind only loopback. The MQTT allowlist contains
only fixture_house/fixture_relay. No physical identity or command permit exists.
The software Relay implements the real MQTT protocol but does not actuate GPIO.

## Automated verification

Smoke-test the same launcher without printing fixture credentials:

```sh
.venv-integration/bin/python -B core-integration/tools/run_software_stack.py --web-root web-integration --smoke-test
```

This starts Web and checks actual proxy login, ON/OFF commands and applied
ACK/state receipts, then cleans up. Startup first runs 11 actual PostgreSQL
revision tests and migrates its fresh database to the current Alembic head.
For the full browser acceptance, install only the user-space Chromium bundle
on a Linux host with its existing required system libraries:

```sh
(cd web-integration && npx playwright install chromium)
(cd web-integration && LIVE_CORE_SOURCE="$(realpath ../core-integration)" ../.venv-integration/bin/python -B e2e/confirmed-live-runner.py)
(cd core-integration && ../.venv-integration/bin/python -m pytest -q -ra)
```

No separate #33 patch application is needed: the integration already contains
it. Do not run production Compose files. A missing Docker/browser prerequisite
is a blocker to report, not a reason to escalate privileges or substitute mocks.
The launcher currently supports POSIX process groups, not native Windows.

`Unified software stack` CI checks this exact Core checkout against the pinned
Web: typecheck, lint, unit/build, evidence-verifier negative tests, launcher smoke,
and the five real-browser scenarios. The live verifier requires all 18 unique
MQTT SET/SQL receipts: 16 applied with matching ACK/state, one device-failed ACK,
and one actual timeout. It also covers definition revisions/409, drafts, roles,
revocation and mobile controls. No fabricated HTTP/MQTT responses are substituted.
Evidence JSON/screenshots are uploaded without trace/video or fixture secrets.

The separate `Core CI` workflow retains full offline checks, PostgreSQL migration
round trips and concurrency, and eight real-broker SUBACK/TLS tests from #34.
The offline suite's service skips are reported separately; a skip is not a pass.
The broker test's deterministic ACL plugin is test policy, not production ACL.

## Boundaries

This is a disposable software MVP and test integration, not firmware or hardware
acceptance. No production secrets/data, host devices, new privileges, gate
changes, main merge or deployment. Existing PRs and branches are preserved.
Definition edits require revision-aware Web; legacy clients and mixed old/new
Core writers do not acquire stale-draft guarantees. Counters reset on record
recreation/database restore; see DEFINITION_REVISIONS.md. No production
migration is performed by these instructions.
