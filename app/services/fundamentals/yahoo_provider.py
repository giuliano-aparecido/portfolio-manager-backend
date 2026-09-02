"""Yahoo Finance fundamentals via yfinance's `.info` dict — one network
call per symbol, no API key.

Adapted from financial-sentiment-api's app/services/fundamentals.py
(`fetch_fundamentals` / `resolve_ticker`). Differences here:

- The caller passes an already-resolved Yahoo symbol (this app derives it
  deterministically from the stored `market` via
  app/services/ticker_config.py:derive_yahoo_ticker), so `resolve_ticker`
  is only a last-ditch fallback, not the common path.
- HTTP 429 is surfaced as FundamentalsRateLimited so the cache can retry
  just the throttled symbols.
- FundamentalsUnavailable is raised ONLY when the `.info` call itself
  succeeds but genuinely carries no fundamentals (an ETF, a physical-gold
  tracker, a crypto pair). A transient failure (timeout, 5xx, connection
  reset) propagates as an ordinary exception so the cache records it as a
  retryable error rather than a permanent "no fundamentals for a day".
"""

import logging
import math

import yfinance as yf

from app.services.fundamentals.base import (
    FundamentalsData,
    FundamentalsRateLimited,
    FundamentalsUnavailable,
)

logger = logging.getLogger(__name__)

_RATE_LIMIT_MARKERS = ("429", "too many requests", "rate limit", "rate-limit")


def _is_rate_limit_error(exc: Exception) -> bool:
    if type(exc).__name__ == "YFRateLimitError":
        return True
    text = str(exc).lower()
    return any(marker in text for marker in _RATE_LIMIT_MARKERS)


def _clean(value: object) -> float | None:
    """Keep only usable numbers — yfinance's `.info` occasionally carries a
    NaN, a bool, or a literal "N/A" string where a float is expected, and
    those must not reach the screen's numeric comparisons."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return None if math.isnan(value) else float(value)


def resolve_ticker(ticker: str) -> str:
    """Bare symbol -> the symbol Yahoo actually recognizes (e.g. "NESN" ->
    "NESN.SW"), via yf.Search's top EQUITY match. Returns the original
    unchanged on any non-rate-limit failure. Ported from
    financial-sentiment-api; only reached here when a direct `.info` fetch
    has already come back empty.
    """
    try:
        matches = yf.Search(ticker).quotes
    except Exception as exc:  # noqa: BLE001
        if _is_rate_limit_error(exc):
            raise FundamentalsRateLimited(str(exc)) from exc
        logger.warning("yfinance ticker search failed for %r: %s", ticker, exc)
        return ticker
    for match in matches:
        if match.get("quoteType") == "EQUITY" and match.get("symbol"):
            return match["symbol"]
    return ticker


def _fetch_info(symbol: str) -> dict | None:
    """`.info` when the call SUCCEEDED and carried a price or a market cap.
    None when the call succeeded but the payload is empty (a genuine
    "no fundamentals" signal). Raises FundamentalsRateLimited on 429 and
    lets any other fetch error propagate as a transient failure.
    """
    try:
        info = yf.Ticker(symbol).info
    except Exception as exc:  # noqa: BLE001
        if _is_rate_limit_error(exc):
            raise FundamentalsRateLimited(str(exc)) from exc
        raise  # transient — cache.py records this as a retryable error
    if not isinstance(info, dict):
        return None
    price = info.get("currentPrice") or info.get("regularMarketPrice")
    market_cap = info.get("marketCap")
    return info if (price is not None or market_cap is not None) else None


def _to_data(symbol: str, info: dict) -> FundamentalsData:
    return FundamentalsData(
        symbol=symbol,
        company_name=info.get("shortName") or info.get("longName"),
        sector=info.get("sector"),
        industry=info.get("industry"),
        currency=info.get("currency"),
        financial_currency=info.get("financialCurrency"),
        price=_clean(info.get("currentPrice") or info.get("regularMarketPrice")),
        market_cap=_clean(info.get("marketCap")),
        pe_trailing=_clean(info.get("trailingPE")),
        pe_forward=_clean(info.get("forwardPE")),
        peg_ratio=_clean(info.get("trailingPegRatio")),
        price_to_book=_clean(info.get("priceToBook")),
        price_to_sales=_clean(info.get("priceToSalesTrailing12Months")),
        enterprise_to_ebitda=_clean(info.get("enterpriseToEbitda")),
        eps_trailing=_clean(info.get("trailingEps")),
        book_value_per_share=_clean(info.get("bookValue")),
        return_on_equity=_clean(info.get("returnOnEquity")),
        operating_margin=_clean(info.get("operatingMargins")),
        profit_margin=_clean(info.get("profitMargins")),
        gross_margin=_clean(info.get("grossMargins")),
        revenue_growth=_clean(info.get("revenueGrowth")),
        earnings_growth=_clean(info.get("earningsGrowth")),
        free_cash_flow=_clean(info.get("freeCashflow")),
        total_revenue=_clean(info.get("totalRevenue")),
        total_debt=_clean(info.get("totalDebt")),
        total_cash=_clean(info.get("totalCash")),
        ebitda=_clean(info.get("ebitda")),
        debt_to_equity=_clean(info.get("debtToEquity")),
        current_ratio=_clean(info.get("currentRatio")),
        dividend_yield=_clean(info.get("dividendYield")),
        dividend_rate=_clean(info.get("dividendRate") or info.get("trailingAnnualDividendRate")),
        payout_ratio=_clean(info.get("payoutRatio")),
        year_low=_clean(info.get("fiftyTwoWeekLow")),
        year_high=_clean(info.get("fiftyTwoWeekHigh")),
    )


class YahooFundamentalsProvider:
    name = "yahoo"

    def fetch(self, yahoo_symbol: str) -> FundamentalsData:
        info = _fetch_info(yahoo_symbol)
        if info is None:
            resolved = resolve_ticker(yahoo_symbol)
            if resolved != yahoo_symbol:
                info = _fetch_info(resolved)
        if info is None:
            raise FundamentalsUnavailable(f"No Yahoo fundamentals for {yahoo_symbol}")
        return _to_data(yahoo_symbol, info)
