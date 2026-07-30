from datetime import datetime, timezone

from sqlalchemy import DateTime, Float, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class PortfolioTransaction(Base):
    """Distinct fields per transaction type (rather than one generic 'shares'
    field) is the whole point — prevents dividend cash from being mistaken
    for a share quantity.
    """

    __tablename__ = "portfolio_transactions"
    __table_args__ = (Index("ix_portfolio_transactions_user_ticker_date", "user_id", "ticker", "date"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)

    ticker: Mapped[str] = mapped_column(String, nullable=False)
    date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    type: Mapped[str] = mapped_column(String, nullable=False)  # "BUY" | "SELL" | "DIVIDEND" | "DRIP"
    native_currency: Mapped[str] = mapped_column(String, nullable=False)

    # BUY / SELL / DRIP only
    quantity: Mapped[float | None] = mapped_column(Float, nullable=True)
    price_per_share: Mapped[float | None] = mapped_column(Float, nullable=True)  # native currency

    # DIVIDEND only
    cash_amount: Mapped[float | None] = mapped_column(Float, nullable=True)  # native currency

    # Captured at transaction time, never recomputed from a live rate later.
    fx_rate_to_chf: Mapped[float] = mapped_column(Float, nullable=False)

    notes: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)

    user: Mapped["User"] = relationship(back_populates="portfolio_transactions")  # noqa: F821
