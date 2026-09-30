import pytest
from sqlalchemy import create_engine, select, func
from sqlalchemy.orm import Session

from app.bootstrap_e2e import bootstrap
from app.db import Base
from app.models import (
    UserORM,
    HouseORM,
    DeviceORM,
    HouseMembershipORM,
    FloorORM,
    RoomORM,
)
from app.security import PasswordManager


def test_safe_database_error_does_not_expose_parameters():
    from sqlalchemy.exc import ProgrammingError
    from app.bootstrap_e2e import safe_error
    from app.core.errors import ConflictError

    class DriverError(Exception):
        sqlstate = "42501"

    original = ProgrammingError(
        "secret SQL",
        {"password": "secret-value"},
        DriverError("secret connection string"),
    )
    wrapped = ConflictError("unsafe secret-value")
    wrapped.__cause__ = original
    message = safe_error(wrapped)
    assert "42501" in message
    assert "privileges" in message
    assert "secret" not in message
    assert "secret" not in safe_error(ValueError("secret-value"))


def test_validation_error_does_not_echo_password():
    from app.bootstrap_e2e import safe_error

    with pytest.raises(ValueError) as caught:
        bootstrap(None, "invalid-email", "secret-password-value")
    assert "Invalid email" in safe_error(caught.value)
    assert "secret-password-value" not in safe_error(caught.value)


def test_late_failure_rolls_back_all_records(engine, monkeypatch):
    from app.services.device_service import DeviceService

    def fail(*args):
        raise RuntimeError("late failure")

    monkeypatch.setattr(DeviceService, "create", fail)
    with pytest.raises(RuntimeError):
        bootstrap(engine, "operator@example.com", "test-only-password")
    with Session(engine) as session:
        for model in (
            UserORM,
            HouseORM,
            HouseMembershipORM,
            FloorORM,
            RoomORM,
            DeviceORM,
        ):
            assert session.scalar(select(func.count()).select_from(model)) == 0


@pytest.fixture
def engine():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


def test_bootstrap_hashes_and_rerun_preserves_observed_state(engine):
    bootstrap(engine, "operator@example.com", "test-only-password")
    with Session(engine) as session:
        user = session.scalar(select(UserORM))
        original_hash = user.password_hash
        assert PasswordManager().verify(user.password_hash, "test-only-password")
        assert not user.is_superuser
        assert session.scalar(select(HouseMembershipORM)).role == "owner"
        relay = session.get(DeviceORM, "e2e_relay")
        relay.state = {"on": True}
        session.commit()
    bootstrap(engine, "operator@example.com", "test-only-password")
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(UserORM)) == 1
        assert session.scalar(select(func.count()).select_from(HouseMembershipORM)) == 1
        assert session.get(DeviceORM, "e2e_relay").state == {"on": True}
        assert session.scalar(select(UserORM)).password_hash == original_hash


@pytest.mark.parametrize(
    "email,password",
    [
        ("operator@example.com", "wrong-password"),
        ("another@example.com", "test-only-password"),
    ],
)
def test_conflict_does_not_reset_password_or_grant_ownership(engine, email, password):
    bootstrap(engine, "operator@example.com", "test-only-password")
    with pytest.raises(ValueError):
        bootstrap(engine, email, password)
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(UserORM)) == 1


def test_invalid_password_leaves_database_empty(engine):
    with pytest.raises(ValueError):
        bootstrap(engine, "operator@example.com", "short")
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(HouseORM)) == 0


def test_password_prompt_refuses_echo_fallback(monkeypatch):
    import getpass
    from app.bootstrap_e2e import BootstrapError, read_password

    monkeypatch.setattr(getpass, "getpass", getpass.fallback_getpass)
    monkeypatch.setattr(
        getpass, "_raw_input", lambda *args: pytest.fail("Echoed input reached")
    )
    with pytest.raises(BootstrapError, match="hidden password input"):
        read_password("Password: ")


def test_password_prompt_preserves_hidden_input(monkeypatch):
    import getpass
    from app.bootstrap_e2e import read_password

    monkeypatch.setattr(getpass, "getpass", lambda prompt: "test-only-password")
    assert read_password("Password: ") == "test-only-password"


def test_foreign_floor_conflict_rolls_back_new_user_and_house(engine):
    from app.bootstrap_e2e import BootstrapError

    with Session(engine) as session:
        session.add(HouseORM(id="foreign", name="Existing house"))
        session.flush()
        session.add(
            FloorORM(id="e2e_floor", house_id="foreign", name="Existing floor", order=0)
        )
        session.commit()
    with pytest.raises(BootstrapError, match="another house"):
        bootstrap(engine, "operator@example.com", "test-only-password")
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(UserORM)) == 0
        assert session.scalar(select(func.count()).select_from(HouseMembershipORM)) == 0
        assert session.get(HouseORM, "e2e_house") is None
        assert session.get(FloorORM, "e2e_floor").house_id == "foreign"
        assert session.get(HouseORM, "foreign") is not None
        assert session.scalar(select(func.count()).select_from(UserORM)) == 0
