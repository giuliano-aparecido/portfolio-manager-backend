from datetime import date, datetime, timezone

from sqlalchemy import Boolean, Date, DateTime, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class TickerFundamentalsCache(Base):
    """One row of cached company fundamentals per (provider, yahoo_symbol).

    Deliberately NOT scoped per user — fundamentals are a property of the
    security, not the holder, so two users who both hold NESN.SW share one
    row and one upstream fetch. Populated lazily from the agent tools (no
    cron); refreshed at most once per UTC day per symbol. See
    app/services/fundamentals/cache.py for the read/refresh/GC logic.
    """

    __tablename__ = "ticker_fundamentals_cache"
    __table_args__ = (
        UniqueConstraint("provider", "yahoo_symbol", name="uq_ticker_fundamentals_cache_provider_yahoo_symbol"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    provider: Mapped[str] = mapped_column(String, nullable=False)
    yahoo_symbol: Mapped[str] = mapped_column(String, nullable=False)

    # Serialized FundamentalsData (see base.py). NULL when the symbol has
    # no usable fundamentals at all (ETF / physical-gold tracker / crypto)
    # and `unavailable` is set, or when the very first fetch has never
    # succeeded and only `fetch_error` is populated.
    payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    unavailable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # Shape version of `payload` — bumped when FundamentalsData gains a
    # field the screen / valuation models read (see cache.CACHE_PAYLOAD_VERSION).
    # A row whose version is behind the current one is treated as needing a
    # refetch even inside its once-per-day window, so a deploy that adds a
    # model input doesn't silently serve degraded values off old rows.
    # NULL = written before this column existed (implicitly the oldest shape).
    payload_version: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # The UTC calendar date `payload` was fetched for — the once-per-day
    # gate. NULL until a fetch (successful or "unavailable") has happened.
    as_of_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Last attempt's error, e.g. "rate_limited". Set without bumping
    # `as_of_date`, so the symbol is retried on the next call while any
    # previously-good `payload` keeps being served (flagged stale).
    fetch_error: Mapped[str | None] = mapped_column(String, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )
