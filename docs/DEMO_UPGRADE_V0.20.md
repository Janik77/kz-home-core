# Demo v0.20 — audit before deployment changes

Both repositories started clean: Core feature/demo-upgrade-v0.20, Web main.
No branch switches. Reviewed deploy/demo.py, production/demo Compose, Dockerfiles,
Web staging, app.demo bootstrap/scenarios, MQTT Relay/protocol/ACL, tests and mentor
guide. No existing installation has been upgraded during discovery.

The demo Web is an immutable copy of tracked files at prepare/update time; .env,
ignored files and Git metadata are excluded. Repeating prepare preserves it.
The local saved check project records Web 341f8b1724a21bb4c9509f33d2f624008e804f86,
whereas current Web is 809266f5c0e44f2a2b591c494c62b692429fbce0. Image tag v0.14
is a historical tag, not proof of code version. Older Web intentionally renders
read-only controls; newer Web requires current permissions API and an online,
boolean-state relay. Offline status independently prevents control.

kzhome-demo-mentor2 is not present on this machine, so its exact image/source
provenance cannot be proven here. Its reported read-only UI is consistent with
an old staged Web, but must be checked on the mentor's host using saved metadata
and actual endpoints. Do not infer an exact version from the tag.

Existing start/update stops demo API/proxy, starts infrastructure, creates the
non-superuser application DB role if absent, explicitly runs Alembic upgrade head
and reruns idempotent bootstrap. Bootstrap creates an ordinary owner of e2e_house
and preserves its password/observed state on repeat, but rejects foreign data.
The account has device.read/device.control via existing Core policy. Safe upgrade
must not rerun provisioning or reject legitimate subsequently added user data.

Old update stages new Web and writes metadata before building and has no integrated
backup/journal. It is not an atomic or proven reversible upgrade. Existing data can
be preserved with explicit pg_dump, immutable private inputs, new candidate image
tags, controlled API downtime and forward-only migration. No schema change is
needed for the deployment tooling itself; current Core head is 0006_command_lifecycle.

Existing simulate is a finite operator acceptance scenario and exits offline.
simulator.mqtt_relay.Relay already implements TLS MQTT v1, exact IDs, applied ACK,
correlated state and bounded volatile deduplication. Existing relay-simulator ACL
is limited to e2e_house/e2e_relay. A persistent demo process can reuse it without
DB/JWT credentials or a new protocol. Browser scenes remain intentionally disabled.

## Upgrade implementation and safety

The metadata format remains version 14 for compatibility; this number is NOT an
application version. New explicit actions are upgrade-check, backup, upgrade,
virtual-relay and virtual-relay-stop. Legacy update now fails with instructions
instead of performing its old unbacked metadata replacement. start with a build
refuses an already-created Core container; use the explicit upgrade operation.
Fresh prepare/start and existing restart remain separate operations.

upgrade-check requires a complete clean tracked Web checkout with the verified
permission/relay client layout. It validates project-owned volumes/networks and
containers, loopback endpoints, known 0005/0006 schema, current infrastructure
image identity, production environment and MQTT TLS. The DB URL must be exactly
the prepared non-superuser kzhome role at postgres:5432/kzhome; stored/runtime
DB/JWT/MQTT credentials must agree. Remote DB URLs, TLS changes, foreign ownership,
unsupported schema, missing inputs and incompatible layout fail before stopping
the API. Mosquitto's known implicit anonymous /mosquitto/log mount is accepted;
arbitrary named foreign data mounts are not. No broker/DB image upgrades occur.

upgrade holds an OS-backed exclusive lock that releases on process death. It:

1. Stages current clean Web tracked files into a NEW context and builds uniquely
   tagged candidate Core/Web images before installed metadata/container changes.
2. Writes upgrade.pending.json with previous/target metadata, source fingerprint,
   Web commit and candidate image IDs. Existing image tags/contexts are retained.
3. Stops only that demo's API/proxy and creates a fresh private backup: binary
   pg_dump -Fc, pg_restore --list validation, copies of original state/env/user/
   simulator/proxy/certificate/key/password/ACL inputs and SHA-256 manifest.
   Upgrade keeps API/proxy stopped between this backup and migration. The separate
   backup action restarts the original API/proxy after backup, without recreation.
