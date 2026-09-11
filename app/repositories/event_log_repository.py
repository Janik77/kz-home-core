from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from app.models import EventLogORM
from app.repositories.base import Repository


class EventLogRepository(Repository[EventLogORM]):
    def __init__(self, session: Session) -> None:
        super().__init__(session, EventLogORM)

    def filtered(
        self, house_id: str | None, event_type: str | None, limit: int
    ) -> list[EventLogORM]:
        statement: Select[tuple[EventLogORM]] = select(EventLogORM)
        if house_id is not None:
            statement = statement.where(EventLogORM.house_id == house_id)
        if event_type is not None:
            statement = statement.where(EventLogORM.event_type == event_type)
        statement = statement.order_by(EventLogORM.created_at.desc()).limit(limit)
        return list(self.session.scalars(statement).all())
