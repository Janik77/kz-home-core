# One-relay operator commissioning (v0.11)

This bridges [v0.10 Core onboarding](DEVICE_ONBOARDING.md) to the existing
Mosquitto TLS/password-file deployment. It is an explicit operator procedure,
not a credential API or broker administration service. No new schema, migrations,
wire fields or firmware changes are needed. Hardware acceptance remains deferred.

## Identity and security boundary

The installed Core must contain v0.10 onboarding, with the explicit
`0005_device_onboarding` migration applied and `/ready` passing. Follow README's
existing image/deployment/migration steps first; a repository merge does not
update the running Core or database. No migration is run by this workflow.

First register trusted inventory and claim it through v0.10. Use the **returned
permanent house/device IDs**, never a hardware serial, room ID, or proposed owner.
Owner/installer authorization remains the existing `device.onboard` permission.
Keep the device `provisioning`/`inactive` while preparing broker access. Never
grant access to unclaimed or terminally revoked inventory. The host ACL helper
does not access Core or verify lifecycle; the trusted operator checks it first.

For example, `house_001` / `device_c6_001` maps deterministically to MQTT username
`kzdevice-device_c6_001`. The stable, globally unique Core device ID makes this
username unique. Choose a distinct MQTT client ID as well. Client ID is connection
routing, not authentication or ownership proof. Device identity is independent
of a human JWT, the Core account and the E2E simulator accounts.

Exactly these permissions are granted; everything else is denied:

```text
user kzdevice-device_c6_001
topic read kzhome/v1/house_001/device_c6_001/set
topic write kzhome/v1/house_001/device_c6_001/ack
topic write kzhome/v1/house_001/device_c6_001/state
topic write kzhome/v1/house_001/device_c6_001/status
```

There are no physical-device wildcards, reverse-direction access, or telemetry
permission. This relay needs only the four topics above; Protocol v1 telemetry
remains available to other deliberately authorized profiles. Anonymous access
stays disabled, the existing Core/E2E ACLs are preserved, and Core still checks
permanent house binding and active lifecycle at ingress/control.

Generate a unique **32-byte random password** (for example a 43-character URL-safe
encoding) in a trusted password manager/secret tool. Store/deliver it there, with
protected operator/firmware access. A memorable password, serial, claim code,
human password or Core password is unacceptable. The procedure cannot measure
entropy; generation is an operator duty. Core never receives, stores or returns
this reusable password. The broker stores only `mosquitto_passwd` hashes.

Use a private terminal without transcription or shell tracing. Do not put secrets
in command arguments, logs, tracked files, API metadata, build arguments or MQTT.
Protect host inputs with Windows ACLs or Linux mode 0600; public CA mode 0644 is
appropriate. `deploy/local/` is already excluded from Git and build context.
Neither ignore file needs changing, and no local installation inputs are supplied.

## Prepare and apply (PowerShell, repository root)

Use the configured project interpreter for `python`. Run one operator update at
a time. The helper deliberately manages **one** physical relay in the local ACL;
it refuses a second/different binding or manual edits rather than replacing them.
Grant/check/revoke reruns are deterministic. Failed atomic replacement preserves
the previous file. Changes to the tracked base ACL require explicit local review.

```powershell
$house = 'house_001'           # replace with the actual claimed house ID
$device = 'device_c6_001'      # replace with the actual claimed device ID
$username = "kzdevice-$device"
python deploy/commission_device.py grant --house-id $house --device-id $device
if ($LASTEXITCODE -ne 0) { throw 'ACL preparation failed' }
python deploy/commission_device.py check --house-id $house --device-id $device
if ($LASTEXITCODE -ne 0) { throw 'ACL verification failed' }
if (!(Test-Path -LiteralPath deploy/local/mqtt/passwords -PathType Leaf)) { throw 'Initialize the existing Core password file using README first' }
docker run --rm -it --user 0:0 --entrypoint mosquitto_passwd --mount "type=bind,source=$($PWD.Path)/deploy/local/mqtt,target=/bootstrap" eclipse-mosquitto:2 /bootstrap/passwords $username
if ($LASTEXITCODE -ne 0) { throw 'Broker password update failed' }
```

