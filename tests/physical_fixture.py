"""Trusted bootstrap for disposable acceptance databases only; absent from image."""

from sqlalchemy import select, func
from sqlalchemy.orm import Session

from app.models import HouseORM, UserORM
from app.repositories import UserRepository
from app.repositories.physical_device_repository import PhysicalDeviceRepository
from app.schemas.onboarding import InventoryRegistration
from app.services.auth_service import UserService
from app.services.onboarding_service import OnboardingService


def initialize_inventory(engine, seed):
    with engine.begin() as connection:
        with Session(bind=connection, join_transaction_mode="rollback_only") as session:
            # Never adopt an existing installation. The Docker fixture owns a new volume.
            if session.scalar(
                select(func.count()).select_from(UserORM)
            ) or session.scalar(select(func.count()).select_from(HouseORM)):
                raise RuntimeError("Acceptance fixture requires an empty database")
            ids = {}
            for role in ("owner", "foreign", "resident"):
                ids[role] = (
                    UserService(UserRepository(session))
                    .create(f"{role}@example.test", seed["password"])
                    .id
                )
            OnboardingService(PhysicalDeviceRepository(session)).register_inventory(
                InventoryRegistration(
                    device_id="fixture_relay",
                    hardware_id="fixture-serial",
                    hardware_model="esp32-c6-relay-v1",
                    claim_code=seed["claim_code"],
                )
            )
            return ids
