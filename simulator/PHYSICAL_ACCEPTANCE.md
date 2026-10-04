# Commissioned physical identity software acceptance (v0.12)

This opt-in runner exercises **one already commissioned ESP32-C6 relay identity**
using the existing `Relay`, Device Protocol v1 envelopes and production APIs.
It does not require or verify ESP32 hardware. v0.8 runners keep their fixed E2E
identities. No Core runtime, schema, deployment defaults or firmware changes are
introduced. No simulator runs automatically.

## Prerequisites and operator command

Complete [v0.10 onboarding](../DEVICE_ONBOARDING.md) and
[v0.11 commissioning](../DEVICE_COMMISSIONING.md) explicitly first. The installed
Core must contain onboarding with revision `0005_device_onboarding`, `/ready`
passing, MQTT connected and physical lifecycle **active**. This runner will not
register inventory, claim, activate, seed, migrate, issue credentials or configure
the broker. A repository merge or acceptance image build does not update Core.

Use the protected physical-device `RELAY_*` environment contract from commissioning:
actual permanent house/device IDs, unique username `kzdevice-{device_id}`, its
operator-generated password, unique client ID, host `mosquitto`, port 8883 and
public CA at `/run/mqtt/ca.crt`. No Core MQTT password or database credential is
needed by the runner. Never use the shared `relay-simulator` account. Passwords
stay outside Core records and ordinary API responses. Existing ignore rules exclude
local inputs from Git and the image; protect them with host access controls.

Disconnect the physical device and stop only its standalone simulator, if one is
running. No other controller or acceptance process may command this relay. The
runner rejects enabled automations referencing the device and never disables them
itself. Use an existing owner/installer account with device control and event-read
permission. A technician with these permissions can diagnose an already active
device; a resident lacks event-read permission. No account/role is created here.

From the repository root in an interactive PowerShell terminal without
transcription or shell tracing:

```powershell
docker build --pull=false -t kz-home-acceptance:v0.12-local .
if ($LASTEXITCODE -ne 0) { throw 'Acceptance image build failed' }
docker run --rm -it --init --network kzhome_backend --no-healthcheck --env-file deploy/local/device-c6.env --mount "type=bind,source=$($PWD.Path)/deploy/local/mqtt/ca.crt,target=/run/mqtt/ca.crt,readonly" kz-home-acceptance:v0.12-local python -m simulator.physical_acceptance --run
if ($LASTEXITCODE -ne 0) { throw 'Physical identity software acceptance failed' }
```

Replace the env file path with the protected file for the selected device; use
the actual project's backend network if it is named differently. The command
hosts the existing relay simulator and HTTP driver in one separate container.
It never replaces/restarts Compose services or publishes ports. HTTP login uses
`http://core:8000` on the private backend; MQTT always verifies the certificate
chain and the hostname `mosquitto`. Mount only the public CA. LAN DNS/SAN/firewall
validation remains the separate commissioning procedure, not this internal test.

Enter the existing house operator email and password at the prompts. Hidden input
is mandatory; echoed getpass fallback fails. Tokens and passwords stay in memory;
the runner prints fixed phase results, never raw responses or broker exceptions.
`--run` explicitly authorizes real control and state/availability changes. Optional
`--timeout 5..60` sets each phase deadline (default 30 seconds); HTTP/MQTT operations
have five-second timeouts. The negative offline observation window is one second.

## Exact scenario and acceptance evidence

1. Verify readiness, authenticate using `/auth/login`, read the house-scoped
   physical binding and ordinary device, and require active `esp32-c6-relay-v1`,
   v1 and capability `on_off`. Require event-read access and no conflicting rule.
2. Verify unauthenticated ON returns exactly 401 and an unrelated house-scoped
   physical lookup returns exactly 404. HTTP success or a different denial cannot
   pass a negative check. Known foreign-house/foreign-user cases are additionally
   checked by the disposable full-stack test below and existing RBAC tests.
3. Subscribe only to this device's `/set`, publish fresh retained online status
   and OFF state over TLS, and wait for persisted GET OFF/online with exact
   `last_seen`. Then POST `/devices/{id}/on` and `/off` in sequence.
4. For each HTTP command receive the actual non-retained MQTT `/set`, validate
   the desired boolean and unique command/correlation IDs, and apply it through
   the unchanged relay handler. Require fresh persisted `device_ack_received`
   with matching **both IDs** and `applied`, correlated `device_state_changed`,
   and a final ordinary GET with the expected boolean. A negative ACK, stale or
   foreign evidence, missing report or missing MQTT receipt fails acceptance.
5. After OFF, publish the same current OFF state again and feed the exact saved
   ON receipt back into the relay's existing duplicate cache. Its replayed ACK
   traverses MQTT and must be persisted. Relay and persisted device remain OFF
   and no extra state event may appear. **This is a deliberate handler replay of
   an actual received command, not proof of broker retransmission.** The device
   account cannot publish `/set`; the runner obtains no extra broker privilege.
6. Unsubscribe the relay from `/set`, publish fresh offline status, and wait for
   persisted offline. POST ON while it has no command subscription. Current
   behavior is HTTP 200 **dispatch only**, with current state unchanged: Core
   has no pending-command timeout/result API. Resubscribe using the same clean
   session and require no retained/queued command during the one-second window,
   no fresh ACK/state completion event and persisted OFF/offline. This deliberately
   tests unavailable command delivery, not a physical network fault or heartbeat
   expiry. It does not assert that a command can never arrive in another session.