Enter the generated password at the hidden native prompts. **Never use `-c` for
this device**: it would erase existing accounts. The helper writes only the
ignored `deploy/local/mqtt/acl`; it does not touch passwords or certificates.
There is no plaintext batch-password option in this workflow.

In the protected `deploy/local/production.env`, set the nonsecret selector:

```dotenv
MQTT_ACL_FILE=./deploy/local/mqtt/acl
```

The base Compose default remains the tracked Core/E2E ACL. This explicit selector
mounts the local ACL read-only at the same `/bootstrap/acl` path. No broad directory
or CA private key is mounted. Select one of the network modes below **before**
applying configuration. Always use the same env file and Compose files afterward.

Internal-only mode (sufficient for software commissioning on Docker backend):

```powershell
function dc { docker compose --env-file deploy/local/production.env -f compose.production.yaml @args; if ($LASTEXITCODE -ne 0) { throw 'Compose command failed' } }
```

Optional physical LAN mode: assign an actual private IPv4 address to the Docker
host, then set `MQTT_LAN_BIND_IP` in the protected production env file. The following
is an **example**, not an address supplied/detected by this repository:

```dotenv
MQTT_LAN_BIND_IP=192.168.50.10
```

Validate the actual chosen address and include the optional override:

```powershell
python deploy/commission_device.py check --house-id $house --device-id $device --lan-ip 192.168.50.10
if ($LASTEXITCODE -ne 0) { throw 'LAN/ACL validation failed' }
function dc { docker compose --env-file deploy/local/production.env -f compose.production.yaml -f compose.mqtt-lan.yaml @args; if ($LASTEXITCODE -ne 0) { throw 'Compose command failed' } }
```

The override requires an explicit address with no default. The preflight rejects
wildcard, public, loopback, link-local and IPv6 addresses. Supply the **same**
validated value to Compose and verify the effective publication after recreation.
Compose interpolation itself checks presence, not IP policy; do not skip preflight.
Only TLS 8883 is published on that address. Mosquitto retains the internal backend
and additionally joins a non-internal `mqtt_lan` bridge for Docker port publishing.
PostgreSQL has no host port and remains only on backend; Core HTTP remains exactly
`127.0.0.1:8000`. This adds no public Internet listener, HTTP proxy or port 1883.

Before LAN activation, firewall the Docker-published TCP 8883 path to the selected
device VLAN/subnet (and designated commissioning host) only, including Docker's
forwarding rules on Linux. Deny WAN/router port forwarding and unrelated VLANs;
binding a private IP is not a firewall. Permit device DNS and trusted time setup
as required for certificate validation. Confirm isolation from another VLAN.

### TLS names and public CA

For this procedure the ESP32 broker host is **`mqtt.kzhome.home.arpa`**, port
**8883**. Configure installation DNS so this name resolves to the selected LAN
address from the device VLAN. The server certificate must have **both** DNS SANs:

```text
DNS:mosquitto
DNS:mqtt.kzhome.home.arpa
```

Core continues to connect to `mosquitto` on Docker backend. An existing certificate
with only `mosquitto` is insufficient for ESP32 LAN access. Arrange replacement
through the existing external certificate process and operator-owned files before
enabling LAN; this block supplies no CA/issuance automation or private material.
If the installation chooses another DNS name, replace it consistently in DNS,
the certificate SAN, firmware configuration and LAN validation. The firmware
must use that DNS name with full chain/hostname verification and trust the public
CA certificate. Connecting by raw IP requires a matching **IP SAN**; this workflow
uses DNS instead. Do not disable verification or deliver the CA/server private key.

Apply only the broker, with a scheduled brief MQTT interruption:

```powershell
dc config --quiet
dc up -d --no-deps --force-recreate mosquitto
# LAN mode only: must show the exact chosen LAN IP and port 8883
dc port mosquitto 8883
```

`config --quiet` avoids printing expanded secrets. The existing bootstrap stages
0600 broker-owned files in private tmpfs and drops privileges. Recreating the
broker refreshes read-only file mounts/copies and terminates existing sessions;
an ACL edit or HUP alone is not this workflow's completion step. Core reconnects
and restores subscriptions. No Core/PostgreSQL recreation, migrations or volume
deletion is needed. After a CA change also refresh Core's CA mount/recreate Core
using its existing loopback deployment procedure; changing the CA is separate
operator work. Verify Core reconnection without printing configuration contents.

