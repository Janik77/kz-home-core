"""Explicit operator-only E2E provisioning; never invoked by application startup."""

import getpass
import re
import warnings

from pydantic import ValidationError
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.core import Settings
from app.db import create_db_engine
from app.events import EventBus
from app.repositories import (
    DeviceRepository,
    FloorRepository,
    HouseRepository,
    MembershipRepository,
    RoomRepository,
    UserRepository,
)
from app.schemas import DeviceCreate, FloorCreate, RoomCreate, LoginRequest
from app.security import PasswordManager
from app.services.auth_service import UserService
from app.services.device_service import DeviceService
from app.services.readiness_service import ReadinessService


class BootstrapError(ValueError):
    """Only fixed, operator-safe messages may be used with this exception."""


def read_password(prompt):
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            return getpass.getpass(prompt)
    except getpass.GetPassWarning:
        raise BootstrapError(
            "A terminal with hidden password input is required"
        ) from None


def safe_error(error):
    if isinstance(error, BootstrapError):
        return str(error)
    if isinstance(error, ValidationError):
        return "Invalid email or password length; use a valid email and 12–256 character password"
    # Repositories wrap integrity errors; inspect causes without printing SQL,
    # parameters, driver messages, connection strings, or user-supplied values.
    cause = error
    while cause is not None:
        if isinstance(cause, DBAPIError):
            code = getattr(cause.orig, "sqlstate", None)
            reasons = {
                "42501": "Database role lacks required table/schema privileges; review migration ownership and grants",
                "23505": "Unique record conflict; check existing E2E IDs and avoid concurrent bootstrap runs",
                "23503": "Related-record constraint failed; check E2E parent records and schema",
                "23502": "Required database column is missing a value; compare deployed models and schema",
                "42P01": "Required table is missing or outside the database search path",
                "42703": "Database column mismatch; compare deployed models and schema",
                "22001": "A value exceeds the database column length",
            }
            if isinstance(code, str) and re.fullmatch(r"[0-9A-Z]{5}", code):
                return f"{reasons.get(code, 'Database operation failed')} (SQLSTATE {code})"
            return "Database operation failed; check connectivity and database-role permissions"
        cause = cause.__cause__
    return "Unexpected bootstrap failure; inspect deployed code compatibility (exception details withheld)"


def bootstrap(engine, email, password):
    if not PasswordManager.MIN_LENGTH <= len(password) <= PasswordManager.MAX_LENGTH:
        raise BootstrapError("Password must be between 12 and 256 characters")
    credentials = LoginRequest(email=email.strip().lower(), password=password)
    email = str(credentials.email)
    # Repository commits join this outer transaction without committing it.
    # Any conflict rolls back the entire bootstrap, including user creation.
    with engine.begin() as connection:
        with Session(bind=connection, join_transaction_mode="rollback_only") as session:
            users = UserRepository(session)
            houses = HouseRepository(session)
            floors = FloorRepository(session)
            rooms = RoomRepository(session)
            devices = DeviceRepository(session)
            user = users.by_email(email)
            if user is not None:
                if (
                    not user.is_active
                    or user.is_superuser
                    or not PasswordManager().verify(user.password_hash, password)
                ):
                    raise BootstrapError(
                        "Existing user is inactive, privileged, or password does not match; no changes made"
                    )
            else:
                user = UserService(users).create(email, password)
            if houses.exists("e2e_house"):
                member = MembershipRepository(session).for_house(user.id, "e2e_house")
                if member is None or member.role != "owner":
                    raise BootstrapError("Existing house is not owned by this user")
            else:
                houses.create_with_owner(
                    {"id": "e2e_house", "name": "E2E acceptance"}, user.id
                )
            if floors.exists("e2e_floor"):
                if floors.get("e2e_floor").house_id != "e2e_house":
                    raise BootstrapError("Existing floor belongs to another house")
            else:
                floors.create(
                    FloorCreate(
                        id="e2e_floor", house_id="e2e_house", name="E2E floor", order=0
                    ).model_dump()
                )
            if rooms.exists("e2e_room"):
                if rooms.get("e2e_room").floor_id != "e2e_floor":
                    raise BootstrapError("Existing room belongs to another floor")
            else:
                rooms.create(
                    RoomCreate(
                        id="e2e_room", floor_id="e2e_floor", name="E2E room"
                    ).model_dump()
                )
            if devices.exists("e2e_relay"):
                device = devices.get("e2e_relay")
                if (
                    device.room_id != "e2e_room"
                    or device.type != "relay"
                    or device.capabilities != ["on_off"]
                ):
                    raise BootstrapError("Existing device is incompatible")
            else:
                DeviceService(devices, rooms, EventBus()).create(
                    DeviceCreate(
                        id="e2e_relay",
                        room_id="e2e_room",
                        name="E2E relay",
                        type="relay",
                        capabilities=["on_off"],
                        state={},
                        online=False,
                    )
                )


def main():
    engine = None
    stage = "production settings"
    try:
        settings = Settings.from_env()
        stage = "database/schema readiness"
        engine = create_db_engine(settings)
        if ReadinessService(engine).check(True):
            raise BootstrapError(
                "Database must be reachable and migrated before bootstrap"
            )
        stage = "operator input"
        email = input("E2E user email: ").strip()
        password = read_password("E2E user password (12–256 characters): ")
        if password != read_password("Confirm password: "):
            raise BootstrapError("Passwords do not match")
        stage = "atomic E2E provisioning"
        bootstrap(engine, email, password)
        print(
            "E2E user/owner, house, floor, room and relay verified; bootstrap complete."
        )
    except Exception as error:
        print(f"Bootstrap failed at {stage}: {safe_error(error)}.")
        raise SystemExit(1) from None
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == "__main__":
    main()
