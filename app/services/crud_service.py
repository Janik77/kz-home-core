from typing import Any, Generic, TypeVar

from pydantic import BaseModel

from app.core.errors import InvalidReferenceError
from app.repositories.base import Repository

ReadT = TypeVar("ReadT", bound=BaseModel)


class CrudService(Generic[ReadT]):
    def __init__(
        self,
        repository: Repository[Any],
        read_schema: type[ReadT],
        parents: dict[str, Repository[Any]] | None = None,
    ) -> None:
        self.repository = repository
        self.read_schema = read_schema
        self.parents = parents or {}

    def list(self) -> list[ReadT]:
        return [
            self.read_schema.model_validate(item) for item in self.repository.list()
        ]

    def get(self, entity_id: str) -> ReadT:
        return self.read_schema.model_validate(self.repository.get(entity_id))

    def create(self, data: BaseModel) -> ReadT:
        values = data.model_dump()
        self._validate_parents(values)
        return self.read_schema.model_validate(self.repository.create(values))

    def update(self, entity_id: str, data: BaseModel) -> ReadT:
        values = data.model_dump(exclude_unset=True, exclude_none=True)
        self._validate_parents(values)
        return self.read_schema.model_validate(
            self.repository.update(entity_id, values)
        )

    def delete(self, entity_id: str) -> None:
        self.repository.delete(entity_id)

    def _validate_parents(self, values: dict[str, Any]) -> None:
        for field, repository in self.parents.items():
            parent_id = values.get(field)
            if parent_id is not None and not repository.exists(parent_id):
                raise InvalidReferenceError(f"Parent entity for {field} does not exist")
