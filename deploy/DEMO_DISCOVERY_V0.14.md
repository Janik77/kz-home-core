# v0.14 — discovery before implementation

Inspected on 2026-10-29, before demo implementation. Core is clean on
`feature/demo-deployment-v0.14`; Web is clean on `main` and is read-only.
Remotes are `https://github.com/Janik77/kz-home-core.git` and
`https://github.com/Janik77/kz-home-web.git`.

Existing evidence: PROJECT_STATUS, ARCHITECTURE, README, DEVICE_PROTOCOL,
local/production/ARM64 Compose, both deployment guides, authentication/RBAC,
entity schemas/routes/services, commissioning ACL preparation, Web v0.4 Core
integration and live acceptance documentation, and simulator sources/tests.

| Already available | Missing for independent mentor demo |
| --- | --- |
| Non-root Core Docker image, PostgreSQL 17 persistence, explicit Alembic migration, health/readiness | One repeatable isolated demo command and generated local configuration |
| Production MQTT TLS, private bootstrap copies, password authentication and exact simulator ACLs | Local throwaway certificate/password preparation without printed secrets |
| Atomic `app.bootstrap_e2e.bootstrap`, actual UserService/Argon2, owner membership, dedicated E2E graph and conflict rejection | Explicit demo-only guard, simulated labels, second room, supported scenes and sample snapshot sensor |
| Actual CRUD/auth/read APIs, declared automation engine | Discoverable practical operator walkthrough, with Web read-only boundary |
| Standalone v1 Relay, motion probe, real MQTT command/ACK/state and automation acceptance runners | Safe opt-in invocation using the same isolated installation credentials |
| ARM64 image/dependency emulation evidence and overlay, HTTPS NGINX routing/static fallback | Windows-first localhost demonstration without requiring public DNS or managed CA |
| Web Core mode, memory-only rotating sessions and `/core-api` mapping | Read-only-source Web build inside Docker, without changing Web checkout/env/dependencies |

Decisions: extend production Compose with a small demo overlay, separate project,
images and persistent volumes. Do not reuse local Compose (anonymous MQTT/default
passwords). Preserve APP_ENV=production and its TLS/auth guards. Generate private
inputs below ignored `deploy/local/demo/<project>`. Use the existing ARM64 overlay
on ARM64 and derive the loopback HTTP demo gateway from its NGINX configuration;
LAN/public HTTPS remains the v0.13 operator deployment path. Local HTTP is only
for same-machine demo or SSH tunnel; no public port binding.

Reuse E2E bootstrap and v1 acceptance runners, not a new simulator/protocol or
background production simulator. Software relay ACKs describe software simulation,
never physical execution. No inventory claim/activation or hardware provisioning
is needed. Web stays read-only; refresh reveals database-backed state changes.
The old in-process virtual simulator is intentionally excluded: production
disables it, and it bypasses the MQTT path this demo should demonstrate.

Host orchestration will use Python's standard library plus Docker from PowerShell
or Linux, with no host pip/Node installs. Core dependencies/provisioning and the
Web build run in containers. The helper will be explicitly opt-in, refuse existing
non-demo resource names, preserve generated credentials on repeat setup, never
reset volumes, and provide explicit start/stop/restart/update/verify/simulate.
Fresh disposable-project acceptance and regression results will be recorded after
implementation; no hardware or fresh ARM64 deployment result is assumed.
