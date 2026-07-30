from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class TickerMetadata(Base):
    """Ticker-level facts that don't vary per transaction: market (for
    resolving a live-price symbol), category (for grouping/display), and the
    currency new transactions for this ticker should be entered in.
    """

    __tablename__ = "ticker_metadata"
    __table_args__ = (UniqueConstraint("user_id", "ticker", name="uq_ticker_metadata_user_id_ticker"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)

    ticker: Mapped[str] = mapped_column(String, nullable=False)
    market: Mapped[str] = mapped_column(String, nullable=False)
    category: Mapped[str] = mapped_column(String, nullable=False)
    native_currency: Mapped[str] = mapped_column(String, nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )

    user: Mapped["User"] = relationship(back_populates="ticker_metadata")  # noqa: F821
