import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    """Multi-user support and data isolation.

    id is an opaque string (uuid4 for new rows) rather than an autoincrement
    int, since some existing rows use a different opaque string ID format
    and are treated identically — nothing about this model assumes any
    particular string format.
    """

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    email: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    name: Mapped[str | None] = mapped_column(String, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )

    portfolio_transactions: Mapped[list["PortfolioTransaction"]] = relationship(  # noqa: F821
        back_populates="user", cascade="all, delete-orphan"
    )
    ticker_metadata: Mapped[list["TickerMetadata"]] = relationship(  # noqa: F821
        back_populates="user", cascade="all, delete-orphan"
    )
    passive_investments: Mapped[list["PassiveInvestment"]] = relationship(  # noqa: F821
        back_populates="user", cascade="all, delete-orphan"
    )
