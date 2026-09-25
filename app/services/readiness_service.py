"""Read-only database readiness; migrations remain an operator action."""

import logging
from pathlib import Path

from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Engine, text
from sqlalchemy.exc import SQLAlchemyError

logger = logging.getLogger(__name__)


class ReadinessService:
    def __init__(self, engine: Engine) -> None:
        # Resolve relative to the application, not the process working directory.
        scripts = ScriptDirectory(str(Path(__file__).resolve().parents[2] / "alembic"))
        heads = scripts.get_heads()
        if len(heads) != 1:
            raise RuntimeError("Readiness requires exactly one packaged Alembic head")
        self.expected_head = heads[0]
        self.engine = engine

    def check(self, initialized: bool) -> str | None:
        if not initialized:
            return "application_not_initialized"
        try:
            # Borrow the application's engine; return the connection on all paths.
            with self.engine.connect() as connection:
                connection.execute(text("SELECT 1"))
                heads = MigrationContext.configure(connection).get_current_heads()
        except SQLAlchemyError:
            # Driver exceptions may contain credentials/SQL parameters. Never
            # interpolate them or attach a traceback to this public probe.
            logger.warning("Readiness database check failed")
            return "database_unavailable"
        if heads != (self.expected_head,):
            return "schema_revision_mismatch"
        return None