## Verify before activation

Use the existing relay environment contract for a **separate physical identity**.
An optional ignored `deploy/local/device-c6.env` for the software check has:

```dotenv
RELAY_MQTT_HOST=mosquitto
RELAY_MQTT_PORT=8883
RELAY_MQTT_USERNAME=kzdevice-device_c6_001
RELAY_MQTT_PASSWORD=<unique operator-generated password, inserted locally>
RELAY_MQTT_CLIENT_ID=commissioning-device-c6-001
RELAY_CA_FILE=/run/mqtt/ca.crt
RELAY_HOUSE_ID=house_001
RELAY_DEVICE_ID=device_c6_001
```

This optional plaintext local simulator input needs the same protected OS access
as existing E2E env files; prefer protected secret-manager environment injection.
It never enters Core's environment/database or an image. Docker administration
can inspect container environment and belongs to the trusted operator boundary.
Do not leave test copies on a customer machine or use the E2E/Core password.

Disconnect the physical relay/standalone simulator while probing. The probe uses
random unregistered foreign IDs and non-retained invalid-v1 markers, which cannot
control a conforming device. It does not claim Core state or hardware acceptance.
It requires the existing Core MQTT password at a hidden prompt for its temporary
observer connection; it does not change that account or install an observer user.

```powershell
docker build -t kz-home-commissioning:v0.11-local .
if ($LASTEXITCODE -ne 0) { throw 'Commissioning image build failed' }
docker run --rm -it --init --network kzhome_backend --no-healthcheck --env-file deploy/local/device-c6.env --mount "type=bind,source=$($PWD.Path)/deploy/local/mqtt/ca.crt,target=/run/mqtt/ca.crt,readonly" kz-home-commissioning:v0.11-local python -m simulator.commissioning_probe --run
if ($LASTEXITCODE -ne 0) { throw 'Broker isolation verification failed' }
```

It proves anonymous/wrong-password denial, own set delivery and ack/state/status
publication, denied reverse direction/telemetry/foreign-house/foreign-device
publishes, and filtering of foreign reads even when wildcard subscriptions receive
positive SUBACKs. MQTT 5 PUBACK reason 135 is required for denied writes; a timeout,
TLS/network error or a publish call returning successfully is not denial evidence.
This diagnostic use of MQTT 5 changes no Protocol v1 payload or firmware contract.

For LAN validation, run `python -m simulator.commissioning_probe --run` from a
trusted machine on the designated device VLAN with the same `RELAY_*` variables
securely injected, but host `mqtt.kzhome.home.arpa` and a readable public CA path.
The internal Docker command alone proves neither LAN DNS/firewall reachability
nor the deployed certificate's additional SAN. Preserve hostname verification.

Only after checks pass, an owner/installer calls the existing
`POST /houses/{house_id}/physical-devices/{device_id}/activate` with
`{"broker_access_confirmed":true}`. Deliver IDs, unique credentials, broker DNS,
port and public CA securely to firmware using an external installation method.
Wi-Fi delivery/BLE/SoftAP/flashing remain deferred. Activation does not mark online.

Without hardware, the existing `simulator.mqtt_relay` can use this identity and
env file via the same one-off container command (replace the probe module). After
activation, perform authorized `/devices/{device_id}/on` then `/off`, requiring
matching persisted ACK/state correlation and final GET values as in
[existing E2E acceptance](simulator/E2E_ACCEPTANCE.md). Do not run another relay
or controller concurrently. The existing automated v0.8 runner remains restricted
to its E2E IDs; it does not silently adopt physical inventory.

v0.12 adds a separate [physical identity acceptance runner](simulator/PHYSICAL_ACCEPTANCE.md)
for these already claimed/active IDs, using the same `RELAY_*` environment file.
It checks ON/OFF, correlated persisted ACK/state, duplicate handling and offline
dispatch without changing lifecycle or broker credentials. Run its documented
one-off command with exclusive relay control. Its isolated full-stack test passed
on 2026-10-04 (**1 passed in 93.08 s**), including PostgreSQL persistence/restart
and real broker isolation checks. No physical hardware or existing-installation
verification is claimed.

