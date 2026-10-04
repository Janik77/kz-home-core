# Operator-run v0.8 acceptance (PowerShell)

For the separately commissioned physical relay identity, use
[v0.12 physical software acceptance](PHYSICAL_ACCEPTANCE.md). The procedures below
retain the v0.8 fixed E2E identities and their original live verification scope.

## Verified milestone — 2026-09-30

The operator reports both scenarios below PASSED against the production Compose
stack. Live verification covers broker TLS/authenticated connectivity; Core
reconnect and subscription restoration after Mosquitto restart; HTTP relay ON/OFF
through MQTT -> matching ACK/state -> PostgreSQL persistence; and MQTT motion ->
the existing AutomationService -> MQTT relay -> correlated ACK/state -> persistence.
These are software-simulator checks, not physical ESP32/hardware verification.
The final source audit does not rerun Docker or modify deployed configuration.
Keep the procedures below for explicit reruns; do not reprovision credentials or
restart a working broker merely because this document was updated.

Run from the repository root. This procedure creates real test records in the
configured database. Do not run against an installation containing conflicting
`e2e_*` IDs. Run one bootstrap at a time. No migrations or automatic demo seed run.
Bootstrap reuses compatible records and never resets passwords, roles or observed
state. Acceptance phases deliberately reset dedicated E2E states through MQTT.
Conflicts roll back the bootstrap transaction. Operator Docker/DB
access authorizes initial ownership; ordinary HTTP operations still require RBAC.

## 1. Build and bootstrap without replacing running Core

```powershell
docker build -t kz-home-e2e:v0.8-local .
```

The following one-off container uses Core's existing configuration and a read-only
mount of the new bootstrap module. It does not replace the running Core image or
service; the separately built image above is used for the simulator:

```powershell
docker compose --env-file deploy/local/production.env -f compose.production.yaml run --rm --no-deps --no-build --entrypoint python -v "${PWD}/app/bootstrap_e2e.py:/app/app/bootstrap_e2e.py:ro" core -m app.bootstrap_e2e
```

Answer the email/password prompts (password is hidden; 12–256 characters).
Bootstrap requires a terminal capable of hidden password input and refuses an
echoed fallback. Settings and database URL come from Core's existing Compose
environment; the command connects to PostgreSQL over the private network.
No broker connection is opened. It checks
the schema head, never migrates. No secrets belong on command lines or in Git.

## 2. Login and verify ownership/resources

```powershell
$email = Read-Host 'E2E user email'
$secure = Read-Host 'E2E user password' -AsSecureString
$credential = [pscredential]::new($email, $secure)
$body = @{ email=$email; password=$credential.GetNetworkCredential().Password } | ConvertTo-Json
try {
    $tokens = Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/auth/login -ContentType application/json -Body $body
} finally { $body=$null; $credential=$null; $secure=$null }
$headers = @{ Authorization="Bearer $($tokens.access_token)" }
foreach ($path in @('houses/e2e_house','floors/e2e_floor','rooms/e2e_room','devices/e2e_relay','houses/e2e_house/members')) {
    Invoke-RestMethod -Headers $headers -Uri "http://127.0.0.1:8000/$path"
}
```

Do not print `$tokens` or `$headers`. Use a private terminal without transcription.

## 3. Automated acceptance against the already-running stack

Prerequisites: complete bootstrap above once; the existing dedicated
`relay-simulator` broker account, exact device ACL, ignored `deploy/local/relay.env`
and public CA must already be provisioned as described in [setup](README.md).
No secrets are created or changed by this runner. Use a private interactive terminal
without transcription. No other controller or automation may command this device.
If the standalone simulator is running, stop **only that simulator** explicitly
before running this test (for example `docker stop kzhome-relay-simulator`).
The acceptance runner itself hosts the existing `Relay` simulator implementation;
do not run a second simulator or reuse its client ID concurrently.

From the repository root in PowerShell:

```powershell
docker build -t kz-home-e2e:v0.8-local .
if ($LASTEXITCODE -ne 0) { throw 'Acceptance image build failed' }
docker run --rm -it --init --name kzhome-e2e-acceptance --network kzhome_backend --no-healthcheck --env-file deploy/local/relay.env --mount "type=bind,source=$($PWD.Path)/deploy/local/mqtt/ca.crt,target=/run/mqtt/ca.crt,readonly" kz-home-e2e:v0.8-local python -m simulator.e2e_acceptance --run
if ($LASTEXITCODE -ne 0) { throw 'E2E acceptance failed' }
```

