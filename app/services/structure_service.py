from collections.abc import Iterable
from typing import TypeVar

from pydantic import BaseModel

from app.models import Floor, House, Room

T = TypeVar("T", bound=BaseModel)


class StructureNotFoundError(KeyError):
    pass


class StructureService:
    def __init__(self, houses: Iterable[House], floors: Iterable[Floor], rooms: Iterable[Room]) -> None:
        self._houses = {item.id: item for item in houses}
        self._floors = {item.id: item for item in floors}
        self._rooms = {item.id: item for item in rooms}

    def houses(self) -> list[House]:
        return self._list(self._houses)

    def floors(self) -> list[Floor]:
        return self._list(self._floors)

    def rooms(self) -> list[Room]:
        return self._list(self._rooms)

    def house(self, item_id: str) -> House:
        return self._get(self._houses, item_id)

    def floor(self, item_id: str) -> Floor:
        return self._get(self._floors, item_id)

    def room(self, item_id: str) -> Room:
        return self._get(self._rooms, item_id)

    @staticmethod
    def _list(items: dict[str, T]) -> list[T]:
        return [item.model_copy(deep=True) for item in items.values()]

    @staticmethod
    def _get(items: dict[str, T], item_id: str) -> T:
        item = items.get(item_id)
        if item is None:
            raise StructureNotFoundError(item_id)
        return item.model_copy(deep=True)
