# Core CI

`.github/workflows/core-ci.yml` runs on pull requests, pushes to `main`, and
manual dispatch. It uses Python 3.12 (as the Core Dockerfile does) and the pinned
packages in `requirements-dev.txt` / `requirements.txt`.

## Offline checks

```sh
python -m pip install -r requirements-dev.txt
python -m ruff check .
python -m pytest -q -ra
```

Run from a clean checkout without installation `.env` files or live-test opt-ins.
The existing tests cover HTTP/service behavior, SQLite migrations, command
lifecycle and the software relay/acceptance logic using in-process substitutes.
They do not establish a real MQTT broker connection or prove hardware behavior.
The skip report explicitly lists checks requiring PostgreSQL or Docker opt-ins.

## Disposable PostgreSQL checks

The second job starts its own PostgreSQL 17 service with CI-only credentials.
It runs an empty-database `upgrade head -> downgrade base -> upgrade head` and
the existing PostgreSQL migration, concurrent claim and provisioning CAS/locking
tests. Those tests use isolated schemas; they are not replaced by SQLite tests.

To reproduce, start an empty, dedicated PostgreSQL 17 database, then run:

```sh
export APP_ENV=test MQTT_ENABLED=false KZHOME_SIMULATOR_ENABLED=false
export DATABASE_URL='postgresql+psycopg://core_ci:ci-only-disposable@127.0.0.1:5432/core_ci'
export TEST_POSTGRESQL_URL="$DATABASE_URL"
python -m alembic upgrade head
python -m alembic downgrade base
python -m alembic upgrade head
python -m pytest -q -ra tests/test_schema.py tests/test_onboarding_postgresql.py tests/test_provisioning_postgresql.py
```

**Use only a disposable test database:** the explicit migration round trip
removes tables. Never copy an installation database URL into these commands.

## Scope

No deployment, device provisioning, real hardware commands, installation secrets,
or physical-command safety changes are part of this workflow. Docker/TLS/MQTT
live acceptance suites remain explicitly opt-in and are not claimed as CI
coverage. A passing offline simulator test is not a broker-level ACK/state round
trip. The existing runners in `simulator/` remain the entry points for that work.