4. Validates retained backup/private inputs/candidate image IDs, runs explicit
   forward Alembic upgrade head in the new Core image, without bootstrap/seeding.
5. Atomically replaces each metadata file separately, starts candidate API/proxy
   with --no-deps --pull never, checks HTTP routing/health/readiness, and archives
   the journal under upgrade-history. PostgreSQL/Mosquitto remain in place.

Successful repeated invocation for the same source fingerprint and Web revision
only verifies; it does not remigrate/recreate. The Core fingerprint includes
tracked/untracked runtime source and deployment inputs, so development acceptance
does not falsely identify uncommitted code by HEAD alone. This is not a general
future-version compatibility detector: use the reviewed v0.20/Core v0.17/Web v0.19
release set, not an arbitrary future branch or a copied helper on an older Core.

No operation deletes volumes, resets the DB, rotates secrets, updates infrastructure
images, or silently recreates a prepared project. Backups, previous images and
staged contexts are retained. Protect private storage and plan retention separately.
The backup is a DB logical dump plus private configuration; it does NOT archive the
live Mosquitto persistence volume. That volume remains untouched. A full disaster
backup also needs an independently quiesced host/volume snapshot; this helper does
not claim a complete VM backup or tested full disaster restoration.

This is deliberately NOT atomic: builds, process shutdown, migration, two metadata
replacements, startup and verification are separate steps. There is downtime.
Forward-only recovery was tested; automatic DB downgrade/rollback is not offered.
Commands already dispatched before shutdown may execute or time out; no resend
worker or physical cancellation is introduced.

## Persistent virtual relay

virtual-relay launches a uniquely named, labeled, foreground software device in
the selected demo backend network. It receives ONLY the existing relay-simulator
password via stdin, has a read-only CA mount, verifies TLS hostname/CA and uses the
existing directional e2e_house/e2e_relay ACL. No DB/JWT/human credentials enter the
simulator. It sends online after subscription/state readiness, refreshes presence,
sends graceful offline and supplies MQTT LWT for unexpected disconnect.

apply reuses Relay.handle unchanged: validate v1 command, preserve exact command
and correlation IDs, emit applied ACK and correlated reported state. fail emits
matching failed ACK; timeout deliberately withholds ACK/state so the REAL Core
30-second deadline becomes timed_out. Neither fault mode fabricates success.
Restarted software state starts OFF and reports that actual software value; it
does not persist a physical relay value or provide durable deduplication.

Only one simulator for these IDs may run. Never run old simulate scenarios or
another MQTT relay client concurrently. A duplicate named simulator is rejected,
not silently replaced. virtual-relay-stop validates its ownership label before
stopping it. Application logs do not print credentials; the foreground helper is
bounded to 24 hours, and the explicit stop command should be used for shutdown.

## Tested Windows PowerShell procedure

Local acceptance below succeeded. These commands are for a reviewed release,
not an instruction to update the mentor's host from an unpublished working tree.
Maintainer must first supply the exact reviewed Core v0.20 commit and Web
809266f5c0e44f2a2b591c494c62b692429fbce0 (or a separately revalidated successor).
Do not proceed without that release set and a successful prerequisite check.
No production installation or kzhome-local command belongs in this procedure.

Use the existing checkout/Windows account/private folder location; do not move
the prepared directory or regenerate inputs. In the existing mentor PowerShell:

```powershell
$core = 'C:\projects\kz-home-core'
$web = 'C:\projects\kz-home-web'
$project = 'kzhome-demo-mentor2'
Set-Location $core
git status --short --branch
git -C $web status --short --branch
docker version
docker compose version
# Inspect public container identities only; never print docker inspect Env/config.
docker ps -a --format '{{.Names}} {{.Image}} {{.Status}}'
```

Verify the expected four mentor containers and existing private state.json are
present. A clean tree is required; preserve work instead of reset/clean. Fetch
the approved release on the existing main branch (no branch switching):

