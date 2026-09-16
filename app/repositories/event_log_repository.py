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

    def filtered_for_houses(
        self,
        house_ids: list[str],
        event_type: str | None,
        limit: int,
        audit_house_ids: list[str] | None = None,
    ) -> list[EventLogORM]:
        if not house_ids:
            return []
        statement = select(EventLogORM).where(EventLogORM.house_id.in_(house_ids))
        security_types = {
            "auth_login_succeeded",
            "auth_login_failed",
            "auth_refresh_succeeded",
            "auth_refresh_failed",
            "auth_logout",
            "authorization_denied",
        }
        if event_type is None:
            from sqlalchemy import or_

            statement = statement.where(
                or_(
                    EventLogORM.event_type.not_in(security_types),
                    EventLogORM.house_id.in_(audit_house_ids or []),
                )
            )
        if event_type is not None:
            statement = statement.where(EventLogORM.event_type == event_type)
        statement = statement.order_by(EventLogORM.created_at.desc()).limit(limit)
        return list(self.session.scalars(statement).all())
