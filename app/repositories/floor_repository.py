from sqlalchemy import select
from sqlalchemy.orm import Session
from app.models import FloorORM
from app.repositories.base import Repository


class FloorRepository(Repository[FloorORM]):
    def __init__(self, session: Session) -> None:
        super().__init__(session, FloorORM)

    def for_houses(self, house_ids: list[str]) -> list[FloorORM]:
        if not house_ids:
            return []
        return list(
            self.session.scalars(
                select(FloorORM).where(FloorORM.house_id.in_(house_ids))
            ).all()
        )
