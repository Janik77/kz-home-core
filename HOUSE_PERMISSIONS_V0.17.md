# Core v0.17 — current-user house permissions

## Discovery before implementation

Both worktrees were clean: Core on feature/house-permissions-v0.17, Web on main.
No branches were switched. Read PROJECT_STATUS, ARCHITECTURE, README, command
lifecycle v0.15 and mentor demo documentation; inspected auth dependencies,
routes, schemas, authentication/authorization/membership services, repositories,
security.py, RBAC and command lifecycle tests. Web is read-only.

The existing contract establishes identity with Bearer access JWT and checks the
database user is active on each request. JWT contains no house permissions.
AuthorizationService.has_permission evaluates membership.role using the central
ROLE_PERMISSIONS mapping; unknown roles/missing memberships grant nothing.
The router require helper hides missing membership as 404 Resource not found;
an existing member lacking the required permission receives 403 Insufficient
house permission and the existing authorization-denied audit behavior. Superuser
metadata provides no house bypass. MembershipRepository reads current database
membership, not a token snapshot. /auth/me returns identity only; enumerating
/houses/{house_id}/members requires owner-only member.read.

All current roles have house.read, device.read and device.control. Commands
independently require device.control in the device's resolved house before replay
or dispatch; result GET requires device.read in its URL house. Device eligibility,
binding, availability, conflict, idempotency and confirmation are separate checks.
These facts were established before adding the permission read endpoint.

## Exact HTTP contract

```http
GET /v1/houses/house-01/me/permissions
Authorization: Bearer <access-token>
```

HTTP 200, `Cache-Control: no-store`, `Content-Type: application/json`:

```json
{
  "house_id": "house-01",
  "permissions": [
    "device.control",
    "device.read",
    "house.read",
    "scene.read",
    "scene.run"
  ]
}
```

This example is a resident. `HousePermissionsRead` contains exactly `house_id: str`
and `permissions: list[Permission]`. Permission is the existing string enum in
app/security.py, also represented as an enum in OpenAPI. The server enumerates
that enum through AuthorizationService.has_permission and returns a sorted,
duplicate-free list. It does not maintain a second role map or accept client
claims about roles, user IDs, capabilities or permissions. There is no request
body, target-user parameter, role field, token, credential or other-member detail.
Unknown query parameters are ignored by the existing FastAPI convention; they
cannot select another identity or override the URL house.

## Existing role matrix

Derived from ROLE_PERMISSIONS in app/security.py; no grant changed in v0.17.

| Permission | owner | installer | technician | resident |
| --- | --- | --- | --- | --- |
| house.read | yes | yes | yes | yes |
| house.manage | yes | — | — | — |
| member.read | yes | — | — | — |
| member.manage | yes | — | — | — |
| device.read | yes | yes | yes | yes |
| device.control | yes | yes | yes | yes |
| device.manage | yes | yes | yes | — |
| device.onboard | yes | yes | — | — |
| scene.read | yes | yes | — | yes |
| scene.run | yes | yes | — | yes |
| scene.manage | yes | yes | — | — |
| automation.read | yes | yes | — | — |
| automation.manage | yes | yes | — | — |
| event.read | yes | yes | yes | — |
| security.audit.read | yes | — | — | — |

A user's roles may differ between houses; only the requested house is evaluated.
The existing roles contain no read-only role: tests that remove device.control
from a role exercise the central policy boundary without adding a runtime role.

## Authentication, IDOR and freshness

- 401: absent/malformed/invalid/expired access token, deleted or inactive user.
  Existing Bearer challenge and detail conventions are retained. Deactivation
  prevents use of a previously issued access token on subsequent requests.
- 404 with `{"detail":"Resource not found"}`: no membership in the URL house,
  including nonexistent houses, foreign houses and revoked memberships. Existing
  foreign and absent resources are indistinguishable; superuser has no bypass.
- 403 with `{"detail":"Insufficient house permission"}`: membership exists but
  its current policy lacks house.read. All four current roles have house.read,
  so this is a policy-change/fail-closed boundary, tested without changing runtime
  grants. The existing denied-authorization audit is retained.
- 200: active authenticated member with house.read. Each request queries current
  database membership and applies the existing server policy. Same JWT observes
  a committed role change or revocation on subsequent requests. No permission
  cache or JWT permission claim is introduced. Unknown roles grant nothing.

The read has no business-state mutation. Denials can generate existing security
audit events through the shared require helper. Error bodies use existing FastAPI
detail envelopes; `no-store` is explicitly set on successful permission responses.

## Frontend expectations and limits

Read for the selected house/session with authenticated HTTP and no-store caching.
Verify response.house_id matches that selection; discard results after a session
or house change. Enable relay control only when device.control is present AND the
device meets current Web/Core capability, online and binding requirements. Polling
command results separately requires device.read. Do not infer permission from
role names, device state, superuser metadata or successful house/device reads.

