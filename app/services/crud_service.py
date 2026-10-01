from typing import Any, Generic, TypeVar

from pydantic import BaseModel

from app.core.errors import ConflictError, InvalidReferenceError
from app.repositories.base import Repository
from app.repositories.physical_device_repository import PhysicalDeviceRepository
from app.services.onboarding_service import PhysicalBindingGuard

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
        self._guard_binding(entity_id, values)
        return self.read_schema.model_validate(
            self.repository.update(entity_id, values)
        )

    def delete(self, entity_id: str) -> None:
        self._guard_binding(entity_id, {}, deleting=True)
        self.repository.delete(entity_id)

    def _guard_binding(
        self, entity_id: str, values: dict, *, deleting: bool = False
    ) -> None:
        kind = {"houses": "house", "floors": "floor", "rooms": "room"}.get(
            self.repository.model.__tablename__
        )
        if kind is None:
            return
        physical = PhysicalDeviceRepository(self.repository.session)
        target = values.get("house_id")
        if "floor_id" in values:
            target = physical.structure_house("floor", values["floor_id"])
        PhysicalBindingGuard(physical).structure_mutation(
            kind, entity_id, target, deleting=deleting
        )
        if (
            "floor_id" in values
            and physical.structure_house("floor", values["floor_id"]) != target
        ):
            raise ConflictError("Target location changed concurrently; retry")

    def _validate_parents(self, values: dict[str, Any]) -> None:
        for field, repository in self.parents.items():
            parent_id = values.get(field)
            if parent_id is not None and not repository.exists(parent_id):
                raise InvalidReferenceError(f"Parent entity for {field} does not exist")
