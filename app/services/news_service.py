"""Builds the `get_ticker_news` agent-tool response (app/schemas/agent.py)
from the Google News feed client in app/services/news.py. Same split the
fundamentals package uses: the feed client knows nothing about the
database or the response shape, this module joins the two.

Everything here is synchronous and does blocking network I/O (a
fundamentals lookup for the company name, then one or more RSS fetches) —
the MCP tool wrapping it runs it off the event loop via asyncio.to_thread.

`ticker_news` takes a session FACTORY rather than a session, for two
reasons, both specific to running in that worker thread:

- Lifetime. Every other tool here opens its session on the event loop and
  closes it when the tool returns. Under `asyncio.to_thread` that breaks:
  cancelling the awaiting task (a client disconnect on the `/agent/ask`
  SSE stream) does NOT stop the worker thread, so the `finally: close()`
  would run on the loop while the worker is still issuing statements on
  that connection — and the pool could hand it to another request
  meanwhile. Opening and closing inside the worker keeps one owner.
- Pool pressure. The escalation can spend up to four 10s RSS requests in
  this function while the actual DB work takes milliseconds at the start.
  Holding a pooled connection across all of that would exhaust a default
  pool of 5 under a handful of concurrent questions, on a database shared
  with another app. So the session is closed before the first fetch.
"""

import logging
from collections.abc import Callable

from sqlalchemy.orm import Session

from app.models import TickerMetadata
from app.schemas.agent import NewsItem, TickerNews
from app.services.fundamentals.cache import get_fundamentals
from app.services.news import fetch_ticker_news
from app.services.ticker_config import derive_yahoo_ticker

logger = logging.getLogger(__name__)


def _company_identity(db: Session, yahoo_symbol: str) -> tuple[str | None, str | None]:
    """(company_name, sector) from the daily fundamentals cache, for the
    news query and the relevance filter. Both are None for a security with
    no fundamentals at all (an ETF, a gold tracker, crypto, or a ticker not
    resolvable at all), which is not an error here — the search just falls
    back to the bare ticker and the sector keyword tier of the relevance
    filter goes unused.

    A fundamentals fetch failure is swallowed for the same reason: news is
    still worth returning on a ticker-only query, and failing the whole
    tool because an unrelated upstream is rate-limited would be worse than
    slightly weaker search terms.
    """
    try:
        entry = get_fundamentals(db, [yahoo_symbol]).get(yahoo_symbol)
    except Exception as exc:  # noqa: BLE001
        logger.warning("news: fundamentals lookup failed for %s, searching by ticker only: %s", yahoo_symbol, exc)
        return None, None
    if entry is None or entry.data is None:
        return None, None
    return entry.data.company_name, entry.data.sector


def ticker_news(session_factory: Callable[[], Session], ticker: str, user_id: str, limit: int = 5) -> dict:
    ticker = ticker.upper()

    # Scoped tightly on purpose — see the module docstring. Nothing below
    # this block touches the database.
    db = session_factory()
    try:
        metadata = (
            db.query(TickerMetadata)
            .filter(TickerMetadata.ticker == ticker, TickerMetadata.user_id == user_id)
            .first()
        )
        # Not tracked isn't an error here (2026-09-18) — see
        # fundamentals/service.py's ticker_fundamentals for the same
        # reasoning. No `market` to build a Yahoo suffix from, so the bare
        # ticker is used instead.
        yahoo_symbol = derive_yahoo_ticker(ticker, metadata.market) if metadata else ticker
        company_name, sector = _company_identity(db, yahoo_symbol)
    finally:
        db.close()

    result = fetch_ticker_news(ticker, name=company_name, sector=sector, limit=limit)

    return TickerNews(
        ticker=ticker,
        company_name=company_name,
        sector=sector,
        window_days=result.window_days,
        status=result.status,
        message=result.message,
        held_in_portfolio=metadata is not None,
        items=[
            NewsItem(
                title=item.title,
                publisher=item.publisher,
                published_date=item.published_date.isoformat() if item.published_date else None,
                link=item.link,
            )
            for item in result.items
        ],
    ).model_dump(mode="json", by_alias=True)
