"""Live price/FX fetching via yfinance — an unofficial Yahoo Finance data
source, no API key needed.

Two risks worth knowing about:
- `Ticker.fast_info` key names have shifted across yfinance releases before
  and may again — if fetches start failing, check `Ticker(...).fast_info.keys()`
  against what's used here.
- `Ticker.history()` returns a timezone-aware pandas index, but NOT
  necessarily in UTC (it uses the exchange's local timezone) — comparisons
  below rely on Python correctly comparing aware datetimes across timezones,
  which works, but every `date` passed in here must be tz-aware (never
  naive) or the comparison raises.

`fetch_current_price`/`fetch_fx_rate_to_chf` are wrapped in an in-memory
TTL cache. This isn't about staleness tolerance so much as concurrency: a
portfolio rollup fans out one yfinance call per open ticker (plus one per
distinct currency) via a thread pool, and under CPU-limited hosting
(observed: Render's free tier) that fan-out doesn't achieve real
parallelism — total latency degrades toward the *sum* of each call's
latency rather than the max. The cache doesn't fix that underlying
concurrency ceiling, but it does mean repeated page loads/navigation
within the TTL window reuse already-fetched quotes instead of repeating
the full fan-out.

The TTL (180s) is deliberately aligned with the frontend's shortest
auto-refresh option (3 min) — short enough that an auto-refresh cycle
always lands on an expired entry and gets genuinely live data, long
enough that navigating between pages (which independently call
/portfolio-rollup and /passive-rollup) reuses one fetch instead of
repeating it per page. The frontend's explicit "Refresh" button passes
`force_refresh=True` to bypass the cache entirely — clicking refresh
must never silently return stale data.
"""

import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import yfinance as yf

_CACHE_TTL_SECONDS = 180.0
_cache_lock = threading.Lock()
_price_cache: dict[str, tuple[float, "PriceQuote"]] = {}
_fx_cache: dict[str, tuple[float, float]] = {}


def clear_quote_cache() -> None:
    """Test-only: the module-level cache otherwise persists across test
    cases within the same pytest process, which would leak a mocked quote
    from one test into another test reusing the same ticker/currency."""
    with _cache_lock:
        _price_cache.clear()
        _fx_cache.clear()


@dataclass
class PriceQuote:
    price: float  # major units (e.g. GBP not GBX)
    currency: str  # normalized (GBp -> GBP)
    timestamp: datetime
    source: str  # "yahoo"
    daily_change_percent: float
    daily_change: float


def fetch_current_price(yahoo_ticker: str, *, force_refresh: bool = False) -> PriceQuote:
    now = time.monotonic()
    if not force_refresh:
        with _cache_lock:
            cached = _price_cache.get(yahoo_ticker)
            if cached is not None and now - cached[0] < _CACHE_TTL_SECONDS:
                return cached[1]

    quote = _fetch_current_price_uncached(yahoo_ticker)

    with _cache_lock:
        _price_cache[yahoo_ticker] = (now, quote)
    return quote


def _fetch_current_price_uncached(yahoo_ticker: str) -> PriceQuote:
    ticker = yf.Ticker(yahoo_ticker)
    fast = ticker.fast_info

    raw_price = fast.get("lastPrice")
    raw_currency = fast.get("currency")
    # fast_info["previousClose"] is unreliable — verified against .get_info()
    # (Yahoo's own regularMarketChangePercent) and historical closes across
    # several tickers, it's sometimes off by a small amount and was observed
    # wildly wrong for AMZN on a >15% single-day move (257.95 vs the real
    # 235.5 prior close, understating the day's gain by two-thirds).
    # regularMarketPreviousClose matched the true previous close every time
    # it was checked — prefer it, falling back to previousClose only if it's
    # ever missing.
    previous_close = fast.get("regularMarketPreviousClose") or fast.get("previousClose")

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


def fetch_fx_rate_to_chf(native_currency: str, *, force_refresh: bool = False) -> float:
    if native_currency == "CHF":
        return 1.0

    now = time.monotonic()
    if not force_refresh:
        with _cache_lock:
            cached = _fx_cache.get(native_currency)
            if cached is not None and now - cached[0] < _CACHE_TTL_SECONDS:
                return cached[1]

    ticker = yf.Ticker(f"{native_currency}CHF=X")
    rate = ticker.fast_info.get("lastPrice")
    if not rate or rate <= 0:
        raise RuntimeError(f"No valid FX rate from Yahoo Finance for {native_currency}CHF=X")

    with _cache_lock:
        _fx_cache[native_currency] = (now, rate)
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
