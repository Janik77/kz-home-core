from pathlib import Path
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text, MetaData, Table, select


def test_scene_draft_default_preserves_rows_and_downgrade_refuses_activation(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'drafts.db'}")
    with engine.begin() as conn:
        cfg = Config()
        cfg.set_main_option(
            "script_location", str(Path(__file__).resolve().parents[1] / "alembic")
        )
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, "0017_metadata_cas")
        conn.execute(text("INSERT INTO houses(id,name) VALUES('h','House')"))
        conn.execute(
            text(
                "INSERT INTO scenes(id,name,house_id,actions) VALUES('s','Scene','h','[]')"
            )
        )
        before = dict(
            conn.execute(select(Table("scenes", MetaData(), autoload_with=conn)))
            .mappings()
            .one()
        )
        command.upgrade(cfg, "head")
        after = dict(
            conn.execute(select(Table("scenes", MetaData(), autoload_with=conn)))
            .mappings()
            .one()
        )
        assert after.pop("is_draft") is False
        assert after == before
        command.downgrade(cfg, "0017_metadata_cas")
        command.upgrade(cfg, "head")
        conn.execute(text("UPDATE scenes SET is_draft=true WHERE id='s'"))
        with pytest.raises(RuntimeError, match="downgrade would activate"):
            command.downgrade(cfg, "0017_metadata_cas")
        assert conn.scalar(text("SELECT is_draft FROM scenes WHERE id='s'")) == 1
        assert (
            conn.scalar(text("SELECT version_num FROM alembic_version"))
            == "0018_scene_drafts"
        )
    engine.dispose()
