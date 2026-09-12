import os

from app.core import Settings
from app.db import create_db_engine, create_session_factory
from app.demo_data import automations, devices, floors, houses, rooms, scenes
from app.repositories import (
    AutomationRepository,
    DeviceRepository,
    FloorRepository,
    HouseRepository,
    RoomRepository,
    SceneRepository,
    UserRepository,
)
from app.services.auth_service import UserService


def add_missing(repository, items) -> None:
    for item in items:
        if not repository.exists(item.id):
            repository.create(item.model_dump())


def main(settings: Settings | None = None) -> None:
    settings = settings or Settings.from_env()
    engine = create_db_engine(settings)
    factory = create_session_factory(settings, engine)
    try:
        with factory() as session:
            add_missing(HouseRepository(session), houses())
            add_missing(FloorRepository(session), floors())
            add_missing(RoomRepository(session), rooms())
            add_missing(DeviceRepository(session), devices())
            add_missing(SceneRepository(session), scenes())
            add_missing(AutomationRepository(session), automations())
            demo_email = os.getenv("AUTH_DEMO_EMAIL")
            demo_password = os.getenv("AUTH_DEMO_PASSWORD")
            if (
                settings.app_env.lower() != "production"
                and demo_email
                and demo_password
            ):
                users = UserRepository(session)
                if users.by_email(demo_email) is None:
                    UserService(users).create(demo_email, demo_password)
    finally:
        engine.dispose()
    print("Demo data is ready.")


if __name__ == "__main__":
    main()
