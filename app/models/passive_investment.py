from datetime import datetime, timezone

from sqlalchemy import DateTime, Float, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class PassiveInvestment(Base):
    """Cash accounts, pension funds, and investment funds. There's no live
    pricing here, and no independent way to value a pension/investment fund
    either (no ticker to look one up by) — so cost basis is just the net
    deposit/withdrawal balance from the PassiveTransaction ledger. Market
    value beyond that cost basis is optionally driven by a user-reported
    gain_loss_pct, since there's no live price to derive it from
    automatically. Multiple rows can share the same type, distinguished
    only by name (e.g. two different CASH entries).
    """

    __tablename__ = "passive_investments"
    __table_args__ = (Index("ix_passive_investments_user_id", "user_id"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)

    name: Mapped[str] = mapped_column(String, nullable=False)
    type: Mapped[str] = mapped_column(String, nullable=False)  # CASH | PENSION_FUND | INVESTMENT_FUND | OTHER
    currency: Mapped[str] = mapped_column(String, nullable=False)
    notes: Mapped[str | None] = mapped_column(String, nullable=True)

    # User-reported gain/loss %. null = not yet entered; market value then
    # defaults to cost basis (zero gain).
    gain_loss_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Set only when gain_loss_pct actually changes — not the blanket
    # updated_at below, which also bumps on name/type/currency/notes edits.
    gain_loss_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )

    user: Mapped["User"] = relationship(back_populates="passive_investments")  # noqa: F821
    transactions: Mapped[list["PassiveTransaction"]] = relationship(  # noqa: F821
        back_populates="passive_investment", cascade="all, delete-orphan"
    )
    recurring_deposit: Mapped["PassiveRecurringDeposit | None"] = relationship(  # noqa: F821
        back_populates="passive_investment", uselist=False, cascade="all, delete-orphan"
    )
