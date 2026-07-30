from datetime import datetime, timezone

from sqlalchemy import DateTime, Float, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class PassiveTransaction(Base):
    """Deposit/withdrawal ledger for a PassiveInvestment. Uses a real FK with
    ON DELETE CASCADE — deleting the investment deletes its ledger with no
    manual cascade needed.
    """

    __tablename__ = "passive_transactions"
    __table_args__ = (Index("ix_passive_transactions_investment_date", "passive_investment_id", "date"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    passive_investment_id: Mapped[int] = mapped_column(
        ForeignKey("passive_investments.id", ondelete="CASCADE"), nullable=False
    )

    type: Mapped[str] = mapped_column(String, nullable=False)  # "DEPOSIT" | "WITHDRAWAL"
    date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Native currency, always stored positive — type gives the sign.
    amount_native: Mapped[float] = mapped_column(Float, nullable=False)
    notes: Mapped[str | None] = mapped_column(String, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)

    passive_investment: Mapped["PassiveInvestment"] = relationship(back_populates="transactions")  # noqa: F821
