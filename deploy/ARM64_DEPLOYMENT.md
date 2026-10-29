# Linux ARM64 deployment readiness — v0.13

This is an opt-in deployment plan and overlay, not a hardware certification.
It leaves `Dockerfile`, `requirements.txt`, `compose.local.yaml` and
`compose.production.yaml` unchanged. Do not combine the local development stack
with production: it deliberately has demo passwords and anonymous MQTT.

## Host selection: requirements, not a board assumption

Choose the board, storage, power supply and supported OS together. No particular
Orange Pi model, GPIO, radio, boot firmware or vendor OS is assumed.

| Item | Planning floor / decision |
| --- | --- |
| CPU / userland | ARMv8-A or newer capable of `aarch64`, **64-bit** Linux and userspace; plan at least two cores. ARMv7/32-bit images are outside this overlay. |
| RAM | Plan at least 2 GiB for a small home with prebuilt images; 4 GiB preferred, more for native builds. These are engineering starting budgets, not measured minimums or a capacity guarantee. |
| Storage | Plan at least 32 GB usable persistent storage with space reserved for WAL, images, logs and upgrades; size from event retention and backup requirements. Prefer power-loss-tolerant SSD/NVMe or reliable eMMC. Removable flash is a boot/recovery alternative, not the preferred database medium. |
| OS / kernel | Maintained board-supported 64-bit Debian or Ubuntu Server; Docker's currently supported distribution release and arm64 packages. Board-specific boot, Ethernet and storage drivers must actually work. Prefer cgroup v2, overlayfs and normal seccomp support. |
| Network | Reliable Ethernet preferred; Wi-Fi is an operator choice requiring reconnect tests. Reserve an IP or DNS name and choose device/client VLANs and firewall rules. Internet is needed for pulls/updates or use an offline image/artifact transfer. |
| Power / cooling | Correctly rated supply, sustained cooling and preferably a UPS with graceful shutdown. Check that the chosen board can boot automatically after power restoration. |
| Time | Working NTP/time synchronization and RTC or boot-time network synchronization: JWT expiry and TLS depend on correct time. |

