from sqlalchemy import select
from sqlalchemy.orm import Session
from app.models import SceneORM
from app.repositories.base import Repository


class SceneRepository(Repository[SceneORM]):
    def __init__(self, session: Session) -> None:
        super().__init__(session, SceneORM)

    def for_houses(self, house_ids: list[str]) -> list[SceneORM]:
        if not house_ids:
            return []
        return list(
            self.session.scalars(
                select(SceneORM).where(SceneORM.house_id.in_(house_ids))
            ).all()
        )