Enter the existing E2E user's email and hidden password when prompted. The runner
logs in directly at `http://core:8000` on the private Compose network; tokens remain
in memory. It uses the dedicated device credentials and verified TLS for MQTT.
It neither loads Core settings nor connects directly to PostgreSQL. Building the
image and starting this one-off container do not replace/restart any Compose service
or publish any host ports. The runner exits nonzero on failure.

The test requires a fresh MQTT online status (matching its `last_seen`) and a
persisted false baseline, then performs **POST /devices/e2e_relay/on** followed by
**POST /devices/e2e_relay/off**. For each command it checks HTTP success, receives
the actual MQTT `/set`, applies it using the standalone relay implementation, and
checks fresh persisted `/events` for an `applied` ACK matching both command ID and
correlation ID and a state change matching correlation ID and expected boolean.
A final GET must report the expected persisted state before the next transition.
The IDs must be distinct across the two commands. The HTTP response exposes neither
ID; association with HTTP relies on exclusive control of this dedicated device.
State messages/events expose correlation ID only. IDs and raw responses are never
printed. No protocol or production tracking changes are made.

Each baseline/transition has a 30-second deadline (override with `--timeout 5..60`),
HTTP/MQTT operations have 5-second timeouts, and event polling uses 200 ms intervals.
Failures identify the phase and, when polling, which ACK/state/GET checks remain
unsatisfied. The runner publishes offline status on exit; a successful run leaves
persisted `on=false`. A failed run may leave either state; rerunning establishes a
new false baseline through MQTT. There is no command retry or database cleanup.

Normal `pytest` does not run this script or need the stack. The script's evidence
matching and orchestration are tested with local fakes; only the command above
proves real TLS/broker/Core/PostgreSQL integration. History is limited to the newest
500 house events, so heavy concurrent house traffic can cause a bounded failure.
This covers a commissioning simulator, not physical hardware, automation, durable
command tracking/deduplication, reconnect delivery, or persistence across DB restart.

## Block 2: MQTT motion triggers the existing automation service

Run after the verified Block 1 setup; do not repeat bootstrap or relay acceptance.
The runner reuses the existing owner, house, room and relay. Through authenticated
RBAC-protected APIs it provisions `e2e_motion` (`type=motion_sensor`,
`capabilities=["motion"]`) and the fixed-ID `e2e_motion_relay` rule:

```json
{
  "id": "e2e_motion_relay",
  "name": "E2E motion turns relay on",
  "house_id": "e2e_house",
  "enabled": false,
  "trigger": {
    "type": "device_state", "device_id": "e2e_motion",
    "field": "motion", "operator": "eq", "value": true
  },
  "conditions": [],
  "actions": [
    {"type": "device_state", "device_id": "e2e_relay", "state": {"on": true}}
  ]
}
```

Compatible records are reused; incompatible records fail without being overwritten.
Provisioning uses separate API transactions, so a partial provisioning failure may
leave a sensor or disabled rule; rerunning safely reuses those records. No user,
password, ownership or schema changes occur. Use the existing E2E owner account.

### One-time operator preparation

The new `motion-simulator` account has **write only** access to
`kzhome/v1/e2e_house/e2e_motion/state` and `.../status`. It has no subscriptions,
relay permissions, `/set`, ACK or telemetry permissions. Core and relay ACL entries
are preserved. The probe only emits the two allowed topics.

If not already provisioned, add the dedicated broker password interactively
from the repository root (never use `-c`, which would replace the password file):

```powershell
docker run --rm -it --user 0:0 --entrypoint mosquitto_passwd --mount "type=bind,source=$($PWD.Path)/deploy/local/mqtt,target=/bootstrap" eclipse-mosquitto:2 /bootstrap/passwords motion-simulator
if ($LASTEXITCODE -ne 0) { throw 'Motion identity provisioning failed' }
```

In your editor, create the ignored `deploy/local/motion.env` with a distinct client
ID and the same password you entered above (do not use a Core, relay or human password):

```dotenv
MOTION_MQTT_HOST=mosquitto
MOTION_MQTT_PORT=8883
MOTION_MQTT_USERNAME=motion-simulator
MOTION_MQTT_PASSWORD=<enter the dedicated motion password locally>
MOTION_MQTT_CLIENT_ID=e2e-motion-simulator-1
MOTION_CA_FILE=/run/mqtt/ca.crt
```

