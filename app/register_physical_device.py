"""Explicit trusted inventory entry; never runs during application startup."""

import argparse

from app.bootstrap_e2e import read_password
from app.core import Settings
from app.db import create_db_engine
from app.repositories.physical_device_repository import PhysicalDeviceRepository
from app.schemas.onboarding import InventoryRegistration
from app.services.onboarding_service import OnboardingService
from app.services.readiness_service import ReadinessService
from sqlalchemy.orm import Session


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device-id", required=True)
    parser.add_argument("--hardware-id", required=True)
    parser.add_argument(
        "--hardware-model", choices=("esp32-c6-relay-v1",), required=True
    )
    args = parser.parse_args()
    engine = None
    try:
        data = InventoryRegistration(
            device_id=args.device_id,
            hardware_id=args.hardware_id,
            hardware_model=args.hardware_model,
            claim_code=read_password("Random 32-byte URL-safe claim code (hidden): "),
        )
        engine = create_db_engine(Settings.from_env())
        if ReadinessService(engine).check(True):
            raise RuntimeError("Database must be migrated")
        with Session(engine) as session:
            OnboardingService(PhysicalDeviceRepository(session)).register_inventory(
                data
            )
    except Exception:
        # Do not print validation/driver errors: they can contain input/parameters.
        print(
            "Inventory registration failed; check input, identity conflicts and database readiness"
        )
        raise SystemExit(1) from None
    finally:
        if engine is not None:
            engine.dispose()
    print("Physical inventory registered; broker access remains operator-managed")


if __name__ == "__main__":
    main()
