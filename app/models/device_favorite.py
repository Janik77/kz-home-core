from sqlalchemy import ForeignKey, ForeignKeyConstraint, Index, String
from sqlalchemy.orm import Mapped, mapped_column, relationship, backref
from app.db.base import Base


class DeviceFavoriteORM(Base):
    __tablename__ = "device_favorites"
    __table_args__ = (
        ForeignKeyConstraint(
            ["user_id", "house_id"],
            ["house_memberships.user_id", "house_memberships.house_id"],
            name="fk_favorite_membership",
            ondelete="CASCADE",
        ),
        Index("ix_favorite_device", "device_id"),
    )
    user_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    house_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    device_id: Mapped[str] = mapped_column(
        ForeignKey("devices.id", ondelete="CASCADE"), primary_key=True
    )

    # ORM cascades also serve SQLite development connections; PostgreSQL FKs
    # enforce the same cleanup for concurrent/bulk SQL deletes.
    device = relationship(
        "DeviceORM", backref=backref("_favorite_rows", cascade="all, delete-orphan")
    )
    membership = relationship(
        "HouseMembershipORM",
        backref=backref("_favorite_rows", cascade="all, delete-orphan"),
    )
