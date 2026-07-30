"""Live price/FX fetching via yfinance — ported from lib/portfolio/price-service.ts
(originally yahoo-finance2). The unofficial Yahoo Finance data source is the
same across both ecosystems; only the client library differs.

Two porting risks worth knowing about:
- `Ticker.fast_info` key names have shifted across yfinance releases before
  and may again — if fetches start failing, check `Ticker(...).fast_info.keys()`
  against what's used here.
- `Ticker.history()` returns a timezone-aware pandas index, but NOT
  necessarily in UTC (it uses the exchange's local timezone) — comparisons
  below rely on Python correctly comparing aware datetimes across timezones,
  which works, but every `date` passed in here must be tz-aware (never
  naive) or the comparison raises.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import yfinance as yf


@dataclass
class PriceQuote:
    price: float  # major units (e.g. GBP not GBX)
    currency: str  # normalized (GBp -> GBP)
    timestamp: datetime
    source: str  # "yahoo"
    daily_change_percent: float
    daily_change: float


def fetch_current_price(yahoo_ticker: str) -> PriceQuote:
    ticker = yf.Ticker(yahoo_ticker)
    fast = ticker.fast_info

    raw_price = fast.get("lastPrice")
    raw_currency = fast.get("currency")
    previous_close = fast.get("previousClose") or fast.get("regularMarketPreviousClose")

    if not raw_price or raw_price <= 0:
        # Defensive fallback — fast_info has occasionally been missing a
        # field yfinance normally provides; try the last close from history
        # before giving up entirely.
        hist = ticker.history(period="5d", interval="1d")
        if hist.empty:
            raise RuntimeError(f"No valid price from Yahoo Finance for {yahoo_ticker}")
        raw_price = float(hist["Close"].iloc[-1])
        previous_close = float(hist["Close"].iloc[-2]) if len(hist) > 1 else raw_price

    daily_change = (raw_price - previous_close) if previous_close else 0.0
    daily_change_percent = (daily_change / previous_close * 100) if previous_close else 0.0
    timestamp = datetime.now(timezone.utc)  # always "now" (fetch time), never from the Yahoo response

    # Yahoo's explicit signal for pence (lowercase p) — chosen over any
    # price-magnitude heuristic since a 45p stock would defeat a ">100" guess.
    if raw_currency == "GBp":
        return PriceQuote(
            price=raw_price / 100,
            currency="GBP",
            timestamp=timestamp,
            source="yahoo",
            daily_change_percent=daily_change_percent,
            daily_change=daily_change / 100,
        )
    return PriceQuote(
        price=raw_price,
        currency=raw_currency,
        timestamp=timestamp,
        source="yahoo",
        daily_change_percent=daily_change_percent,
        daily_change=daily_change,
    )


def fetch_fx_rate_to_chf(native_currency: str) -> float:
    if native_currency == "CHF":
        return 1.0
    ticker = yf.Ticker(f"{native_currency}CHF=X")
    rate = ticker.fast_info.get("lastPrice")
    if not rate or rate <= 0:
        raise RuntimeError(f"No valid FX rate from Yahoo Finance for {native_currency}CHF=X")
    return rate


def fetch_historical_fx_rate(native_currency: str, date: datetime) -> float:
    """Used when entering a transaction dated in the past, so fx_rate_to_chf
    reflects the rate at that time rather than today's.
    """
    if native_currency == "CHF":
        return 1.0
    ticker = yf.Ticker(f"{native_currency}CHF=X")
    # FX doesn't trade every calendar day (weekends/holidays) — look back up
    # to 7 days for the closest prior trading day.
    period1 = date - timedelta(days=7)
    period2 = date + timedelta(days=1)
    hist = ticker.history(start=period1, end=period2, interval="1d")
    filtered = hist[hist.index <= date]
    if filtered.empty:
        raise RuntimeError(f"No historical FX rate found for {native_currency}CHF=X near {date.date().isoformat()}")
    return float(filtered["Close"].iloc[-1])
