from sqlalchemy.orm import Session
from app.models import HouseORM
from app.repositories.base import Repository


class HouseRepository(Repository[HouseORM]):
    def __init__(self, session: Session) -> None:
        super().__init__(session, HouseORM)
