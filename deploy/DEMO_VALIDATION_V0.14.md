# Demo deployment v0.14 — actual validation

2026-10-29. Windows PowerShell host, configured Python 3.12.7 SDK, Docker Desktop
Linux/amd64 engine, Compose 5.5.1. This report does not claim physical hardware
or a fresh native ARM64 demo deployment.

## Existing work reused

See [discovery before implementation](DEMO_DISCOVERY_V0.14.md). Production
Compose, ARM64 overlay, Dockerfile, migrations, TLS broker bootstrap/config/ACL,
actual auth/services/schemas, E2E bootstrap, relay/motion and acceptance runners
remain the underlying implementation. Web was read-only at
`341f8b1724a21bb4c9509f33d2f624008e804f86` on main.

No application route/schema/migration/auth bypass, second server architecture,
new MQTT protocol or frontend change was introduced. New app tooling is invoked
explicitly by a container command, never startup. Bootstrap has two resumable
atomic stages: the existing E2E bootstrap, then service-validated demo enrichment.
An enrichment conflict leaves the initial E2E graph recoverable, not a reset.

## Commands and results

The exclusively created project was `kzhome-demo-check-3905f02d`, private inputs
under ignored `deploy/local/demo/<project>`, with random secrets, its own TLS
identity, unique Core/Web image tags and separate data volumes. Only loopback
18180/18181 were published. PostgreSQL and authenticated TLS Mosquitto had no
published ports. The existing local stack and old production volumes were not
used by this fixture.

| Check | Result |
| --- | --- |
| `prepare --run --project ... --web C:/projects/kz-home-web` | Generated restricted private inputs, converted broker passwords to hashes, copied only tracked Web sources; no secrets printed |
| `start --run --project ...` | Fresh Core/Web Docker builds, separate non-superuser DB role, explicit migrations, bootstrap, actual compiled Web and four services started |
| Health/readiness/static SPA routes | Web/deep link, `/core-api/health`, `/core-api/ready` returned 200 |
| `simulate --run` | Existing real HTTP → MQTT v1 Relay → correlated software ACK/state → persisted OFF/ON/OFF evidence passed |
| `simulate --run --scenario automation` | Existing motion → AutomationService → MQTT Relay → correlated ACK/state and database reads passed; rule left disabled |
| `tests/demo_acceptance.py <project>` | Real login/me/refresh/logout, consumed/revoked refresh rejection and all domain read lists passed |
| Repeat bootstrap | Twice, preserving one user/owner and existing reported state; no password reset/duplicate records |
| Full graceful stop/start | Same reported state persisted across restart; migrations and bootstrap repeated safely |
| Independent PostgreSQL reads | One user/house, two rooms, three devices, two scenes; relay state matched HTTP; app role `rolsuper/rolcreatedb/rolcreaterole` all false |
| Existing `commissioning_probe.probe` inside isolated demo network | Verified TLS, anonymous/wrong-password rejection, directional and cross-identity ACL denial passed |
| `tests/demo_browser.mjs <project> C:/projects/kz-home-web` | Real Chrome, actual Docker-built Web, actual Core login/rooms/room deep link/scenes/logout; no request interception; zero device/scene commands from browser |
| Repeated prepare | Existing configuration and credentials retained |
| `update --run --project ...` | Fresh separate Web snapshot, rebuild/start/bootstrap passed; no Git operations or secret rotation; browser acceptance passed again |
| Effective Compose, amd64 and production+ARM64+demo merge | Both passed; production/TLS guards retained, no DB/broker publication, only loopback Web/API, isolated volume names |
| New Node/OpenSSL image manifests | `node:22-bookworm-slim` and `alpine/openssl:latest` advertise Linux ARM64; not a native npm/runtime acceptance claim |
| Full pytest with both config opt-ins | **328 passed, 7 skipped**, five existing deprecation warnings |
| Ruff for all new Python, `git diff --check` | Passed |