```powershell
git fetch origin
git -C $web fetch origin
# Only after maintainer confirms the approved release is on these main branches:
git pull --ff-only
git -C $web pull --ff-only
git rev-parse HEAD
git -C $web rev-parse HEAD
```

Compare both public hashes with the delivered tested release set; STOP if they
differ. Fetch/pull is source delivery, not deployment. Do not cherry-pick only the
helper or apply it to an unreviewed newer API/schema. Then, one command at a time:

```powershell
py -3.12 deploy/demo.py upgrade-check --run --project $project --web $web
# Explicit preliminary backup (brief API/proxy downtime; original containers restart).
py -3.12 deploy/demo.py backup --run --project $project
```

Copy the new deploy/local/demo/$project/backups directory and original private
inputs to protected local/offline NTFS storage with access limited to the owner.
The directory contains real passwords, keys and user data: never print, upload,
put it in Git, enable transcripts or send it in chat. Do not delete the original
files or any volume. No manual PostgreSQL user creation or table editing is needed.

```powershell
# This builds first and creates its OWN final consistent DB backup before migration.
py -3.12 -u deploy/demo.py upgrade --run --project $project --web $web
py -3.12 deploy/demo.py verify --run --project $project
Start-Process 'http://127.0.0.1:18081'
```

Use the existing known demo login; do not run credentials in a recording/transcript
or print user.json. In a second PowerShell, keep the software relay running:

```powershell
Set-Location 'C:\projects\kz-home-core'
py -3.12 -u deploy/demo.py virtual-relay --run --project kzhome-demo-mentor2
```

Refresh the Web home if it was opened while the device was offline. SIM relay
should become online, ON/OFF enabled by server permissions. Click ON then OFF;
wait for «Выполнено · данные Core обновлены». HTTP 202 or online alone is not
completion. Other two demo devices may remain offline; temperature is a snapshot.
Scenes remain visible/read-only. To stop the simulator, from the first terminal:

```powershell
py -3.12 deploy/demo.py virtual-relay-stop --run --project $project
```

Refresh Web and verify offline controls are disabled. For deliberate software
fault demonstration, stop apply first, then run in the second terminal with
--relay-mode fail or --relay-mode timeout. A timeout takes the real ~30 seconds,
means unknown outcome and must never display success. These modes are demo-only.
Return to apply after stopping the fault-mode container.

## Recovery from interruption

Preserve upgrade.pending.json, backups, metadata .new files, contexts and image
tags. Do not delete/rename the journal to force progress. The OS lock releases
automatically on process termination; do not run concurrent upgrade/backup commands.

- Before journal creation: candidate builds may remain; installed metadata is
  unchanged. Correct the prerequisite/build error and rerun the same upgrade.
- Journal phase built: a backup may be partial. Rerun upgrade with the same source;
  it creates another backup without deleting the partial one.
- After backed-up/migration/metadata/startup interruption: rerun upgrade with the
  SAME Core source/Web commit. It validates backup hashes and candidate image IDs,
  repeats forward migration safely and converges metadata/startup. This path was
  tested after a real PostgreSQL migration committed and its response was lost.
- Changed source, secrets, image IDs, missing backup or unknown DB revision: fail
  closed. Restore the ORIGINAL complete inputs/exact reviewed source/images from
  retained copies with operator assistance. Do not create new passwords for old DB
  data, prune images, downgrade a populated command ledger or reset volumes.

There is no tested one-command disaster restore. If forward recovery is impossible,
leave the original stopped/intact and plan a dedicated side-by-side restore drill;
never pg_restore --clean into the only existing database. The preliminary backup
and upgrade's final backup may differ because the API resumes between them.
During a pending upgrade, do not run bootstrap/start/restart/update as a workaround.

## Exact verification and evidence

- Full normal pytest: 397 passed, 14 skipped, 34 existing deprecation warnings,
  108.51 s. Two final incompatible-configuration cases added afterward passed in
  the final focused run; they do not change runtime code.
- Final demo/diagnostics/upgrade focused tests: 48 passed, 2 config-gated skipped,
  1.87 s. Covers repeat/interrupt/metadata recovery, input/image tamper refusal,
  OS concurrency lock, missing checkout, remote DB/TLS refusal, mode ACK semantics
  and all prior provisioning/private-input/Windows diagnostics tests.