## Rotate and revoke

Rotation keeps house/device/username unchanged. First Core-deactivate the device
via the existing API. Run the same interactive `mosquitto_passwd` command without
`-c`, entering a fresh unique generated password, then `dc config --quiet` and
`dc up -d --no-deps --force-recreate mosquitto`. With securely injected **old**
credentials, run the probe with `--run --expect-denied`: only explicit broker
authentication denial passes, not TLS/network failure. Update the device's secret
through secure external delivery; run the normal probe with the new password,
then explicitly reactivate Core. Broker recreation terminates old live sessions;
Core HTTP deactivation alone does not revoke MQTT access.

Terminal revocation: Core-revoke first, then remove both the ACL and password hash:

```powershell
python deploy/commission_device.py revoke --house-id $house --device-id $device
if ($LASTEXITCODE -ne 0) { throw 'ACL revocation failed' }
docker run --rm --user 0:0 --entrypoint mosquitto_passwd --mount "type=bind,source=$($PWD.Path)/deploy/local/mqtt,target=/bootstrap" eclipse-mosquitto:2 -D /bootstrap/passwords $username
if ($LASTEXITCODE -ne 0) { throw 'Password hash revocation failed; inspect account existence' }
dc config --quiet
dc up -d --no-deps --force-recreate mosquitto
```

Confirm `--run --expect-denied` with the last device credential. ACL revoke reruns
are harmless; `mosquitto_passwd -D` can report an absent user on a repeat, so inspect
that nonsecret account-existence condition rather than treating unrelated failures
as success. No other account is removed. Never re-grant a terminally revoked Core
identity; recovery/transfer are not implemented. Retained state/status survive
broker recreation but cannot authorize a revoked/inactive Core identity. This
workflow uses fresh, clean diagnostic sessions and non-retained set; general
retained/queued-message cleanup and freshness are existing release limitations.

## Validation and remaining boundary

Normal pytest checks deterministic exact ACLs, injection/global-pattern rejection,
binding conflicts, idempotent files, replacement failure, IP policy, ignore rules,
private production defaults and probe authorization/secret-safe diagnostics.
Explicit isolated Docker validation uses only generated temporary test TLS/hash
inputs and a uniquely named broker on a random loopback port; no operator files,
running services, production database or named installation volumes are used:

```powershell
$env:RUN_COMMISSIONING_DOCKER_TESTS = '1'
python -m pytest tests/test_commissioning_live.py -q
Remove-Item Env:RUN_COMMISSIONING_DOCKER_TESTS
```

It checks effective Compose defaults/LAN merge/missing address, real broker TLS
and hostname/untrusted-CA rejection, the probe's positive/negative ACLs (including
detection of a deliberately broadened fixture ACL), existing relay v1
command/ACK/state traffic, old-password denial/new-password success and revocation
with old live-session termination and Core account preserved. Linux-container
Docker, Compose v2+, locally available
`eclipse-mosquitto:2` and `alpine/openssl:latest` images are needed. Certificate
generation here is disposable test setup, not production certificate automation.
The test refuses image pulls. Paho MQTT 2.1.0, already used transitively by aiomqtt,
is pinned explicitly because the probe uses its public MQTT 5 callbacks.

Session results and exact suite totals are in [PROJECT_STATUS.md](PROJECT_STATUS.md).
Validation on 2026-10-02: focused suite **218 passed**; full normal suite
**287 passed, 6 skipped**; opt-in isolated Docker suite **2 passed**. Ruff,
compileall, whitespace checks and sole Alembic head `0005_device_onboarding`
passed. Four skips require a dedicated PostgreSQL test URL; two are the Docker
checks, which passed separately. The existing Starlette/AnyIO warning remains.
No physical ESP32 or deployed LAN/Core/PostgreSQL acceptance follows from the
isolated broker fixture. Remaining steps are actual operator setup and LAN probe,
secure firmware/network installation, optional dedicated PostgreSQL checks, then
hardware ON/OFF/ACK/state, reconnect and revocation acceptance when hardware arrives.
Heartbeat/freshness, command outcomes and physical automation loop safety remain
separate existing release gaps.
