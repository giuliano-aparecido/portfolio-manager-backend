from datetime import datetime, timezone

from sqlalchemy import DateTime, Float, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class PassiveRecurringDeposit(Base):
    """One optional recurring deposit rule per PassiveInvestment (strict
    1:1 — passive_investment_id is unique). All schedule fields are fully
    editable — catch-up always filters candidate occurrences against the
    actual last_generated_date timestamp, never a recomputed occurrence
    index, so it can't misfire after a schedule edit. Deleting the rule
    never touches already-generated PassiveTransaction rows: there is
    deliberately no FK from PassiveTransaction to this model, only to
    PassiveInvestment.
    """

    __tablename__ = "passive_recurring_deposits"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    passive_investment_id: Mapped[int] = mapped_column(
        ForeignKey("passive_investments.id", ondelete="CASCADE"), unique=True, nullable=False
    )

    amount_native: Mapped[float] = mapped_column(Float, nullable=False)
    start_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    frequency: Mapped[str] = mapped_column(String, nullable=False)  # "WEEKLY" | "MONTHLY" | "YEARLY"
    # No occurrences generated after this date (inclusive).
    end_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    notes: Mapped[str | None] = mapped_column(String, nullable=True)

    # Date of the most recently materialized occurrence (inclusive). None
    # means nothing generated yet — the first due occurrence is start_date
    # itself. Never reset when the schedule is edited — it only ever moves
    # forward.
    last_generated_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )

    passive_investment: Mapped["PassiveInvestment"] = relationship(back_populates="recurring_deposit")  # noqa: F821