- Explicit amd64/arm64 Compose configuration tests: 2 passed, 1.03 s. This is
  configuration validation, not ARM64 runtime/hardware acceptance.
- Ruff app/deploy helper/simulator/tests/Alembic passed; git diff --check passed.
- Actual Windows 11/Python 3.12/Docker Desktop Linux/Chrome compiled-demo acceptance
  passed in kzhome-demo-check-v02068d750c95b. Fresh prepared data was seeded, an
  extra room added, and the empty ledger downgraded ONLY in this new fixture to
  emulate legacy schema 0005. Existing business rows were not reset. Upgrade then
  ran actual migration to 0006, deliberately lost its response after commit,
  resumed, and repeated with no further recreation/migration.
- Compared users/password hashes, houses, rooms, devices/state and scenes before
  and after upgrade; compared SHA-256 of user/simulator credentials, CA/server keys,
  certs, ACL/password and proxy files. All unchanged. Metadata project/ports
  preserved; additional room proves bootstrap was not rerun during upgrade.
- Actual browser against compiled nginx Web, no interception: login, permission
  response, ON, OFF, unchanged OFF, failed ACK, real timeout, offline disabled
  buttons, duplicate-click blocking, anonymous POST 401 and logout all passed.
  Independent SQL: applied=3, failed=1, timed_out=1; zero applied rows missing
  matching ACK/state fields or desired/reported boolean. Full service restart
  preserved business state. HTTP/Web/Core/MQTT TLS/software Relay were real.
- Earlier check d2b5d252b4 also passed end-to-end before final guard refinements.
  First check 8e50e36490 stopped at an overly strict anonymous broker-log mount
  guard, before upgrade mutations. That guard was corrected for the exact implicit
  Docker log volume; no TLS/credential/ownership bypass was introduced.

All pre-existing container identities/configuration/start times/restart counts,
volumes, networks and image entries were preserved by subset/inventory assertions.
Owned containers/networks were removed after acceptance, with NO --volumes flag.
Six new named test volumes (and image-declared anonymous log volumes), private
inputs, backups and test images are intentionally retained for these three owned
check projects. They are not unrelated data and were not deleted. Package/base
image cache additions, including node:22-bookworm-slim, remain. No installed demo,
kzhome-local, production volume, production secret or ACL was changed.

## Limits, files and readiness

This establishes local software acceptance, not an upgrade of the mentor's actual
machine or physical ESP32/GPIO. The actual mentor snapshot/image provenance and
its host-specific drive sharing/ACL/certificate expiry/disk capacity remain to be
checked there with the non-destructive prerequisite command. Real local NTFS ACL,
bind mounts, custom dump IO, metadata replacement and browser control were tested
on this Windows host; different drive letters/spaces are covered by diagnostics
unit tests, not a second real mentor-host deployment. Backups are valid dump/list
checked but a full disaster restore was not executed. No atomicity or universal
rollback is claimed. Live broker disconnect/LWT failure timing and software relay
state durability across restart are not certified; graceful offline was tested.

Modified: deploy/demo.py, deploy/MENTOR_DEMO_RU.md, README.md.
Added: deploy/demo_upgrade.py, app/demo_relay.py, tests/test_demo_upgrade.py,
tests/demo_upgrade_acceptance.py, tests/demo_control_browser.mjs,
docs/DEMO_UPGRADE_V0.20.md.

Core started clean at 5f7db60647a29e1ba36c7613b990a06b25f0b462 on
feature/demo-upgrade-v0.20. Web main at 809266f5c0e44f2a2b591c494c62b692429fbce0
started and remains clean/read-only. No branches switched, commits, pushes, merges,
PRs or installed deployment changes performed. The implementation is ready for
commit/PR review within this scope. The mentor procedure is locally accepted;
send it as an executable upgrade ONLY after the reviewed Core release commit is
published/provided, together with the tested Web ref. Do not ask the mentor to
apply this unpublished working tree or guess release versions.
