"""0017 preserves all existing columns and refuses destructive revision rollback."""

from pathlib import Path
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, MetaData, Table, select, text


def test_metadata_upgrade_preserves_rows_and_downgrade_guard(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'metadata.db'}")
    with engine.begin() as conn:
        cfg = Config()
        cfg.set_main_option(
            "script_location", str(Path(__file__).resolve().parents[1] / "alembic")
        )
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, "0016_durable_waits")
        conn.execute(text("INSERT INTO houses(id,name) VALUES('h','Original')"))
        conn.execute(
            text(
                "INSERT INTO floors(id,house_id,name,\"order\") VALUES('f','h','Floor',1)"
            )
        )
        conn.execute(text("INSERT INTO rooms(id,floor_id,name) VALUES('r','f','Room')"))
        conn.exec_driver_sql(
            "INSERT INTO devices(id,room_id,name,type,state,online,capabilities,metadata) VALUES('d','r','Relay','relay','{\"on\":false}',1,'[\"on_off\"]','{\"user\":\"preserve\"}')"
        )
        names = ("houses", "floors", "rooms", "devices")
        before = {
            name: dict(
                conn.execute(select(Table(name, MetaData(), autoload_with=conn)))
                .mappings()
                .one()
            )
            for name in names
        }
        command.upgrade(cfg, "head")
        for name in names:
            after = dict(
                conn.execute(select(Table(name, MetaData(), autoload_with=conn)))
                .mappings()
                .one()
            )
            assert after.pop("metadata_revision") == 1
            assert after == before[name]
        command.downgrade(cfg, "0016_durable_waits")
        command.upgrade(cfg, "head")
        conn.execute(text("UPDATE rooms SET metadata_revision=2 WHERE id='r'"))
        with pytest.raises(RuntimeError, match="Metadata revisions have been used"):
            command.downgrade(cfg, "0016_durable_waits")
        assert (
            conn.scalar(text("SELECT metadata_revision FROM houses WHERE id='h'")) == 1
        )
        assert (
            conn.scalar(text("SELECT metadata_revision FROM rooms WHERE id='r'")) == 2
        )
    engine.dispose()