Verify `uname -m` reports `aarch64`, `getconf LONG_BIT` reports 64,
`docker info` reports the expected architecture/storage/cgroup driver, and
`docker buildx inspect --bootstrap` includes `linux/arm64`. Enable Docker at boot.
Do not infer working ARM64 from an ARM64 CPU running a 32-bit OS.
Docker documents [supported distributions and architectures](https://docs.docker.com/engine/install/)
and [Debian prerequisites](https://docs.docker.com/engine/install/debian/).
If Docker data resides on another disk, make Docker depend on that mount
(`RequiresMountsFor=` for the actual data-root); a missing disk must not silently
create an empty installation on the boot filesystem. Test this on the selected OS.

## Compatibility evidence (2026-10-29)

Registry manifests were inspected using `docker buildx imagetools inspect`:

| Component | Existing/new image tag | Advertised platform |
| --- | --- | --- |
| Core base | `python:3.12-slim-bookworm` | `linux/arm64/v8` |
| Database | `postgres:17-bookworm` | `linux/arm64/v8` |
| Broker | `eclipse-mosquitto:2` | `linux/arm64/v8` |
| HTTPS/static Web | `nginx:1.28-alpine` | `linux/arm64/v8` |

The **unchanged** Core Dockerfile built with
`docker buildx build --platform linux/arm64 --tag kz-home-core:v0.13-arm64-check --load .`
on Docker Desktop's amd64 Linux engine with emulation. Pip resolved all current
requirements, including ARM64 wheels for Pydantic Core, psycopg-binary,
SQLAlchemy/greenlet, Argon2 bindings/cffi, uvloop, httptools, PyYAML, watchfiles and
websockets. No compiler or dependency changes were needed. An isolated, read-only,
network-disabled ARM64 container reported `aarch64`, imported those native modules
and `app.main`, completed an Argon2 hash/verify roundtrip, and passed `pip check`.
No database connection or migration was used in that smoke test.

This establishes a compatible build/import path for the **resolved dependency
set**, not native performance or all ARM board kernels. Direct dependencies are
pinned; transitive requirements and base-image tags are not fully locked. Record
the resolved package list, source revisions and image digests for each release;
rebuild/scan before promotion. Pin reviewed multi-platform digests in a separate
installation override if reproducibility is required; do not reuse old amd64-only
image tags on ARM64. The Core image keeps a distinct tag in this overlay.

Official image sources: [Python](https://github.com/docker-library/python),
[PostgreSQL](https://github.com/docker-library/postgres),
[Mosquitto](https://github.com/eclipse-mosquitto/mosquitto/tree/master/docker),
[NGINX](https://github.com/nginx/docker-nginx).

## Production topology and preserved contracts

```text
Browser -- HTTPS :443 --> NGINX (frontend network)
                           |-- / and SPA routes --> immutable Web dist files
                           `-- /core-api/* ------> Core :8000 (prefix stripped)
                                                     |-- PostgreSQL :5432 (backend)
                                                     `-- MQTT TLS :8883 (backend)
Devices -- optional private LAN TLS :8883 ----------> Mosquitto + exact device ACL
```

`compose.arm64.yaml` is an overlay on production, with default project name
`kzhome-arm64`. It sets all four services to `linux/arm64`; a fresh project creates
`kzhome-arm64_postgres_data` and `kzhome-arm64_mosquitto_data`, never the existing
`kzhome_*` or `kzhome-local_*` volumes. It extends database/broker shutdown grace.
It does not import, rename, migrate or copy existing installation data.

PostgreSQL and Mosquitto remain on the internal backend without published ports.
NGINX joins only frontend. Core retains the existing loopback-only `127.0.0.1:8000`
diagnostic port; port 8000 is not a LAN API. Its Docker healthcheck is liveness,
not database/schema/MQTT readiness. The proxy's dependency is intentionally no
stronger than this: check `/ready` and MQTT subscription logs before acceptance.

The API, RBAC, single-worker in-process EventBus, Argon2 passwords, JWT lifetimes,
refresh rotation/revocation and tab-memory Web sessions are unchanged. Preserve
the JWT signing key across normal upgrades; deliberate key rotation logs users
out. Never add query-token WebSockets or cookies to bypass the existing contract.
The browser app remains read-only, including device and scene controls. Core's
authenticated operator APIs remain available; deployment does not redefine RBAC.

The proxy strips exactly `/core-api/`, forwards Authorization, avoids API caching
and disables upstream retries so a rotating refresh POST is not replayed. Login,
refresh, logout and me share a per-source-IP auth rate limit (10/minute, burst 20,
429 on excess). Tune for household size/shared NAT after measuring; localhost
administrators can bypass proxy throttling via the diagnostic port. This is not
a distributed lockout system. No client-IP trust is configured for another proxy;
review that separately before putting a load balancer in front.

Access logs omit URL, query string, headers and body. Do not enable body/header
debug logging. Error logs and Core logs still need protected access/rotation and
must not be treated as a safe place for secrets. Logs use Docker's existing bounded
rotation. There is no browser-compatible WebSocket endpoint in this deployment;
`/core-api/ws` is closed. Swagger under a stripped prefix is not configured with
an OpenAPI root_path; use SSH-tunneled loopback diagnostics for operator Swagger.

## HTTPS, MQTT and network choices

Only `HTTPS_BIND_IP:443` is published by default. Set a real private host address
or `127.0.0.1` for a trusted tunnel; no wildcard is selected for you. Enforce client
subnet/VPN access through Docker-aware firewall rules. Docker-published ports can
bypass a simple host UFW policy ([Docker firewall guidance](https://docs.docker.com/engine/network/packet-filtering-firewalls/)):
verify from another machine. Internet exposure,
router forwarding and another upstream proxy are separate operator decisions.

Supply `deploy/local/https/fullchain.pem` and `privkey.pem` for the HTTPS identity:

- Public domain with public CA: choose DNS-01 issuance for a private deployment,
  or separately design HTTP-01 reachability. This overlay publishes no port 80
  and contains no ACME client or automatic renewal service.
- Private LAN DNS/IP: use a managed private CA and install its trust certificate
  on every browser device. The leaf certificate needs the actual DNS/IP SAN.
- A temporary self-signed certificate is acceptable only for isolated validation
  with explicit client trust; do not disable verification or ask users to ignore
  browser certificate warnings as a production procedure.

NGINX serves TLS 1.2/1.3; certificate validity/key matching and renewal remain
operator-owned. Keep an expiry alert and renewal schedule. Stage the complete
new chain/key with restrictive permissions in the mounted directory, run
`dc exec proxy nginx -t`, then `dc exec proxy nginx -s reload`. Verify with a
trusted client and retain the previous pair for recovery. Do not mount CA private
keys, credential directories or the Docker socket into the proxy. HSTS/preload
is intentionally not imposed before the domain/trust policy has been decided.

MQTT uses the **unchanged** production `mosquitto.conf`, bootstrap permissions
script and password/ACL files. Anonymous access stays disabled; no port 1883 is
published. Broker credentials are separate from human accounts. The broker leaf
certificate must contain DNS SAN `mosquitto` for Core's verified internal TLS.
For real LAN devices, also add the chosen LAN DNS/IP SAN and use
`compose.mqtt-lan.yaml` with explicit `MQTT_LAN_BIND_IP`, device-subnet firewall
rules, individual device accounts and exact topic ACLs. Follow
[DEVICE_COMMISSIONING.md](../DEVICE_COMMISSIONING.md); do not use shared simulator
credentials or the local anonymous broker. Preserve the private CA offline;
retain only the public CA, leaf chain/key and password hashes on the host.

## New-host preparation and explicit startup (Linux shell)

These commands are a **new installation procedure**, not commands executed on
the current development stack. They do not migrate old volumes. Choose/record
all source revisions, host paths, project name, IPs, DNS and trust policy first.

1. Install supported Docker Engine, Buildx and Compose v2+ (the overlay uses
   ordinary Compose features). Check disk health, free space, cooling and time.
2. Copy `deploy/arm64.env.example` to ignored `deploy/local/arm64.env`, restrict
   it to the operator (`0600`), and supply distinct DB admin/application/broker
   secrets and a strong JWT signing key. URL-encode the application DB password.
   Environment values remain visible to Docker administrators. Never print
   expanded Compose configuration or pass real secrets in command arguments.
3. Prepare MQTT TLS/passwords and an installation-specific ACL using the existing
   README/commissioning workflow. `MQTT_ACL_FILE` deliberately requires a local
   ACL copy rather than silently activating sample simulator identities.
4. Obtain a reviewed **Core-mode Web build artifact** from the separate Web
   build pipeline: Node >=22.12, locked npm dependencies, `VITE_DATA_MODE=core`,
   `VITE_API_URL=/core-api`. No credentials, signing keys or broker/DB settings
   belong in `VITE_*`. `CORE_DEV_PROXY_TARGET` is irrelevant to a static build.
   Extract the entire `dist` artifact into `deploy/local/web` (including assets).
   Record its revision/checksum. No Web source or build is modified here.
5. Install HTTPS files as above. Keep only that leaf pair in `deploy/local/https`.
   NGINX master needs to read the key (root-owned `0600` on Linux is suitable);
   workers need readable/traversable Web assets (`0644`/`0755`). Core UID 10001
   must read the public MQTT CA. Private credentials stay protected.

From the Core repository root on the chosen Linux host:

```sh
dc() { docker compose -p kzhome-arm64 --env-file deploy/local/arm64.env \
  -f compose.production.yaml -f compose.arm64.yaml "$@"; }
test -s deploy/local/web/index.html
test -s deploy/local/https/fullchain.pem
test -s deploy/local/https/privkey.pem
dc config --quiet
dc build core                         # native ARM64; alternatively load a reviewed ARM64 release image
dc pull postgres mosquitto proxy
dc up -d postgres mosquitto
dc ps
dc logs --tail 50 mosquitto
```

On **only the new empty installation**, provision the DB application role as in
the production README (`dc exec postgres psql -U postgres -d kzhome`):

```text
CREATE ROLE kzhome LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE;
\password kzhome
ALTER DATABASE kzhome OWNER TO kzhome;
\q
```

The role owns this database and runs migrations; this preserves the existing
privilege model. Changing POSTGRES_PASSWORD in a file does not rotate a password
in an existing cluster. Provision initial human users using the existing
operator-side `UserService` mechanism; there is no registration endpoint and
production demo seeding is forbidden. Do not invent default user credentials.

```sh
dc run --rm --no-deps core python -m alembic upgrade head
dc up -d core
curl --fail http://127.0.0.1:8000/health
curl --fail http://127.0.0.1:8000/ready
dc logs --tail 50 core                 # require MQTT connected/subscriptions restored separately
dc up -d proxy
dc exec proxy nginx -t
# Use the chosen hostname; supply --cacert /path/to/public-ca.crt for private CA trust.
curl --fail https://YOUR_CHOSEN_HOST/core-api/health
curl --fail https://YOUR_CHOSEN_HOST/core-api/ready
```

Do not use `--insecure`. Test browser login/me/refresh/logout, house reads and
read-only controls through the same HTTPS origin, with a disposable operator
account, then remove only that fixture. Check SPA deep links and missing assets,
certificate trust from each client, and no network reachability of PostgreSQL or
plaintext MQTT. These hardware/deployed-Web acceptance checks remain to be done.
To stop while preserving everything: `dc stop`. Restart dependencies first,
wait for PostgreSQL health, start Core and require `/ready` plus MQTT logs, then
start proxy. Restart policies recover processes, not unhealthy application logic;
healthchecks alone do not restart unhealthy containers.

## Persistent state and coordinated backups

| State | Location | Recovery rule |
| --- | --- | --- |
| DB: users, houses, refresh sessions, audit, physical identities | `kzhome-arm64_postgres_data`, `/var/lib/postgresql/data` | Logical PostgreSQL dump plus tested restore; keep PostgreSQL 17 major during initial recovery. |
| Retained broker/session state | `kzhome-arm64_mosquitto_data`, `/mosquitto/data` | Copy after graceful broker stop, preserve ownership; not the authority for house membership/device lifecycle. |
| Secrets/config | ignored `deploy/local/arm64.env`, MQTT TLS/hash/ACL files and HTTPS leaf pair | Encrypted off-host backup; CA signing keys remain offline. |
| Static Web and images | reviewed Web artifact, Core/component image digests and source revisions | Reproducible release bundle; keep previous release for recovery. |

Named volumes are not backups. Pick a retention/RPO policy (for example daily
and before every update), monitor backup completion/space and keep an encrypted
off-host copy. A daily dump can lose up to a day's changes. If that is unacceptable,
design/test PostgreSQL base backups plus WAL archiving/PITR separately. Do not
tar/copy a running PostgreSQL data directory as a backup.

Example **Linux-shell** maintenance backup; plan the outage and use the `dc`
helper above. Ingress, Core and broker are stopped so DB and retained broker state
do not diverge during the copy; PostgreSQL stays up for a consistent logical dump:

```sh
umask 077
backup="$HOME/kzhome-backups/$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$backup"
dc stop proxy core mosquitto
dc exec -T postgres pg_dump -U postgres -d kzhome -Fc > "$backup/kzhome.dump"
dc exec -T postgres pg_restore --list < "$backup/kzhome.dump" > "$backup/contents.txt"
dc cp mosquitto:/mosquitto/data "$backup/mosquitto-data"
cp -a deploy/local/arm64.env deploy/local/mqtt deploy/local/https "$backup/"
sha256sum "$backup/kzhome.dump" > "$backup/kzhome.dump.sha256"
# Encrypt/transfer the backup off-host; record image digests and Web artifact checksum.
dc start mosquitto
dc start core
curl --fail http://127.0.0.1:8000/ready
dc start proxy
```

Check every command's exit status before continuing; failed dumps must not replace
the last known-good backup. These examples are not a backup automation service.
Do not perform binary dump redirection using old Windows PowerShell text handling;
run the procedure on Linux. Do not put these backups inside an image build context.

## Restoration, upgrades and power-loss recovery

Rehearse restore on a **different host or explicitly new Compose project** with
fresh volumes, offline from real devices. Use another host to avoid loopback/HTTPS
port collisions; for a same-host drill use a reviewed override with alternate
ports. Never restore into the live installation to test a backup.

1. Verify backup checksums and decrypt into an operator-only directory. Load the
   exact PostgreSQL 17/broker/Core images and Web artifact from the backup release.
   Prepare bind files and select a new `-p` project name. Inspect `config --quiet`
   and effective volume names **with dummy values only** before first startup.
2. Start only the new postgres service. Create the non-superuser role/password
   and the empty owned database as above. Restore through the local container:
   `dc exec -T postgres pg_restore -U postgres -d kzhome --role=kzhome --no-owner --no-acl --exit-on-error < /safe/path/kzhome.dump`.
   Do not use `--clean` against a working database. Role passwords are recovered
   separately; do not assume an application dump creates cluster roles.
3. Create the new broker container without starting it (`dc create mosquitto`),
   then `dc cp /safe/path/mosquitto-data/. mosquitto:/mosquitto/data/` before its
   first start. Ensure copied data belongs to the broker UID/GID for this exact
   image (inspect `id mosquitto` in a disposable image container); the bootstrap
   fixes the directory owner, not recursively every restored file. Use a scoped
   one-shot helper mounted only to the new broker volume if ownership needs repair.
4. Check Alembic revision with the matching Core release; do not blindly migrate
   with a newer image during disaster recovery. Start broker/Core/proxy, verify
   `/ready`, TLS, auth and read data. Restored refresh sessions and signing keys
   retain their original semantics; deliberate session invalidation/key rotation
   is a separate operator decision. Prevent duplicate Core MQTT client IDs when
   the original installation could still be online.
5. Keep device LAN access blocked until ACLs, physical lifecycle and retained
   state are reconciled. Restored state is not proof that a device command ran.

For planned updates: record release/digests and backup first; stage new images,
Web assets and certificate files; stop proxy/Core before explicit migrations;
keep PostgreSQL major 17; start and validate Core then switch Web/proxy. Do not
update PostgreSQL major with a simple tag change: use a tested logical restore or
`pg_upgrade` plan. After a schema migration, rolling back only the image may be
invalid: restore the matching backup into a fresh project and validate before
cutover. No automatic `alembic downgrade`, volume reset or prune is prescribed.

On sudden power loss: PostgreSQL normally replays WAL when its volume/storage
remain intact. Keep `fsync`, `full_page_writes` and synchronous commits enabled;
do not trade durability for benchmark numbers. Mosquitto persistence is not a
transactionally synchronized PostgreSQL journal; changes since its last save can
be lost in an unclean shutdown. Restart policies, UPS/graceful shutdown, disk
health checks and off-host backups address different failures. After boot verify
the correct disk mounted, time synchronized, DB healthy, `/ready`, MQTT reconnect
and TLS, then inspect physical device states before enabling use. If corruption
is suspected, stop writes, preserve evidence and recover into fresh storage;
never delete WAL or reset a volume as a repair step.

References: [pg_dump](https://www.postgresql.org/docs/17/app-pgdump.html),
[filesystem backup constraints](https://www.postgresql.org/docs/17/backup-file.html),
[WAL reliability](https://www.postgresql.org/docs/17/wal-reliability.html),
[PITR](https://www.postgresql.org/docs/17/continuous-archiving.html),
[NGINX upstream DNS](https://nginx.org/en/docs/http/ngx_http_upstream_module.html),
[request limiting](https://nginx.org/en/docs/http/ngx_http_limit_req_module.html).

## Validation boundary

Validation performed on the amd64 Docker Desktop Linux engine with ARM emulation:

| Check | Result |
| --- | --- |
| Four image registry manifests | All advertise `linux/arm64/v8` |
| Unchanged Core ARM64 Docker build | Passed; retained as `kz-home-core:v0.13-arm64-check` |
| ARM64 native extension imports, Core app construction, Argon2 roundtrip | Passed in a read-only, network-disabled disposable container |
| ARM64 `pip check` | No broken requirements |
| New opt-in merged Compose tests | 3 passed: preserved security/mounts, required HTTPS bind address, optional TLS-only MQTT LAN |
| Full pytest with `RUN_ARM64_CONFIG_TESTS=1` | 314 passed, 7 skipped; existing opt-in/external acceptance gates remain skipped |
| Ruff on new test, `git diff --check`, local Compose config validation | Passed |
| ARM64 NGINX `nginx -t` | Passed using disposable self-signed fixture material with explicit client CA trust |
| Temporary HTTPS proxy to the existing local Core, with synthetic SPA only | SPA root/deep link 200; `/core-api/health` and `/core-api/ready` 200; unauthenticated me 401; unknown API/missing asset 404 |
| Auth burst limiting | 21 unauthenticated GETs returned 401, next 5 returned 429 |
| Existing stack / volumes / Web repository | Stack remains healthy; no existing containers/images/volumes removed or reset; Web remains clean on main |

Proxy checks used a newly owned temporary container on the local network with
loopback-only random HTTPS publication, then stopped/auto-removed **only that new
test container**. No existing service was stopped or recreated and no database
fixture writes were needed. This is not a full production Web/login acceptance:
the static page was synthetic, and Core behind it was the existing amd64 local
stack. NGINX logged `io_setup() ... Function not implemented` under emulation
despite passing requests; native kernel I/O behavior remains unverified. Generated
test certificate/key files are confined to ignored `deploy/local/arm64-validation`.
The check image/build cache and newly pulled NGINX image are retained; no prune
was run. No release was deployed, committed, pushed or submitted as a PR.

Repeat the configuration checks from Core root with the configured SDK:

```sh
RUN_ARM64_CONFIG_TESTS=1 /path/to/configured/python -m pytest tests/test_arm64_deployment.py -q
```

Native board boot, thermal/load capacity, storage/power-loss durability, deployed
Web browser acceptance, public/private CA renewal and backup/restore rehearsal on
ARM64 remain hardware/operator acceptance gates. Do not call this a tested Orange
Pi release. The board/OS image, durable storage/power, IP/DNS, HTTPS issuer and
renewal, firewall subnets and backup RPO remain explicit installation decisions.
