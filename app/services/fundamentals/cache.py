"""get_fundamentals(): the only entry point the agent tools use.

Guarantees:
- At most one upstream request per (provider, symbol) per UTC day. After
  the first call of the day everything is served from Postgres.
- A concurrent caller blocks on a Postgres advisory lock rather than
  stampeding the provider, then re-reads the row the first caller wrote.
- HTTP 429 retries ONLY the throttled symbols, with backoff; the symbols
  that already succeeded this run are committed and never refetched.
- Rows for tickers no longer held by ANY user are swept whenever the
  refresh path runs (no cron — same lazy model as
  app/services/recurring.py).

There is no staleness-tolerance window: `as_of_date` is a calendar date,
so a fetch at 23:00 UTC and another at 01:00 UTC are different days. A
small in-process TTL cache sits in front purely to avoid a re-SELECT on
repeated tool calls within one request.
"""

import logging
import threading
import time
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime, timezone

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models import TickerFundamentalsCache, TickerMetadata
from app.services.fundamentals.base import (
    FundamentalsData,
    FundamentalsProvider,
    FundamentalsRateLimited,
    FundamentalsUnavailable,
    get_fundamentals_provider,
)
from app.services.ticker_config import derive_yahoo_ticker

logger = logging.getLogger(__name__)

# Backoff (seconds) between retries of the 429'd subset. Deliberately
# short and shallow: the agent tool that triggers this awaits the result
# inline on the event loop, so a long sleep here would stall other
# in-flight /agent/ask streams. A symbol still throttled after this is
# marked `rate_limited` and retried on the next call / next day.
_RETRY_BACKOFF: tuple[float, ...] = (0.5, 1.5)

# Shape version of the JSONB `payload`. Bump this whenever FundamentalsData
# gains (or renames) a field the screen / valuation models actually read,
# so rows written by an older deploy are refetched instead of silently
# feeding those models a None where real data now exists.
#   1 — initial fundamentals feature (no analyst-consensus inputs)
#   2 — added growth_0y/1y/low/high + recent_eps_surprise for the DCF model
CACHE_PAYLOAD_VERSION = 2

_MEM_TTL_SECONDS = 120.0
_MEM_MAX_ENTRIES = 512
_mem_lock = threading.Lock()
_mem: dict[tuple[str, str], tuple[float, "FundamentalsEntry"]] = {}

_MAX_FETCH_WORKERS = 8


def clear_fundamentals_cache() -> None:
    """Test-only: drop the in-process TTL layer so a mocked provider result
    from one test doesn't leak into another (mirrors
    price_service.clear_quote_cache)."""
    with _mem_lock:
        _mem.clear()


@dataclass(frozen=True)
class FundamentalsEntry:
    symbol: str
    data: FundamentalsData | None
    as_of_date: date | None
    stale: bool  # a payload is being served but it's older than today
    unavailable: bool  # the symbol genuinely has no fundamentals (ETF/gold/crypto)
    error: str | None


@dataclass(frozen=True)
class _Fetched:
    kind: str  # "ok" | "unavailable" | "error"
    data: FundamentalsData | None = None
    message: str | None = None


def _resolve_today(today: date | None) -> date:
    return today or datetime.now(timezone.utc).date()


def _mem_get(provider_name: str, symbol: str, today: date) -> "FundamentalsEntry | None":
    with _mem_lock:
        hit = _mem.get((provider_name, symbol))
    # Also gated on the UTC day, not just the wall-clock TTL: an entry
    # cached at 23:59 must not still be served after the day rolls over
    # (every memoized entry carries today's as_of_date at write time).
    if hit is not None and time.monotonic() - hit[0] < _MEM_TTL_SECONDS and hit[1].as_of_date == today:
        return hit[1]
    return None


def _mem_put(provider_name: str, entry: "FundamentalsEntry") -> None:
    with _mem_lock:
        if len(_mem) >= _MEM_MAX_ENTRIES:
            cutoff = time.monotonic() - _MEM_TTL_SECONDS
            for key in [k for k, (ts, _) in _mem.items() if ts < cutoff]:
                del _mem[key]
        _mem[(provider_name, entry.symbol)] = (time.monotonic(), entry)


def _load_rows(db: Session, provider_name: str, symbols: Iterable[str]) -> dict[str, TickerFundamentalsCache]:
    symbols = list(symbols)
    if not symbols:
        return {}
    rows = (
        db.query(TickerFundamentalsCache)
        .filter(
            TickerFundamentalsCache.provider == provider_name,
            TickerFundamentalsCache.yahoo_symbol.in_(symbols),
        )
        .all()
    )
    return {r.yahoo_symbol: r for r in rows}


