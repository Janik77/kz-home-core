"""Offline new-copy policy. Never use this against a running/original profile."""

from datetime import UTC, datetime, date
from decimal import Decimal
from hashlib import sha256
import json
import re
from sqlalchemy import MetaData, Table, inspect, select, update


def encode(value):
    if isinstance(value, datetime):
        return (
            value.replace(tzinfo=UTC).isoformat()
            if value.tzinfo is None
            else value.astimezone(UTC).isoformat()
        )
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, bytes):
        return value.hex()
    raise TypeError("Unsupported backup fingerprint value")


def row_json(row):
    return json.dumps(dict(row), sort_keys=True, separators=(",", ":"), default=encode)


def table_fingerprint(columns, rows):
    payload = json.dumps(
        {"columns": list(columns), "rows": sorted(row_json(row) for row in rows)},
        sort_keys=True,
        separators=(",", ":"),
    )
    return {"rows": len(rows), "sha256": sha256(payload.encode()).hexdigest()}


def validate_fingerprint_manifest(value):
    if not isinstance(value, dict) or not {
        "alembic_version",
        "scene_runs",
        "scene_run_waits",
        "device_commands",
    } <= set(value):
        raise ValueError("Invalid complete database fingerprint manifest")
    for name, fingerprint in value.items():
        if (
            not re.fullmatch(r"[a-z][a-z0-9_]*", name)
            or not isinstance(fingerprint, dict)
            or set(fingerprint) != {"rows", "sha256"}
            or type(fingerprint["rows"]) is not int
            or fingerprint["rows"] < 0
            or not isinstance(fingerprint["sha256"], str)
            or not re.fullmatch(r"[a-f0-9]{64}", fingerprint["sha256"])
        ):
            raise ValueError("Invalid complete database fingerprint manifest")


def database_snapshot(connection):
    schema = (
        connection.get_execution_options().get("schema_translate_map", {}).get(None)
    )
    names = inspect(connection).get_table_names(schema=schema)
    data = {}
    tables = {}
    for name in sorted(names):
        if not re.fullmatch(r"[a-z][a-z0-9_]*", name):
            raise ValueError("Unexpected backup table")
        table = Table(name, MetaData(), schema=schema, autoload_with=connection)
        tables[name] = table
        data[name] = [dict(row) for row in connection.execute(select(table)).mappings()]
    fingerprints = {
        name: table_fingerprint(table.c.keys(), data[name])
        for name, table in tables.items()
    }
    return tables, data, fingerprints


def full_fingerprints(connection):
    return database_snapshot(connection)[2]


def invalidate_restored_waits(connection, *, expected_before=None):
    """Caller must own NEW stopped target and one transaction before initialized.

    Manifest equality is checked on this transaction snapshot before any update.
    All rows/columns here are compared exactly to a computed narrow expected delta.
    """
    tables, before, fingerprints = database_snapshot(connection)
    if expected_before is not None and fingerprints != expected_before:
        raise ValueError("Restored data differs from complete backup fingerprints")
    runs = tables["scene_runs"]
    waits = {(r["run_id"], r["position"]): r for r in before["scene_run_waits"]}
    actions = {(r["run_id"], r["position"]): r for r in before["scene_run_actions"]}
    receipts = {(r["run_id"], r["position"]): r for r in before["scene_run_receipts"]}
    commands = {r["command_id"]: r for r in before["device_commands"]}
    changed = []
    now = datetime.now(UTC)
    for row in before["scene_runs"]:
        if not (
            row["durable_plan"]
            and row["wait_position"] is not None
            and row["executor_token"] is None
            and not row["execution_finished"]
            and row["execution_error"] is None
            and row["cancelled_at"] is None
        ):
            continue
        key = (row["id"], row["wait_position"])
        wait = waits.get(key)
        successor = actions.get((key[0], key[1] + 1))
        predecessor = actions.get((key[0], key[1] - 1))
        previous = receipts.get((key[0], key[1] - 1))
        command = commands.get(previous["command_id"]) if previous else None
        if (
            not wait
            or wait["started_at"] is None
            or wait["due_at"] is None
            or wait["completed_at"] is not None
            or not successor
            or successor["claimed"]
            or (key[0], key[1] + 1) in receipts
            or not predecessor
            or not command
            or command["device_id"] != predecessor["device_id"]
            or command["requested_on"] != predecessor["requested_on"]
            or command["house_id"] != row["house_id"]
            or command["status"] != "applied"
            or command["ack_status"] != "applied"
            or command["reported_on"] != command["requested_on"]
            or command["completed_at"] is None
        ):
            raise ValueError("Malformed parked wait; restore remains unstartable")
        values = {
            "execution_finished": True,
            "execution_error": "restore_invalidation",
            "cancelled_at": now,
        }
        count = connection.execute(
            update(runs).where(runs.c.id == row["id"]).values(**values)
        ).rowcount
        if count != 1:
            raise ValueError("Restored wait ownership changed")
        changed.append(
            {
                "run_id": row["id"],
                "wait_position": row["wait_position"],
                "reason": "restore_invalidation",
                "cancelled_at": now.isoformat(),
            }
        )
        row.update(values)
    expected = dict(fingerprints)
    expected["scene_runs"] = table_fingerprint(runs.c.keys(), before["scene_runs"])
    after = full_fingerprints(connection)
    if after != expected:
        raise ValueError("Restore policy changed data outside exact allowed delta")
    return {
        "policy": "cancel-cloned-parked-waits-v1",
        "changes": changed,
        "before": fingerprints,
        "after": after,
    }
