from datetime import datetime

from app.schemas.common import CamelModel

# --- Ticker (TickerMetadata) ---------------------------------------------


class TickerCreateRequest(CamelModel):
    """Fields are deliberately loose/optional here — validation and exact
    error messages are handled manually in the router to match the
    original app's contract precisely, not FastAPI's default 422 shape.
    """

    ticker: str | None = None
    market: str | None = None
    category: str | None = None
    native_currency: str | None = None


class TickerUpdateRequest(CamelModel):
    market: str | None = None
    category: str | None = None
    native_currency: str | None = None


class TickerOut(CamelModel):
    id: int
    user_id: str
    ticker: str
    market: str
    category: str
    native_currency: str
    created_at: datetime
    updated_at: datetime


# --- PortfolioTransaction ---------------------------------------------------


class TransactionCreateRequest(CamelModel):
    ticker: str | None = None
    type: str | None = None
    date: str | None = None
    notes: str | None = None
    quantity: float | None = None
    price_per_share: float | None = None
    cash_amount: float | None = None


class TransactionUpdateRequest(CamelModel):
    type: str | None = None
    date: str | None = None
    notes: str | None = None
    quantity: float | None = None
    price_per_share: float | None = None
    cash_amount: float | None = None


class TransactionOut(CamelModel):
    id: int
    user_id: str
    ticker: str
    date: datetime
    type: str
    native_currency: str
    quantity: float | None
    price_per_share: float | None
    cash_amount: float | None
    fx_rate_to_chf: float
    notes: str | None
    created_at: datetime


# --- Ticker detail -----------------------------------------------------


class TransactionRow(CamelModel):
    id: int
    date: datetime
    type: str
    native_currency: str
    quantity: float | None
    price_per_share: float | None
    cash_amount: float | None
    fx_rate_to_chf: float
    notes: str | None


class RealizedGainRow(CamelModel):
    date: datetime
    qty_sold: float
    proceeds_native: float
    cost_basis_native: float
    gain_native: float
    gain_chf: float


class TickerDetail(CamelModel):
    ticker: str
    market: str
    category: str
    native_currency: str
    current_shares: float
    cost_basis_native: float
    cost_basis_chf: float
    average_cost_per_share_native: float
    current_price_native: float | None = None
    current_fx_rate_to_chf: float | None = None
    market_value_native: float | None = None
    market_value_chf: float | None = None
    unrealized_gain_native: float | None = None
    unrealized_gain_chf: float | None = None
    price_timestamp: datetime | None = None
    price_source: str | None = None
    price_error: str | None = None
    dividends_native: float
    dividends_chf: float
    total_realized_gain_native: float
    total_realized_gain_chf: float
    realized_gains: list[RealizedGainRow]
    transactions: list[TransactionRow]


# --- Portfolio rollup -----------------------------------------------------


class OpenTickerRollup(CamelModel):
    ticker: str
    category: str
    native_currency: str
    current_shares: float
    cost_basis_native: float
    cost_basis_chf: float
    current_price_native: float
    current_fx_rate_to_chf: float
    market_value_native: float
    market_value_chf: float
    unrealized_gain_native: float
    unrealized_gain_chf: float
    dividends_chf: float
    price_timestamp: datetime
    price_source: str
    daily_change_percent: float
    daily_change: float


class ClosedTickerRollup(CamelModel):
    ticker: str
    dividends_chf: float
    realized_gain_chf: float


class TickerPriceError(CamelModel):
    ticker: str
    error: str


class PortfolioRollup(CamelModel):
    open_tickers: list[OpenTickerRollup]
    closed_tickers: list[ClosedTickerRollup]
    price_errors: list[TickerPriceError]
    total_cost_basis_chf: float
    total_market_value_chf: float
    total_unrealized_gain_chf: float
    total_dividends_chf: float
    total_realized_gain_chf: float
