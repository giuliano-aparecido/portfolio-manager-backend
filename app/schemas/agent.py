from typing import Literal

from app.schemas.common import CamelModel
from app.schemas.portfolio import TickerPriceError

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
