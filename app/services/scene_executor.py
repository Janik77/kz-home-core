"""One process owns a run. Losing ownership never resumes or replays work."""

import asyncio
import logging
from datetime import timedelta
from sqlalchemy import update
from app.models import SceneRunORM
from app.core.errors import ConflictError
from app.services.command_clock import command_clock
from app.services.scene_run_service import SceneRunService
from app.services.scene_run_admission import deadline_predicate, lease_predicate

logger = logging.getLogger(__name__)


class SceneExecutor:
    def __init__(self, session_factory, transport, *, interval=0.2):
        self.session_factory, self.transport = session_factory, transport
        self.interval = interval
        self.tasks = set()

    def start(self, house_id, run_id, token):
        task = asyncio.create_task(self.run(house_id, run_id, token))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def stop(self):
        for task in tuple(self.tasks):
            task.cancel()
        await asyncio.gather(*tuple(self.tasks), return_exceptions=True)

    async def run(self, house_id, run_id, token):
        reason = None
        try:
            while True:
                with self.session_factory() as session:
                    now = command_clock.now()
                    ownership = session.execute(
                        update(SceneRunORM)
                        .where(
                            SceneRunORM.id == run_id,
                            SceneRunORM.executor_token == token,
                            SceneRunORM.execution_finished.is_(False),
                            SceneRunORM.execution_error.is_(None),
                            SceneRunORM.lease_until > now,
                            lease_predicate(session),
                            deadline_predicate(session),
                        )
                        .values(lease_until=now + timedelta(seconds=30))
                    )
                    session.commit()
                    if ownership.rowcount != 1:
                        reason = "executor_lost"
                        break
                    service = SceneRunService(session, self.transport)
                    run = service.row(house_id, run_id)
                    view = service.get(house_id, run_id)
                    if view.status != "running":
                        break
                    ready = next((a for a in view.actions if a.status == "ready"), None)
                    if ready is not None:
                        try:
                            await service.dispatch(
                                house_id,
                                run_id,
                                ready.position,
                                run.requested_by,
                                executor_token=token,
                            )
                        except ConflictError:
                            reason = "authorization_or_admission_lost"
                            break
                await asyncio.sleep(self.interval)
        except asyncio.CancelledError:
            reason = "executor_interrupted"
            raise
        except Exception:
            reason = "executor_interrupted"
            logger.exception("Scene executor interrupted")
        finally:
            with self.session_factory() as session:
                session.execute(
                    update(SceneRunORM)
                    .where(
                        SceneRunORM.id == run_id,
                        SceneRunORM.executor_token == token,
                        SceneRunORM.execution_finished.is_(False),
                    )
                    .values(execution_finished=True, execution_error=reason)
                )
                session.commit()