7. Check ordinary device GET/list, physical GET and scoped events for private
   fields or known device credential/access token values. On exit publish offline.
   Success leaves the existing active binding **OFF and offline**. Reruns establish
   a fresh OFF baseline and preserve ownership, credentials and prior event history.

The application path is authenticated FastAPI house RBAC → DeviceService →
MQTTGateway/aiomqtt → real Mosquitto TLS → existing Relay → ACK/state/status →
Gateway identity/admission checks → DeviceService repositories → PostgreSQL and
EventBus/EventLogService persistence → scoped API reads. Ordinary command HTTP
responses still contain observed state and expose no command/correlation IDs;
exclusive control is required to associate a received MQTT command with its HTTP
request. Events are the existing audit/history store, not a new command ledger.

Latest-500-event polling can fail under heavy house traffic. A failure may leave
either state; check the device before resuming other work. Offline cleanup is
attempted on ordinary failure but cannot be guaranteed after process/network loss.
Failure exits nonzero and confers no acceptance. Core currently stores duplicate
ACK rows, suppresses unchanged-state events, and does not order retained/replayed
state by timestamp, track/retry pending commands or expire heartbeats. Simulator
deduplication is bounded/volatile with the existing 300-second expiry; this does
not prove firmware durable execution or full normative Protocol v1 conformance.

## Disposable full-stack regression test

Normal pytest runs migrated SQLite + actual API/RBAC/services/gateway/event
persistence tests, with only MQTT network I/O replaced. It does not contact Docker
or an installed database/broker. This is useful integration coverage, not live
PostgreSQL/TLS evidence.

The separate opt-in test builds an isolated image from the current Dockerfile and
derives its stack from the **effective production Compose configuration**. It uses
random project/image/network/volume names and disposable TLS/password inputs,
never `deploy/local`, `.env`, the installed `kzhome` stack or its volumes. Core
retains production guards, nonroot execution, verified TLS, one worker and the
disabled in-process simulator. PostgreSQL has no host port; fixture-only Core and
broker ports bind random loopback ports for the host test driver. The internal
backend network and real tracked Mosquitto config/startup are preserved.

The fixture explicitly creates a non-superuser DB role, migrates only its new
database, registers trusted inventory via the existing service, then uses human
JWT APIs for house/room creation, claim and activation. It generates the exact
physical ACL with the v0.11 helper and provisions only temporary broker hashes.
No automatic migrations, seed or broker-administration path is added to Core.

It runs the existing real MQTT negative ACL/authentication probe, known foreign
user/house IDOR and list-isolation checks, and resident onboarding denial (403).
It runs the complete physical runner twice, independently reads PostgreSQL for
matching ACK/state rows, permanent binding, consumed claim hash, lifecycle/security
audit rows and final OFF/offline, checks secret absence, then restarts **only its
own PostgreSQL** and recreates **only its own Core** to require the same persisted
state/event IDs. Fixture cleanup removes only its random project resources and
generated test secret files/image. Cleanup failure is reported rather than hidden.

Install the project's dev requirements first. Docker Desktop/Linux containers and
Compose v2+ must be running. Locally available images are required:
`python:3.12-slim-bookworm`, `postgres:17-bookworm`, `eclipse-mosquitto:2`,
`alpine/openssl:latest`. The fixture refuses runtime image pulls; a Core build may
need network access to install pinned requirements if its layer is not cached.
Disposable certificate generation is test setup, not a production CA system.

```powershell
python -m pytest tests/test_physical_acceptance.py -q
$fixtureTemp = Join-Path $PWD.Path ('.venv/physical-e2e-' + [guid]::NewGuid().ToString('N'))
if (Test-Path -LiteralPath $fixtureTemp) { throw 'Fixture temp directory must be new' }
$env:RUN_PHYSICAL_E2E_DOCKER_TESTS = '1'
try {
    python -m pytest tests/test_physical_acceptance_live.py -q --tb=short --basetemp $fixtureTemp
    if ($LASTEXITCODE -ne 0) { throw 'Isolated full-stack acceptance failed' }
} finally { Remove-Item Env:RUN_PHYSICAL_E2E_DOCKER_TESTS }
```

Use the project interpreter. The fresh ignored temp directory avoids Windows
pytest temp-directory ownership conflicts. Do not substitute a production project
or reuse an existing temp directory: pytest owns/cleans `--basetemp`.

## Verification state — 2026-10-04

Offline checks and the real isolated full-stack test have passed; exact results
are in [PROJECT_STATUS.md](../PROJECT_STATUS.md). With Docker Client/Linux Server
29.8.0 running, `tests/test_physical_acceptance_live.py` completed **1 passed in
93.08 seconds**. It verified two complete physical-identity software acceptance
passes, real broker TLS/authentication/negative ACLs, human RBAC/IDOR, independent
PostgreSQL evidence and state/event persistence after database restart and Core
recreation. No application or test code fix was needed.

The previous unavailable-daemon attempt is resolved. A missing local Python base
image on the first resumed attempt was fetched as a test dependency, then removed
after validation. The fixture cleaned its containers, networks, volumes, image
and private inputs; both new workspace test directories were removed. Existing
installation resources retained their original IDs/volumes and were untouched.
These results establish isolated software acceptance, not hardware or deployed
installation/LAN verification.

Next smallest block: run this acceptance against the already commissioned
installation before connecting hardware. Hardware-only acceptance remains
GPIO/electrical output, firmware
timing/clock and durable replay behavior, power cycling, actual LAN DNS/SAN/VLAN
reachability, disconnect/reconnect and secure secret installation. Hardware
availability does not block the software acceptance run.
