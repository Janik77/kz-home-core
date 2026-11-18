import json
import pytest
from alembic import command
from sqlalchemy import create_engine, text
from test_schema import migration_config


@pytest.mark.parametrize("data", ["draft", "consumed", "zone_history"])
def test_additive_schedule_migration_and_loss_refusal(tmp_path, data):
    engine = create_engine(f"sqlite:///{tmp_path / 'schedule-migration.db'}")
    with engine.begin() as connection:
        config = migration_config(connection)
        command.upgrade(config, "0014_numeric_thresholds")
        connection.execute(
            text("INSERT INTO houses(id,name) VALUES ('home','Original')")
        )
        original = {
            "type": "device_state",
            "device_id": "missing",
            "field": "on",
            "value": True,
        }
        connection.execute(
            text(
                "INSERT INTO automations(id,name,house_id,enabled,trigger,conditions,actions,threshold_state) VALUES ('rule','Original','home',false,:trigger,'[]','[]',:state)"
            ),
            {
                "trigger": json.dumps(original),
                "state": json.dumps({"armed": False, "revision": 1}),
            },
        )
        command.upgrade(config, "0015_house_schedules")
        row = connection.execute(
            text("SELECT timezone,timezone_revision,schedule_cursor,name FROM houses")
        ).one()
        assert tuple(row) == (None, 1, None, "Original")
        row = connection.execute(
            text(
                "SELECT trigger,threshold_state,schedule_state,schedule_effective_after FROM automations"
            )
        ).one()
        assert json.loads(row[0]) == original and json.loads(row[1]) == {
            "armed": False,
            "revision": 1,
        }
        assert json.loads(row[2]) == {} and row[3] is None
        if data == "draft":
            connection.execute(
                text("UPDATE automations SET trigger=:value"),
                {
                    "value": json.dumps(
                        dict(type="schedule", time="10:00", weekdays=[0])
                    )
                },
            )
        elif data == "consumed":
            connection.execute(
                text("UPDATE automations SET schedule_state=:value"),
                {"value": json.dumps({"occurrence": "2026-11-18T10:00:00+00:00"})},
            )
        else:
            connection.execute(text("UPDATE houses SET timezone_revision=2"))
        with pytest.raises(RuntimeError, match="restore a verified backup"):
            command.downgrade(config, "0014_numeric_thresholds")
        assert (
            connection.scalar(text("SELECT version_num FROM alembic_version"))
            == "0015_house_schedules"
        )
    engine.dispose()