Full pytest used `RUN_DEMO_CONFIG_TESTS=1 RUN_ARM64_CONFIG_TESTS=1`. Existing
external/physical/opt-in Docker checks remained skipped; this fixture was validated
separately with the explicit scripts above. No Web unit/lint regression command
was run inside the read-only checkout. The Docker Web build did run its actual
TypeScript/build pipeline, and actual browser acceptance ran twice.

The developer scripts are opt-in tools, not ordinary pytest tests or intercepted
fixtures. `tests/demo_acceptance.py` only accepts `kzhome-demo-check-*`, restarts
that project and sends deliberate invalid-v1 ACL-probe markers using existing
probe logic. Do not aim it at production or a shared mentor presentation in use.
The browser helper requires Node and already-installed Playwright/Chrome from the
read-only reference checkout; those are developer validation prerequisites,
not mentor startup requirements. No screenshots/token traces are written.

## Security and cleanup boundary

Credentials are random per installation, not checked-in defaults. `user.json`,
`simulation.json`, `state.json`, Compose env, CA/leaf material and staged sources
stay below ignored private inputs. Runtime simulation receives only its user and
device credentials via stdin plus a public CA bind; no DB/JWT credentials. The
normal API service receives no demo user password or device passwords.

API/broker passwords are intentionally local plaintext inputs protected by
filesystem permissions, not a vault. The intentional `credentials` display is
terminal-only. Broker hash conversion briefly uses a protected plaintext input
file; failure must not trigger a broad reset. Protect backups and Docker access.
No credentials were committed, printed into this report or placed in VITE.

Only fresh test resources may be removed after inspection of exact project labels
and mounts. No `down -v`, prune, old volume cleanup or image deletion is used.
Cleanup results and final statuses are recorded at completion below.

Cleanup completed for Docker resources: four owned demo service containers and
their networks were removed with `down` **without** a volume flag. After exact
label/mount inspection, only the two newly created named data volumes and that
test broker's newly created anonymous log volume were removed explicitly. No
containers or networks bearing the test project label remain. The protected set
of **17 pre-existing volumes** is unchanged; the four local stack container IDs
are unchanged, all three local services remain healthy and `/health` / `/ready`
still pass. Old production volume metadata is unchanged. Newly built test images
and build cache are retained, not pruned.

Recursive removal of the test-only private input/snapshot directory was rejected
by automatic approval review (`blocked by policy`, no further reason supplied).
It was not bypassed: ignored `deploy/local/demo/kzhome-demo-check-3905f02d` remains
with restricted access. Its demo database/broker no longer exists. This is a
local ignored-artifact cleanup limitation, not a remaining deployed demo service.

Final Git status: Core on `feature/demo-deployment-v0.14`, two modified files
(`README.md`, `PROJECT_STATUS.md`) and ten new files (`app/demo.py`,
`compose.demo.yaml`, this report, discovery, mentor guide, `deploy/demo.py`,
`deploy/demo-web.Dockerfile`, `tests/test_demo.py`, `tests/demo_acceptance.py`,
`tests/demo_browser.mjs`). Nothing staged. Web is clean on main. No commits,
pushes, merges, PRs or automatic branch changes occurred.

## Remaining limits / next milestone

- Owner must publish/deliver a reviewed v0.14 ref before mentor clone commands can
  obtain these uncommitted files. No branch change/commit/push/merge/PR was done.
- Native ARM64 demo startup, board/storage/thermal/power-loss behavior remain
  untested. v0.13 emulation evidence and image manifests are supporting evidence.
- Demo HTTP remains loopback-only (SSH tunnel on ARM64); real LAN/public HTTPS,
  trust/renewal and production backup/restore remain the reviewed v0.13 operator path.
- Simulators are software actors, not firmware or GPIO. Temperature is a static
  snapshot; browser state updates require Refresh home; Web commands remain off.
- Tags/transitive Python dependencies are not completely frozen. Reproducibility
  here means explicit, repeatable setup with recorded Web ref and persistent
  credentials, not bit-identical builds or a tested rollback after future migrations.

Smallest next milestone: owner-delivered immutable demo release refs/images,
followed by a second-person clean-machine walkthrough of this guide, then native
ARM64 acceptance. Do not add physical-device controls to Web as part of that work.
