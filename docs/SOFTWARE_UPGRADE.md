# Isolated software upgrade and rollback

This integration uses Core #43 `5b87f4b71a7a2a99d4dd3d288c7f1ec76ab781f2`,
migration `0011_device_favorites`, and unmodified Web #25
`d6533042b950ded2f8b90dbec1af5a05fd078ce0`. Core independent review is complete;
Web #25 independent review is complete. This draft's exact tested integration SHA/CI links are in its PR.

Only ONE upgrade input is supported:
- backup made with the exact clean Core #42 tool checkout
  `82653a849d2b61479de1c3bb4d74e7f388523667`;
- profile runtime `f946a07f67f9ccb1e8f62c99860a0842bf3a062b`;
- Web #20 `c2c4fa89a0da3652e8e0cca3629300ecfd7e658f`;
- complete owner/path/device/inode/database identity fields and manifest;
- restored source database at exactly `0010_scene_runs`, no physical inventory.

Profiles created by Core #44/Web #23 or Core #45/Web #24 are not accepted by this
checkout. Keep using their exact Core checkout: #44
`733a1b31cdf4eb8a97e732ebf4ed6bf9cc243518`, or #45
`42668c320e2937768c2a210ac5f15fc9c746c1ff`, respectively.
This pin update does not add an upgrade path from either intermediate profile. It preserves the
single #42/Web #20 source contract above.

Earlier draft profiles without identity, #40/#41 Web pins, intermediate #43 runtime
pins, other revisions and arbitrary cross-version backups are unsupported. Ordinary
restore stays exact-version only. Never edit profile JSON, hashes, pins, labels or
bindings to bypass refusal. The dedicated upgrade creates new bindings itself.

## Setup in a fresh development directory

Use an ordinary Ubuntu WSL user with already working Docker Desktop integration,
Python 3.12/venv, Node 24/npm and Git. No sudo, added groups/privileges, daemon changes,
or use of kzhome-local, mentoring demo or orchestrator VM. Keep profiles on the Linux
home filesystem. The user's WSL machine has not been tested.

```bash
git clone --branch codex/integrated-web25 https://github.com/Janik77/kz-home-core.git core-new
git -C core-new rev-parse HEAD
git clone https://github.com/Janik77/kz-home-web.git web-new
git -C web-new checkout --detach d6533042b950ded2f8b90dbec1af5a05fd078ce0
python3.12 -m venv .venv-new
.venv-new/bin/python -m pip install -r core-new/requirements-dev.txt
.venv-new/bin/python -m pip check
npm --prefix web-new ci
```

Pin core-new to the exact tested integration SHA shown in the PR before use.
For a completely new home (no old data), use a NEW path:

```bash
mkdir -p "$HOME/.local/share/kzhome-software"
.venv-new/bin/python -B core-new/tools/durable_software.py init --state "$HOME/.local/share/kzhome-software/favorites-new"
.venv-new/bin/python -B core-new/tools/durable_software.py start --state "$HOME/.local/share/kzhome-software/favorites-new" --web-root web-new
```

The launcher prints a loopback URL. Login owner@example.test; private generated
password is the password field of profile.json. Ctrl-C stops services, retains data.
Do not use init on an existing profile.

## Upgrade an existing supported profile

First Ctrl-C the ORIGINAL launcher and wait for shutdown. Keep its directory at
its original path/inode and keep its database volume. Do not copy/rename it.
Prepare a separate clean old checkout; dependencies stay outside both repositories:

```bash
git clone https://github.com/Janik77/kz-home-core.git core-before
git -C core-before checkout --detach 82653a849d2b61479de1c3bb4d74e7f388523667
git clone https://github.com/Janik77/kz-home-web.git web-before
git -C web-before checkout --detach c2c4fa89a0da3652e8e0cca3629300ecfd7e658f
python3.12 -m venv .venv-old
.venv-old/bin/python -m pip install -r core-before/requirements-dev.txt
npm --prefix web-before ci
```

