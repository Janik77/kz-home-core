from sqlalchemy.orm import Session
from app.models import SceneORM
from app.repositories.base import Repository


class SceneRepository(Repository[SceneORM]):
    def __init__(self, session: Session) -> None:
        super().__init__(session, SceneORM)
