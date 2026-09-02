"""The provider seam: a typed fundamentals record, the protocol every data
source implements, the errors callers branch on, and the settings-driven
factory.
"""

from dataclasses import dataclass, fields
from typing import Protocol, runtime_checkable

from app.exceptions import AppError
from app.config import get_settings


class FundamentalsRateLimited(Exception):
    """Upstream returned HTTP 429. The cache retries ONLY the symbols that
    raised this, with backoff — never the whole batch (the rest are already
    written for today). See app/services/fundamentals/cache.py.
    """


class FundamentalsUnavailable(Exception):
    """The symbol has no usable fundamentals to fetch at all — an ETF, a
    physical-gold tracker, a crypto pair. Distinct from a transient fetch
    failure (a bare Exception): the cache records "unavailable" for the day
    so it isn't retried on every call, whereas a transient failure is.
    """


@dataclass(frozen=True)
class FundamentalsData:
    """One security's fundamentals, in its own trading/reporting currency —
    never converted to CHF. Multiples are unitless; absolute figures carry
    `currency` / `financial_currency`. Every field is Optional: a provider
    can return a partial record and callers render the gaps as "N/A".

    Field names are snake_case here and serialize 1:1 to the JSONB
    `payload` column; `from_payload` tolerates a payload written by an
    older or newer field set.
    """

    symbol: str
    company_name: str | None = None
    sector: str | None = None
    industry: str | None = None
    currency: str | None = None
    financial_currency: str | None = None

    price: float | None = None
    market_cap: float | None = None

    pe_trailing: float | None = None
    pe_forward: float | None = None
    peg_ratio: float | None = None
    price_to_book: float | None = None
    price_to_sales: float | None = None
    enterprise_to_ebitda: float | None = None

    eps_trailing: float | None = None
    book_value_per_share: float | None = None
    return_on_equity: float | None = None
    operating_margin: float | None = None
    profit_margin: float | None = None
    gross_margin: float | None = None

    revenue_growth: float | None = None
    earnings_growth: float | None = None

    free_cash_flow: float | None = None
    total_revenue: float | None = None
    total_debt: float | None = None
    total_cash: float | None = None
    ebitda: float | None = None
    debt_to_equity: float | None = None
    current_ratio: float | None = None

    dividend_yield: float | None = None
    dividend_rate: float | None = None
    payout_ratio: float | None = None

    year_low: float | None = None
    year_high: float | None = None

    # Near-term analyst-consensus inputs for the scenario-DCF model
    # (app/services/fundamentals/valuation.py). Best-effort — all None when
    # the extra Yahoo endpoints fail; the model falls back to a
    # sustainable-growth / generic estimate.
    growth_0y: float | None = None
    growth_1y: float | None = None
    growth_0y_low: float | None = None
    growth_0y_high: float | None = None
    recent_eps_surprise: float | None = None

    def to_payload(self) -> dict:
        return {f.name: getattr(self, f.name) for f in fields(self)}

    @classmethod
    def from_payload(cls, payload: dict) -> "FundamentalsData":
        known = {f.name for f in fields(cls)}
        kwargs = {k: v for k, v in payload.items() if k in known}
        kwargs.setdefault("symbol", payload.get("symbol", ""))
        return cls(**kwargs)


@runtime_checkable
class FundamentalsProvider(Protocol):
    name: str

    def fetch(self, yahoo_symbol: str) -> FundamentalsData:
        """Return fundamentals for an already-resolved Yahoo symbol.

        Raises FundamentalsRateLimited on HTTP 429, FundamentalsUnavailable
        when the symbol genuinely has no fundamentals, and a bare Exception
        for anything transient.
        """
        ...


def get_fundamentals_provider() -> FundamentalsProvider:
    provider = get_settings().fundamentals_provider
    if provider == "yahoo":
        from app.services.fundamentals.yahoo_provider import YahooFundamentalsProvider

        return YahooFundamentalsProvider()
    raise AppError(500, f"Unknown FUNDAMENTALS_PROVIDER: {provider!r}")