The existing broker startup script stages ACL/password files into `/run/mosquitto`.
An operator must explicitly apply the new files; editing the repository ACL alone
does not activate them. The following recreates **only Mosquitto**, briefly
interrupting MQTT; Core reconnects. Schedule this step as appropriate. Skip it if
the account and updated ACL are already active. No agent runs it automatically.

```powershell
docker compose --env-file deploy/local/production.env -f compose.production.yaml up -d --no-deps --force-recreate mosquitto
if ($LASTEXITCODE -ne 0) { throw 'Broker configuration activation failed' }
```

### Run acceptance (PowerShell, repository root)

Stop an existing standalone relay **only if it is running**:
`docker stop kzhome-relay-simulator`. Do not run Block 1, a second motion probe,
other controllers or concurrent acceptance runs against these devices. Other
enabled automations using the E2E devices are rejected by the runner.

```powershell
docker build -t kz-home-e2e:v0.8-local .
if ($LASTEXITCODE -ne 0) { throw 'Acceptance image build failed' }
docker run --rm -it --init --name kzhome-e2e-automation --network kzhome_backend --no-healthcheck --env-file deploy/local/relay.env --env-file deploy/local/motion.env --mount "type=bind,source=$($PWD.Path)/deploy/local/mqtt/ca.crt,target=/run/mqtt/ca.crt,readonly" kz-home-e2e:v0.8-local python -m simulator.e2e_automation --run
if ($LASTEXITCODE -ne 0) { throw 'Block 2 acceptance failed' }
```

Enter the existing E2E email/password at the prompts. Each MQTT connection has its
own identity, distinct client ID and verified TLS context. HTTP uses `core:8000`
inside the private Compose network. No host ports, Core settings or DB credentials
are needed by the runner. Tokens, passwords, raw responses and broker errors are
not printed; password input refuses the echoed-input fallback.

The runner disables the rule, publishes MQTT motion=false and relay=false, and
waits for persisted false states plus fresh MQTT status timestamps. It then enables
the rule and publishes motion=true with a new correlation ID. It requires:

- Fresh persisted motion=true event and motion GET=true.
- `automation_triggered` and `automation_completed` for the exact rule and correlation.
- An actual relay MQTT `/set` carrying that correlation and exactly `on=true`.
- Relay application, persisted `applied` ACK matching command ID **and** correlation,
  correlated relay state event, and relay GET=true.

`automation_completed` means actions were dispatched, not device confirmation;
the ACK/state checks supply that confirmation separately. The sensor correlation
propagates through `report_state` and AutomationService into the relay command.
Only MQTT commands/ACKs expose command IDs; sensor and state events use correlation.
The trigger is never synthesized via a service call or HTTP state update.

Provisioning, baseline and trigger phases each have a 30-second deadline (optional
`--timeout 5..60`); MQTT/HTTP operations use 5-second timeouts and polling is 200 ms.
The rule is disabled and devices report offline on normal exit, including ordinary
scenario failures. Success leaves motion=true and relay=true persisted; the next
run resets both through MQTT with the rule disabled. On interruption, connection
loss or cleanup failure, check the rule's enabled state before resuming other work;
there is no durable cleanup guarantee. Nothing deletes records or rewrites secrets.

The MQTT-only probe can also be run independently, without relay credentials:

```powershell
docker run --rm --init --name kzhome-motion-probe --network kzhome_backend --no-healthcheck --env-file deploy/local/motion.env --mount "type=bind,source=$($PWD.Path)/deploy/local/mqtt/ca.crt,target=/run/mqtt/ca.crt,readonly" kz-home-e2e:v0.8-local python -m simulator.mqtt_motion --run --motion false
```

Use `--motion true` for a one-shot true report; this may trigger any enabled matching
rule. The probe uses a 15-second coroutine deadline, has no subscriptions/reconnect loop, and
does not itself assert Core persistence. Acceptance hosts this same probe logic
alongside the unchanged relay implementation using separate MQTT connections.

Offline tests use the actual API, Gateway and AutomationService with SQLite and
in-memory transport. Live results are recorded above; source ACL checks do not
constitute a live negative-authorization test of every forbidden topic. Phase/I/O
timeouts do not guarantee a hard process-exit deadline under DNS/executor stalls
or failed cleanup. The latest-500-event window, exclusive
device control, retained reports and no hardware/restart-durability/reconnect
guarantees remain limitations. This changes no production automation behavior.