Set KZ_OLD_STATE to the ORIGINAL absolute profile path; this assignment does not
modify its recorded path. Destination/backup paths below must not already exist:

```bash
KZ_OLD_STATE="/absolute/path/to/your/existing-supported-profile"
mkdir -p "$HOME/.local/share/kzhome-software/backups"
.venv-old/bin/python -B core-before/tools/durable_software.py backup --state "$KZ_OLD_STATE" --destination "$HOME/.local/share/kzhome-software/backups/pre-favorites-001"
.venv-new/bin/python -B core-new/tools/durable_software.py upgrade --source-checkout core-before --backup "$HOME/.local/share/kzhome-software/backups/pre-favorites-001" --state "$HOME/.local/share/kzhome-software/favorites-upgraded"
.venv-new/bin/python -B core-new/tools/durable_software.py start --state "$HOME/.local/share/kzhome-software/favorites-upgraded" --web-root web-new
```

Upgrade reads the trusted local backup, verifies whitelist/checksums, exact pins and
required source identity, allocates a NEW owner/path/network/volume, restores into an
empty database, checks the actual source revision, and migrates only the new database.
All old public tables are compared before/after migration using ordered row fingerprints
and row counts. Users, memberships, house/room/device definitions, scene revisions,
runs, claims, receipts and auth records must be unchanged. Favorites starts empty.
The upgraded target gets its own database identity and Web port; inherited lab login
credentials remain valid. Browser localStorage/theme preferences are not DB backup data.

Initialization markers are written only after migration/data checks succeed. On failure,
only the new target is stopped and left for inspection; it cannot be started, reused
as an existing target or automatically reset. Source profile and backup are not deleted
or rewritten. Never turn a partial target into an initialized one manually.

The old manifest is checksum-based, not signed: the exact source-checkout check does
not cryptographically prove who created a backup. Accept ONLY your own trusted backup
made with the old command above. A database dump can contain executable SQL.
Backups contain lab passwords/hashes/JWT/private CA material; keep them private and
never commit them or upload them as CI artifacts.

## Backup/restore of the new profile

Ctrl-C first. Use new destinations:

```bash
.venv-new/bin/python -B core-new/tools/durable_software.py backup --state "$HOME/.local/share/kzhome-software/favorites-upgraded" --destination "$HOME/.local/share/kzhome-software/backups/post-favorites-001"
.venv-new/bin/python -B core-new/tools/durable_software.py restore --backup "$HOME/.local/share/kzhome-software/backups/post-favorites-001" --state "$HOME/.local/share/kzhome-software/favorites-restored"
.venv-new/bin/python -B core-new/tools/durable_software.py start --state "$HOME/.local/share/kzhome-software/favorites-restored" --web-root web-new
```

Active profiles, missing/foreign storage, wrong database identity/migration, corrupt
manifests, symlinks and occupied targets remain refused. No pruning/delete command exists.

## Rollback without downgrade or data loss in the source

Ctrl-C the NEW launcher, keep its volume/profile and post-upgrade backup, then:

```bash
.venv-old/bin/python -B core-before/tools/durable_software.py start --state "$KZ_OLD_STATE" --web-root web-before
```

This starts the untouched original at 0010. It does not downgrade/overwrite the new
database. Changes made ONLY in the new profile (including favorites and later commands)
are not copied back; preserve both histories. A previously pending scene remains subject
to its original deadline, and rollback never automatically resumes/replays actions.
No safe/OFF conclusion follows from rollback or a stored receipt.

CI uses real PostgreSQL/Mosquitto TLS and the existing Python Relay: old exact-SHA
backup, negative/partial upgrade cases, all legacy row preservation, user/home favorites
isolation, ON/restart/explicit OFF, new backup/restore and old-checkout rollback.
Only labelled profiles under the test's fresh temp root are cleaned; inventory must
match its baseline. No private backup artifacts are uploaded. This is not firmware C,
hardware, multi-day soak, power-loss or user-machine qualification.
