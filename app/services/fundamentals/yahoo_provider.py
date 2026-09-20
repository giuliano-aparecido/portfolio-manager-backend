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
  succeeds, has no price/market cap, AND still carries a real `quoteType`
  (an ETF, a physical-gold tracker, a crypto pair) - yfinance can also
  return a `.info` dict with neither, on a transient Yahoo-side hiccup
  with no exception raised, so a missing `quoteType` is treated as that
  instead of a genuine no-fundamentals verdict (see `fetch`). Any other
  transient failure (timeout, 5xx, connection reset, rate limit)
  propagates or is wrapped as an ordinary exception so the cache records
  it as a retryable error rather than a permanent "no fundamentals for a
  day".
"""

import logging
import math
from dataclasses import replace

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


def _usable(value: object) -> bool:
    """None and NaN both mean "not usable" — NaN passes a plain truthiness
    check. Ported from financial-sentiment-api's fundamentals._usable.

    Deliberately looser than `_clean`: the analyst-consensus fetches below
    read raw pandas cells (numpy scalars, occasionally pandas nullables)
    where `_clean`'s strict `isinstance(int|float)` would reject a
    perfectly good numpy.float64. Callers coerce the survivors through
    `_num()` before they reach the payload.
    """
    if value is None:
        return False
    try:
        return not math.isnan(value)  # type: ignore[arg-type]
    except TypeError:
        return True


def _num(value: object) -> float | None:
    """Coerce a usable pandas/numpy cell to a plain float for JSONB —
    a pandas NA / NaT that slipped past _usable would otherwise break
    json.dumps at commit time."""
    if not _usable(value):
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


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
    """Raw `.info` dict when the call succeeded (whatever it carries -
    judging whether it has usable fundamentals is fetch()'s job, done
    only after a resolved-symbol retry has also been given a chance).
    Raises FundamentalsRateLimited on 429 and lets any other fetch error
    propagate as a transient failure.
    """
    try:
        info = yf.Ticker(symbol).info
    except Exception as exc:  # noqa: BLE001
        if _is_rate_limit_error(exc):
            raise FundamentalsRateLimited(str(exc)) from exc
        raise  # transient — cache.py records this as a retryable error
    return info if isinstance(info, dict) else None


def _has_price(info: dict) -> bool:
    return (
        info.get("currentPrice") is not None
        or info.get("regularMarketPrice") is not None
        or info.get("marketCap") is not None
    )


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


def _fetch_growth_consensus(symbol: str) -> dict:
    """Near-term consensus EPS growth from yfinance's earnings_estimate
    table (0y / +1y), an input to the scenario-DCF model. All-None on any
    non-429 failure or missing data — the model has a generic-growth
    fallback for exactly this. Ported from financial-sentiment-api's
    fundamentals._fetch_growth_consensus.
    """
    empty = {"growth_0y": None, "growth_1y": None, "growth_0y_low": None, "growth_0y_high": None}
    try:
        estimate = yf.Ticker(symbol).earnings_estimate
        row_0y = estimate.loc["0y"]
        row_1y = estimate.loc["+1y"]
        year_ago = row_0y["yearAgoEps"]
        growth_0y = row_0y["growth"]
        growth_1y = row_1y["growth"]
        low = row_0y["low"]
        high = row_0y["high"]
        if not _usable(year_ago) or year_ago == 0 or not _usable(growth_0y) or not _usable(growth_1y):
            return empty
        result = {
            "growth_0y": _num(growth_0y),
            "growth_1y": _num(growth_1y),
            "growth_0y_low": None,
            "growth_0y_high": None,
        }
        if _usable(low):
            result["growth_0y_low"] = _num((low - year_ago) / abs(year_ago))
        if _usable(high):
            result["growth_0y_high"] = _num((high - year_ago) / abs(year_ago))
        return result
    except Exception as exc:  # noqa: BLE001
        if _is_rate_limit_error(exc):
            raise FundamentalsRateLimited(str(exc)) from exc
        logger.warning("yfinance growth-estimate fetch failed for %s: %s", symbol, exc)
        return empty


def _fetch_recent_eps_surprise(symbol: str) -> float | None:
    """Actual-vs-consensus EPS surprise (fraction) for the most recently
    reported quarter, from yfinance's earnings_dates table — the DCF model
    uses it to flag a likely one-time item in trailing EPS. None (not a
    failure) when no reported row with a usable estimate exists yet. Ported
    from financial-sentiment-api's fundamentals._fetch_recent_eps_surprise.
    """
    try:
        dates = yf.Ticker(symbol).earnings_dates
        reported = dates.dropna(subset=["Reported EPS"])
        if reported.empty:
            return None
        row = reported.iloc[0]
        estimate = row["EPS Estimate"]
        actual = row["Reported EPS"]
        if not _usable(estimate) or not _usable(actual) or estimate == 0:
            return None
        return _num((actual - estimate) / abs(estimate))
    except Exception as exc:  # noqa: BLE001
        if _is_rate_limit_error(exc):
            raise FundamentalsRateLimited(str(exc)) from exc
        logger.warning("yfinance earnings-surprise fetch failed for %s: %s", symbol, exc)
        return None


class YahooFundamentalsProvider:
    name = "yahoo"

    def fetch(self, yahoo_symbol: str) -> FundamentalsData:
        info = _fetch_info(yahoo_symbol)
        symbol = yahoo_symbol
        if not info or not _has_price(info):
            resolved = resolve_ticker(yahoo_symbol)
            if resolved != yahoo_symbol:
                resolved_info = _fetch_info(resolved)
                if resolved_info and _has_price(resolved_info):
                    info, symbol = resolved_info, resolved
        if not info or not _has_price(info):
            if info and info.get("quoteType"):
                raise FundamentalsUnavailable(f"No Yahoo fundamentals for {yahoo_symbol}")
            raise RuntimeError(f"Degraded/incomplete Yahoo .info response for {yahoo_symbol} (no price, no quoteType)")

        data = _to_data(yahoo_symbol, info)

        # Two extra Yahoo endpoints, only feeding the scenario-DCF model.
        # Skip them entirely for a security with no EPS at all — it can
        # only be valued on the revenue basis, which uses neither an EPS
        # growth consensus nor the trailing-EPS-surprise distortion check,
        # so these calls would be pure rate-limit cost. Best-effort
        # otherwise: a non-429 failure leaves the fields None (the model
        # falls back to a sustainable-growth / generic estimate); a 429
        # propagates so the daily cache retries the whole symbol.
        if info.get("trailingEps") is None and info.get("forwardEps") is None:
            return data
        return replace(
            data,
            **_fetch_growth_consensus(symbol),
            recent_eps_surprise=_fetch_recent_eps_surprise(symbol),
        )
