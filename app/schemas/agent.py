from typing import Literal

from app.schemas.common import CamelModel
from app.schemas.portfolio import TickerPriceError

# One definition of the news status values, shared with the service that
# produces them (app/services/news.py imports this). Defined here rather
# than there so the dependency points the way every other module in this
# app already points — services import schemas, not the reverse.
# "ok" — items present. "no_news" — every window came back empty or
# all-junk, an honest answer rather than a failure. "error" — the fetch
# itself failed.
NewsStatus = Literal["ok", "no_news", "error"]

# --- Allocation -------------------------------------------------------------


class CategoryAllocationRow(CamelModel):
    category: str
    market_value_chf: float
    percent_of_portfolio: float


class CurrencyAllocationRow(CamelModel):
    currency: str
    market_value_chf: float
    percent_of_portfolio: float


class AllocationBreakdown(CamelModel):
    total_market_value_chf: float
    by_category: list[CategoryAllocationRow]
    by_currency: list[CurrencyAllocationRow]
    price_errors: list[TickerPriceError]


# --- What-if simulator --------------------------------------------------


class WhatIfImpact(CamelModel):
    ticker: str
    action: Literal["BUY", "SELL"]
    shares_before: float
    shares_after: float
    cost_basis_chf_before: float
    cost_basis_chf_after: float
    market_value_chf_before: float
    market_value_chf_after: float
    realized_gain_chf: float
    portfolio_value_chf_before: float
    portfolio_value_chf_after: float
    ticker_allocation_percent_before: float
    ticker_allocation_percent_after: float
    error: str | None = None


# --- Agent chat ---------------------------------------------------------


class ChatMessageIn(CamelModel):
    role: Literal["user", "assistant"]
    content: str


class AskRequest(CamelModel):
    # Full running conversation, resent by the frontend every turn — nothing
    # is persisted server-side (see PROJECT.md's agent-chat section).
    messages: list[ChatMessageIn]


class TickerNotFound(CamelModel):
    found: bool = False
    message: str


# --- Fundamentals (value-investing tools) ------------------------------


class FundamentalsMetric(CamelModel):
    metric: str
    value: float | None
    verdict: Literal["good", "fair", "poor", "n/a"]
    note: str


class IntrinsicValue(CamelModel):
    """Scenario-DCF intrinsic value for one security, in its own trading
    currency. `available` is False when the model can't produce a number
    (no price, or the classified basis has no usable input) — `reason`
    then carries the model's own wording. `gapPercent` is positive when
    the market price is above intrinsic (overvalued); `marginOfSafetyPercent`
    is its inverse (positive = buying below intrinsic).
    """

    available: bool
    reason: str | None = None
    currency: str | None = None
    current_price: float | None = None
    intrinsic_value: float | None = None
    gap_percent: float | None = None
    margin_of_safety_percent: float | None = None
    verdict: str | None = None  # "undervalued" | "overvalued" | "near fair value"
    valuation_basis: str | None = None  # "EPS-based" | "FCF-based" | "Dividend-based" | "Revenue-based"
    assessment: str | None = None  # the model's rendered text block, verbatim


class SecurityIntrinsicValue(CamelModel):
    """get_intrinsic_value's response wrapper. `status` mirrors
    SecurityFundamentals: "unavailable" for an ETF/gold/crypto with no
    fundamentals to value at all."""

    ticker: str
    yahoo_symbol: str
    status: Literal["ok", "unavailable", "error"]
    message: str | None = None
    as_of_date: str | None = None
    stale: bool = False
    valuation: IntrinsicValue | None = None


class SecurityFundamentals(CamelModel):
    """One holding's fundamentals, in its own trading/reporting currency —
    never CHF. `status` is "ok" (data present), "unavailable" (an ETF /
    gold tracker / crypto — no fundamentals exist to fetch), or "error" (a
    fetch failure; any `value`s shown are a stale last-good snapshot).
    """

    ticker: str
    yahoo_symbol: str
    status: Literal["ok", "unavailable", "error"]
    message: str | None = None
    as_of_date: str | None = None
    stale: bool = False

    company_name: str | None = None
    sector: str | None = None
    industry: str | None = None
    currency: str | None = None

    price: float | None = None
    market_cap: float | None = None
    pe_trailing: float | None = None
    pe_forward: float | None = None
    peg_ratio: float | None = None
    price_to_book: float | None = None
    price_to_sales: float | None = None
    enterprise_to_ebitda: float | None = None
    return_on_equity: float | None = None
    operating_margin: float | None = None
    profit_margin: float | None = None
    revenue_growth: float | None = None
    earnings_growth: float | None = None
    fcf_yield: float | None = None
    debt_to_equity: float | None = None
    current_ratio: float | None = None
    dividend_yield: float | None = None
    payout_ratio: float | None = None

    screen_overall: str | None = None
    screen_good_count: int | None = None
    screen_poor_count: int | None = None
    metrics: list[FundamentalsMetric] = []

    # Scenario-DCF intrinsic value / margin of safety — omitted (None) for
    # ETFs, gold and crypto, or when the model can't value the security.
    valuation: IntrinsicValue | None = None


class HoldingFundamentals(SecurityFundamentals):
    weight_percent: float


class WeightedAggregates(CamelModel):
    """Value-weighted across holdings with `status == "ok"` and the metric
    present. `covered_percent` is the share of total market value those
    holdings represent — read every aggregate in that light.
    """

    covered_percent: float
    weighted_pe_trailing: float | None = None
    weighted_price_to_book: float | None = None
    weighted_dividend_yield: float | None = None
    weighted_fcf_yield: float | None = None


class PortfolioFundamentals(CamelModel):
    as_of: str
    provider: str
    total_market_value_chf: float
    holdings: list[HoldingFundamentals]
    weighted_aggregates: WeightedAggregates
    uncovered_tickers: list[str]
    price_errors: list[TickerPriceError]
    notes: list[str]


# --- Ticker news --------------------------------------------------------


class NewsItem(CamelModel):
    """One headline. `link` is Google News' redirect URL for the article,
    not the publisher's canonical URL — good enough to open, not something
    to scrape."""

    title: str
    publisher: str
    published_date: str | None = None
    link: str


class TickerNews(CamelModel):
    """Recent headlines for one holding. `status` is "ok" (items present),
    "no_news" (nothing meaningful found even in the widest window — an
    honest answer, not a failure) or "error" (the news feed couldn't be
    reached).

    `window_days` is how far back the returned items actually came from:
    the search starts at a week and widens only when nothing meaningful
    turns up, so a large value means the news is old, not that more of it
    was wanted. It's None when there are no items.
    """

    ticker: str
    company_name: str | None = None
    sector: str | None = None
    window_days: int | None = None
    status: NewsStatus
    message: str | None = None
    items: list[NewsItem] = []
