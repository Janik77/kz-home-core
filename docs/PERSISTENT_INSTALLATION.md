# One private software installation (manual lifecycle candidate)

This adds an **opt-in, unqualified user-systemd controller** around the existing
`durable_software.py` profile. It does not replace profile identity validation,
change migration compatibility, install systemd, enable boot startup/linger,
restart Docker/WSL or connect physical devices. Do not install into production.
Cloud unit tests are not an end-to-end persistence test.

## Version boundary

The implementation base is Core #47 `898a337b725cd5aad6a4c56f194898ff8c574493`
and Web #26 `c55dba53cb093c55d006dcd76f54b823a6f6e2be`, schema
`0012_server_scene_execution`. The launcher's internal runtime-content pin is
intentional and is not the checkout SHA. Do not edit it to make another checkout
pass. Use the final reviewed integration SHA, not an arbitrary branch tip.

Automation work will add `0013_device_automations`. This manager alone **does not
support that upgrade yet**. Integration must pin both final trees, update durable
schema/runtime checks and acceptance tests, and test backup/restore on the exact
pair before calling the automation installation ready. There is no new worker:
the shared scene executor is started by Core. Interrupted runs must never be
resumed automatically; lease expiry makes an uncertain outcome unknown.

## Stable paths and prerequisites

Use an ordinary Ubuntu/WSL user, existing Docker Desktop integration, Python
3.12 with venv, Node 24/npm, and an already-working `systemctl --user`. Check:

```bash
systemctl --user show-environment >/dev/null
docker context show
node --version
python3.12 --version
```

If user systemd is unavailable, stop here. No automatic WSL restart or system
configuration is authorized. The existing foreground durable launcher is a
fallback (Ctrl-C to stop), **not** an unattended installation. An explicit
maintenance window is required for changes needing a WSL restart.

Keep clean Core/Web checkouts and `.venv` at fixed Linux-home paths, for example
`$HOME/kzhome-install/{core,web,.venv}`. Keep state separately at
`$HOME/.local/share/kzhome-software/daily-new`. The repositories may be updated
only through a tested, backed-up version transition. Do not rebuild a new lab
per ordinary start, and do not copy an initialized profile to another directory.
These Linux paths are not `C:\projects\...`; inspect them from Windows via
`\\wsl.localhost\<actual-distribution>\home\<actual-user>\kzhome-install`.
Use `wsl -l -q` to find the real distribution name; do not guess a binding.

Create a **new** profile once with the existing `durable_software.py init`
command. Never point init at the old acknowledgement profile. Existing profile
identity errors remain fail-closed; recovery is a separate, deferred task.
Install requirements and `npm ci` using the exact reviewed versions, as in
`SERVER_SCENE_EXECUTION.md` / the reviewed integration PR.

## Install once, then use the same commands

From the stable installation root, with an already initialized NEW profile:

```bash
export KZ_STATE="$HOME/.local/share/kzhome-software/daily-new"
.venv/bin/python core/tools/manage_software.py install --state "$KZ_STATE" --web-root "$PWD/web"
.venv/bin/python core/tools/manage_software.py start --state "$KZ_STATE" --web-root "$PWD/web"
.venv/bin/python core/tools/manage_software.py status --state "$KZ_STATE" --web-root "$PWD/web"
.venv/bin/python core/tools/manage_software.py stop --state "$KZ_STATE" --web-root "$PWD/web"
.venv/bin/python core/tools/manage_software.py restart --state "$KZ_STATE" --web-root "$PWD/web"
```

Install writes one private user unit; it neither enables nor starts it. Commands
refuse a modified unit, drop-ins, a different loaded unit, or an invalid profile.
The unit uses SIGINT for the foreground launcher and an owned-resource stop hook
on exit. No automatic restart is configured, deliberately avoiding blind command
replay. PostgreSQL volume/profile are retained. Startup still runs all existing
identity, runtime, database and software-only inventory checks.

`status` reports process lifecycle, **not readiness or physical state**. Find the
loopback URL in the unit's journal (`journalctl --user -u <printed-unit> -n 40`)
after startup. Profile `session.json` can be stale after a failed start; do not
interpret its existence as health. Confirm login, MQTT connectivity and a fresh
software state before sending commands. Login remains owner@example.test;
password stays in private profile.json. Do not paste passwords/journal secrets
into issues or commit state, certificates, database dumps, profiles or backups.

## Backup, restore, update and rollback

Stop the unit first and require a successful stop before backup. Use the same
version's existing verified-manifest tool:

```bash
.venv/bin/python core/tools/manage_software.py stop --state "$KZ_STATE" --web-root "$PWD/web"
.venv/bin/python core/tools/durable_software.py backup --state "$KZ_STATE" --destination "$HOME/.local/share/kzhome-software/backups/daily-001"
.venv/bin/python core/tools/durable_software.py restore --backup "$HOME/.local/share/kzhome-software/backups/daily-001" --state "$HOME/.local/share/kzhome-software/daily-restored-001"
```

Create the backup parent beforehand. Destinations must not exist. Ordinary
restore is exact-version only and allocates new identity/volume; install a
separate unit against the restored profile and verify it while source is stopped.
Never edit identity, pins, migration markers or Docker labels. Do not delete the
source or overwrite either history. Read `SOFTWARE_UPGRADE.md` for the narrowly
supported older source; it is **not** permission to upgrade arbitrary profiles.
For rollback, stop the new unit and restart the untouched source using its own
exact checkout/unit. Changes made only in the new profile stay there.

## Required integration acceptance before release

On an isolated machine with real PostgreSQL/Mosquitto TLS and user systemd:

1. Create one new profile and install/start once. Observe live software relay.
2. Create room, scene, automation, favorite and durable execution history.
3. Stop/start twice, then restart. Verify IDs, values and row counts unchanged;
   verify no commands emitted merely because of restart.
4. Kill Core during dispatch; restart must not replay an unknown command.
5. Back up stopped source, restore into a new profile, verify all records and
   execute a newly requested software command; source identity is unchanged.
6. Verify modified/drop-in units, clock faults, missing user bus, occupied port,
   missing volume and mismatched identity fail safely without reinitialization.
7. Repeat with final Core/Web automation SHAs and schema0013 before release.

Known local blocker: previously observed WSL wall/monotonic drift. Do not weaken
Core's clock guard or reset system time. This cloud has no Docker and no usable
user bus, so no real service lifecycle, data-preservation or local-PC readiness
claim is made by these controller unit tests.