def _stale_shape(row: TickerFundamentalsCache) -> bool:
    """A payload written before the current CACHE_PAYLOAD_VERSION is missing
    fields the models now read — refetch it. `unavailable` rows carry no
    payload and don't depend on its shape, so they're exempt; a future
    CACHE_PAYLOAD_VERSION bump that adds a field relevant to a
    currently-unavailable security would need to drop this exemption."""
    return not row.unavailable and (row.payload_version or 0) < CACHE_PAYLOAD_VERSION


def _fresh_entry(symbol: str, row: TickerFundamentalsCache | None, today: date) -> FundamentalsEntry | None:
    """An entry only if `row` already holds today's data, at the current
    payload shape, with no pending error; otherwise None, meaning
    "attempt a fetch"."""
    if row is None or row.as_of_date != today or row.fetch_error is not None or _stale_shape(row):
        return None
    if row.unavailable:
        return FundamentalsEntry(symbol, None, today, stale=False, unavailable=True, error=None)
    return FundamentalsEntry(
        symbol, FundamentalsData.from_payload(row.payload or {}), today, stale=False, unavailable=False, error=None
    )


def _fallback_entry(symbol: str, row: TickerFundamentalsCache | None, today: date) -> FundamentalsEntry:
    """Best available answer once a fetch attempt has failed: last-good
    payload (flagged stale only if it isn't already today's), a standing
    "unavailable", or nothing."""
    if row is None:
        return FundamentalsEntry(symbol, None, None, stale=False, unavailable=False, error="unavailable")
    if row.unavailable:
        return FundamentalsEntry(symbol, None, row.as_of_date, stale=False, unavailable=True, error=row.fetch_error)
    if row.payload:
        return FundamentalsEntry(
            symbol,
            FundamentalsData.from_payload(row.payload),
            row.as_of_date,
            stale=row.as_of_date != today or _stale_shape(row),
            unavailable=False,
            error=row.fetch_error,
        )
    # Empty payload: nothing to be "stale" about — data=None + error
    # already signal the degradation, so _stale_shape isn't folded in here.
    return FundamentalsEntry(
        symbol, None, row.as_of_date, stale=False, unavailable=False, error=row.fetch_error or "unavailable"
    )


def _advisory_lock(db: Session, provider_name: str) -> None:
    db.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"fundamentals_refresh:{provider_name}"},
    )


def _sweep_unheld(db: Session, provider_name: str, keep: set[str]) -> None:
    """Delete cache rows for symbols no ticker_metadata row (any user) maps
    to any more — table hygiene after an add/remove changes the held set.
    A row is only dead once nobody holds it (the cache is not per-user).

    `keep` is the set of symbols this call is about to (re)write — they're
    never swept, even if no metadata currently maps to them, so the sweep
    can't yank a row out from under the _apply_fetched that follows.
    """
    held: set[str] = set(keep)
    for tm in db.query(TickerMetadata.ticker, TickerMetadata.market).all():
        try:
            held.add(derive_yahoo_ticker(tm.ticker, tm.market))
        except Exception:  # noqa: BLE001 — a bad market string must not abort the sweep
            logger.warning("could not derive Yahoo symbol for %s/%s during cache sweep", tm.ticker, tm.market)
    if not held:  # nothing to compare against — never means "delete everything"
        return
    for row in db.query(TickerFundamentalsCache).filter(TickerFundamentalsCache.provider == provider_name).all():
        if row.yahoo_symbol not in held:
            db.delete(row)


def _fetch_one(provider: FundamentalsProvider, symbol: str) -> tuple[str, _Fetched | None]:
    try:
        return symbol, _Fetched(kind="ok", data=provider.fetch(symbol))
    except FundamentalsRateLimited:
        return symbol, None
    except FundamentalsUnavailable:
        return symbol, _Fetched(kind="unavailable")
    except Exception as exc:  # noqa: BLE001 — a transient provider failure is per-symbol, not fatal
        logger.warning("fundamentals fetch failed for %s: %s", symbol, exc)
        return symbol, _Fetched(kind="error", message=str(exc))


def _fetch_many(
    provider: FundamentalsProvider, symbols: list[str]
) -> tuple[dict[str, _Fetched], list[str]]:
    if len(symbols) == 1:
        results = [_fetch_one(provider, symbols[0])]
    else:
        with ThreadPoolExecutor(max_workers=min(len(symbols), _MAX_FETCH_WORKERS)) as pool:
            results = list(pool.map(lambda s: _fetch_one(provider, s), symbols))
    fetched: dict[str, _Fetched] = {}
    rate_limited: list[str] = []
    for symbol, outcome in results:
        if outcome is None:
            rate_limited.append(symbol)
        else:
            fetched[symbol] = outcome
    return fetched, rate_limited


def _retry_rate_limited(
    provider: FundamentalsProvider, symbols: list[str], sleep: Callable[[float], None]
) -> tuple[dict[str, _Fetched], list[str]]:
    pending = list(symbols)
    fetched: dict[str, _Fetched] = {}
    for delay in _RETRY_BACKOFF:
        if not pending:
            break
        sleep(delay)
        still_pending: list[str] = []
        for symbol, outcome in (_fetch_one(provider, s) for s in pending):
            if outcome is None:
                still_pending.append(symbol)
            else:
                fetched[symbol] = outcome
        pending = still_pending
    return fetched, pending