The response is an observation at request time, never a grant, lease or command
credential. Re-read when eligibility becomes stale; treat missing/error responses
as unknown/disabled and handle command errors normally. Membership and policy can
change between GET and POST. Every POST independently resolves the device's actual
house and requires device.control, including idempotent replays. A prior permission
response cannot bypass that check; tests explicitly revoke the grant after a
successful GET/POST and verify both new submissions and replay receive 403 without
publishing again. Cross-house device movement remains subject to existing checks;
this endpoint does not add an atomic expected-house command precondition.

The endpoint does not promise device availability, physical execution, exclusive
control, transport readiness or firmware safety. v0.15 queued/dispatched/applied/
failed/timed_out, idempotency, ACK+state verification and 30-second deadline are
unchanged. Legacy commands and member enumeration retain their existing behavior.
No migration, dependency, Web edit or installed-stack update is required or made.

The smallest Web milestone is to replace owner-membership enumeration with this
current-user permission read, retain device eligibility/session/house isolation,
and test all four roles plus permission revocation and stale responses. This is
separate Web work; no frontend file is changed here.

## Verification performed

| Check | Result |
| --- | --- |
| Focused permission/RBAC/command suite, before final additional per-house case | 45 passed, 55.36 s |
| Full normal pytest, including final permission tests and legacy/lifecycle regressions | 387 passed, 14 skipped, 151.78 s |
| Ruff app/simulator/tests/Alembic | Passed |
| Pydantic/OpenAPI schema tests | Passed in normal suite: enum identifiers, required fields, exact DTO, invalid permission rejection |
| Opt-in real isolated HTTP/PostgreSQL | 1 passed, 26.98 s |
| git diff --check | Passed |

The normal permission tests use real FastAPI/auth/services/repositories with
isolated SQLite; command tests use the existing migrated fixture with only MQTT
network I/O faked. They cover all four role sets, the same user in different
houses, missing/invalid auth, inactive users, foreign/absent houses, superuser
non-bypass, role changes, revocation, caller-supplied identity query arguments,
house.read denial and unknown-role fail-closed policy. Command tests verify 202
for each existing role, then remove device.control in the shared test policy and
verify both new POST and same-key replay return 403 without another publication.
The existing lifecycle/legacy tests cover confirmation, deadline and idempotency
regression. No static Python type checker is configured here; DTO/schema validation
was executed through Pydantic and OpenAPI tests, not claimed as mypy verification.

The Docker test uses actual httpx network HTTP against a freshly built Core,
PostgreSQL 17 and the existing exclusively owned production-component fixture.
It uses random project/image names, isolated credentials/networks/volumes,
loopback ephemeral ports and explicit migration of its new empty database to
unchanged head 0006_command_lifecycle. Independent SQL confirms zero commands
created by rejected offline/revoked requests and retained owner membership.
Role changes, revocation and deactivation are exercised in that fixture only.
Permission reads do not make an offline device controllable: authorized POSTs
still return 409; after revocation they return 404; inactive users get 401.
No intercepted HTTP responses or altered Core policies are used in this live test.
It does not test browser integration, successful MQTT execution or physical devices.

The test compares Docker inventories before setup and after finally cleanup,
including container IDs/state/start times/restart counts/configuration fingerprints,
volumes, networks and image entries. They matched; all four existing containers
and 17 existing volumes were preserved. Only fixture-created resources were
removed. kzhome-local and mentor/production data, secrets, ACLs and deployment
settings were not migrated, restarted, recreated or changed.

Normal-suite skips are environment/opt-in gates, including the new live test;
the latter was explicitly executed separately. Warnings are existing
Starlette/AnyIO and Alembic legacy path_separator deprecations (34 in full pytest).
An initial Ruff fixture-import naming conflict was fixed in test code before
the final clean lint/full-suite run; no runtime workaround was introduced.

Reproduce with the configured Core SDK (verify it before use):

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check app simulator tests alembic
$env:RUN_HOUSE_PERMISSIONS_DOCKER_TESTS = '1'
.\.venv\Scripts\python.exe -m pytest tests/test_house_permissions_live.py -q
git diff --check
```

## Files and Git

Starting Core: clean feature/house-permissions-v0.17 at
080061c8c3a6f353aaa4b47712f31d113a2168e7.
Starting/final Web: clean main at e2eb17427ae3ae1d9d780dc18d541d73032d43c3.
Final Core: same branch/HEAD, 5 modified + 3 untracked, nothing staged.

Modified: README.md, PROJECT_STATUS.md, ARCHITECTURE.md, app/api/routes.py,
app/schemas/auth.py.
Created: HOUSE_PERMISSIONS_V0.17.md, tests/test_house_permissions.py,
tests/test_house_permissions_live.py.

No branch switches, commits, pushes, merges, PRs, deployment, database migration
files, new dependencies or Web modifications.
