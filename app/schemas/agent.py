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