def _row_for(
    db: Session, provider_name: str, symbol: str, rows: dict[str, TickerFundamentalsCache]
) -> TickerFundamentalsCache:
    row = rows.get(symbol)
    if row is None:
        row = TickerFundamentalsCache(provider=provider_name, yahoo_symbol=symbol)
        db.add(row)
        rows[symbol] = row
    return row


def _apply_fetched(
    db: Session,
    provider_name: str,
    today: date,
    rows: dict[str, TickerFundamentalsCache],
    fetched: dict[str, _Fetched],
) -> None:
    stamp = datetime.now(timezone.utc)
    for symbol, outcome in fetched.items():
        row = _row_for(db, provider_name, symbol, rows)
        if outcome.kind == "ok" and outcome.data is not None:
            row.payload = outcome.data.to_payload()
            row.payload_version = CACHE_PAYLOAD_VERSION
            row.unavailable = False
            row.as_of_date = today
            row.fetched_at = stamp
            row.fetch_error = None
        elif outcome.kind == "unavailable":
            row.payload = None
            row.payload_version = CACHE_PAYLOAD_VERSION
            row.unavailable = True
            row.as_of_date = today
            row.fetched_at = stamp
            row.fetch_error = None
        else:
            # Transient failure: keep any prior payload, leave as_of_date
            # alone so the symbol is retried next call.
            row.fetch_error = outcome.message or "fetch failed"


def _mark_rate_limited(
    db: Session, provider_name: str, rows: dict[str, TickerFundamentalsCache], symbols: list[str]
) -> None:
    for symbol in symbols:
        _row_for(db, provider_name, symbol, rows).fetch_error = "rate_limited"


def get_fundamentals(
    db: Session,
    yahoo_symbols: Iterable[str],
    *,
    provider: FundamentalsProvider | None = None,
    force_refresh: bool = False,
    today: date | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, FundamentalsEntry]:
    provider = provider or get_fundamentals_provider()
    provider_name = provider.name
    today = _resolve_today(today)

    # dedupe, preserve caller order, drop blanks
    symbols = list(dict.fromkeys(s for s in yahoo_symbols if s))
    result: dict[str, FundamentalsEntry] = {}
    if not symbols:
        return result

    pending = symbols
    if not force_refresh:
        remaining: list[str] = []
        for symbol in symbols:
            hit = _mem_get(provider_name, symbol, today)
            if hit is not None:
                result[symbol] = hit
            else:
                remaining.append(symbol)
        pending = remaining
    if not pending:
        return result

    rows = _load_rows(db, provider_name, pending)
    needs_fetch: list[str] = []
    for symbol in pending:
        entry = None if force_refresh else _fresh_entry(symbol, rows.get(symbol), today)
        if entry is None:
            needs_fetch.append(symbol)
        else:
            result[symbol] = entry
            _mem_put(provider_name, entry)
    if not needs_fetch:
        return result

    # --- write path -------------------------------------------------------
    _advisory_lock(db, provider_name)
    rows = _load_rows(db, provider_name, needs_fetch)
    still: list[str] = []
    for symbol in needs_fetch:
        entry = None if force_refresh else _fresh_entry(symbol, rows.get(symbol), today)
        if entry is None:
            still.append(symbol)
        else:
            result[symbol] = entry
            _mem_put(provider_name, entry)

    # keep = every symbol this call was asked about, so the sweep can never
    # delete a row another step here is about to write or that we've
    # already handed back in `result`.
    _sweep_unheld(db, provider_name, keep=set(symbols))
    if not still:
        db.commit()
        return result

    fetched, rate_limited = _fetch_many(provider, still)
    _apply_fetched(db, provider_name, today, rows, fetched)
    db.commit()  # releases the advisory lock — successes are now durable

    if rate_limited:
        # Retry the throttled subset off-lock (only network, plus sleeps),
        # then re-acquire the lock and re-load rows before writing so a
        # concurrent caller that inserted the same brand-new symbol in the
        # meantime is an UPDATE here, not a unique-constraint violation.
        retried, still_limited = _retry_rate_limited(provider, rate_limited, sleep)
        _advisory_lock(db, provider_name)
        rows = _load_rows(db, provider_name, rate_limited)
        _apply_fetched(db, provider_name, today, rows, retried)
        _mark_rate_limited(db, provider_name, rows, still_limited)
        db.commit()

    final_rows = _load_rows(db, provider_name, still)
    for symbol in still:
        row = final_rows.get(symbol)
        entry = _fresh_entry(symbol, row, today) or _fallback_entry(symbol, row, today)
        result[symbol] = entry
        if entry.as_of_date == today and entry.error is None:
            _mem_put(provider_name, entry)
    return result
