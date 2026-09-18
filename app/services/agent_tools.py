"""The tool set the portfolio agent (and any external MCP client) can call.
Registered once onto the shared mcp_server instance (see app/mcp_server.py)
via register_tools() — the /agent/ask loop also dispatches through that same
mcp_server object (mcp_server.list_tools() / .call_tool(...), called
in-process, no HTTP loopback), so these functions are the single source of
truth for both consumers.

Every function here opens its own short-lived DB session and resolves the
caller's identity via agent_context.resolve_user_id() — neither is injected
by FastAPI, since MCP tool calls don't go through FastAPI's dependency
system. Return values are plain camelCase-keyed dicts (matching every other
API response in this app) so the LLM sees the same field names a human
would in the UI.

The LLM must never compute a financial figure itself — every number
returned here comes from the same deterministic services the rest of the
app uses (FIFO, live pricing, rollups).
"""

import asyncio
import math
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
from typing import Literal

from mcp.server import MCPServer
from sqlalchemy.orm import Session

from app.db.session import SessionLocal
from app.models import PortfolioTransaction, TickerMetadata
from app.schemas.agent import AllocationBreakdown, TickerNotFound
from app.schemas.agent import WhatIfImpact as WhatIfImpactOut
from app.schemas.passive import PassiveRollup
from app.schemas.portfolio import PortfolioRollup, TickerDetail
from app.services.agent_context import resolve_user_id
from app.services.allocation_service import compute_allocation
from app.services.fifo import ProcessedTransaction
from app.services.fundamentals.base import FundamentalsRateLimited
from app.services.fundamentals.service import (
    portfolio_fundamentals,
    ticker_fundamentals,
    ticker_intrinsic_value,
)
from app.services.fundamentals.yahoo_provider import resolve_ticker
from app.services.mappers import portfolio_transaction_to_processed
from app.services.news_service import ticker_news
from app.services.passive_rollup_service import compute_passive_rollup
from app.services.portfolio_rollup_service import compute_portfolio_rollup
from app.services.price_service import PriceQuote, fetch_current_price, fetch_fx_rate_to_chf
from app.services.ticker_config import MARKET_YAHOO_SUFFIX, TICKER_CONFIGS, derive_yahoo_ticker
from app.services.ticker_detail import compute_ticker_detail
from app.services.whatif_service import WhatIfImpact, WhatIfTransaction, simulate_whatif


def _get_session() -> Session:
    """Indirection point so tests can substitute a session bound to their
    own transactional connection (see tests/test_agent_route.py) — each
    tool below opens its session through this, not SessionLocal() directly.
    """
    return SessionLocal()


@contextmanager
def _tool_context() -> Iterator[tuple[str, Session]]:
    """Every tool needs the same two things and the same cleanup — resolve
    who's calling, open a session, close it after. Pulled out once instead
    of repeating resolve_user_id()/_get_session()/try-finally in each tool.
    """
    user_id = resolve_user_id()
    db = _get_session()
    try:
        yield user_id, db
    finally:
        db.close()


def _load_existing_transactions(db: Session, ticker: str, user_id: str) -> list[ProcessedTransaction]:
    """process_ticker already no-ops DIVIDEND rows on its own (see fifo.py)
    — filtering here isn't load-bearing for correctness, it just mirrors
    portfolio_rollup_service.py/ticker_detail.py's same split and skips
    mapping rows compute_whatif has no use for (unlike those two, it
    doesn't need a dividends total).
    """
    txns = (
        db.query(PortfolioTransaction)
        .filter(PortfolioTransaction.ticker == ticker, PortfolioTransaction.user_id == user_id)
        .order_by(PortfolioTransaction.date.asc(), PortfolioTransaction.id.asc())
        .all()
    )
    return [portfolio_transaction_to_processed(t) for t in txns if t.type != "DIVIDEND"]


def _guess_yahoo_symbol(ticker: str) -> str:
    """A first guess at a Yahoo symbol with no `market` to build a suffix
    from: TICKER_CONFIGS first, else the bare ticker — except a ticker
    already ending in a known exchange suffix (e.g. "NESN.SW") is left
    alone rather than dashed into "NESN-SW".
    """
    if ticker in TICKER_CONFIGS:
        return TICKER_CONFIGS[ticker].yahoo_ticker
    if any(suffix and ticker.endswith(suffix) for suffix in MARKET_YAHOO_SUFFIX.values()):
        return ticker
    return ticker.replace(".", "-")


class _WhatIfPriceError(Exception):
    """Carries the exact user-facing message compute_whatif should return
    for a candidate BUY whose price couldn't be resolved — keeps
    _resolve_transactions_and_quote's return type a plain tuple instead of
    a tuple-or-error-dict union."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def _resolve_new_position_quote(ticker: str) -> PriceQuote:
    """Live price for a ticker with no TickerMetadata row. Tries a
    best-guess symbol directly first; on failure, falls back to
    yahoo_provider.resolve_ticker's yf.Search. A FundamentalsRateLimited
    from that fallback is deliberately left uncaught here, so the caller
    can distinguish it from a genuinely unresolvable ticker.
    """
    candidate = _guess_yahoo_symbol(ticker)
    try:
        return fetch_current_price(candidate)
    except Exception:
        resolved = resolve_ticker(candidate)
        if resolved == candidate:
            raise
        return fetch_current_price(resolved)


def _resolve_transactions_and_quote(
    db: Session, ticker: str, user_id: str, metadata: TickerMetadata | None
) -> tuple[list[ProcessedTransaction], PriceQuote, float]:
    """The tracked-vs-candidate branch compute_whatif needs before it can
    call simulate_whatif. A tracked ticker's fetch failure propagates
    unchanged; a candidate's is converted to _WhatIfPriceError.
    """
    if metadata is not None:
        processed = _load_existing_transactions(db, ticker, user_id)
        yahoo_ticker = derive_yahoo_ticker(ticker, metadata.market)
        quote = fetch_current_price(yahoo_ticker)
        fx_rate = fetch_fx_rate_to_chf(metadata.native_currency)
        return processed, quote, fx_rate

    try:
        quote = _resolve_new_position_quote(ticker)
    except FundamentalsRateLimited:
        raise _WhatIfPriceError("Price data is temporarily rate-limited upstream — try again shortly.") from None
    except Exception:
        raise _WhatIfPriceError(f"Could not find live price data for {ticker}.") from None
    fx_rate = fetch_fx_rate_to_chf(quote.currency)
    return [], quote, fx_rate


def register_tools(mcp: MCPServer) -> None:
    @mcp.tool()
    async def get_holdings(refresh: bool = False) -> dict:
        """Get all current portfolio holdings (open positions), each with
        live market value, cost basis, and unrealized gain in CHF, plus
        portfolio-wide totals. Call this for any question about what's
        currently held or the overall portfolio value.
        """
        with _tool_context() as (user_id, db):
            rollup: PortfolioRollup = compute_portfolio_rollup(db, user_id, force_refresh=refresh)
            return rollup.model_dump(mode="json", by_alias=True)

    @mcp.tool()
    async def get_allocation(refresh: bool = False) -> dict:
        """Get the portfolio's allocation breakdown by category (e.g. Stock,
        REITS, Gold, Crypto) and by currency, each as a percentage of total
        market value in CHF. Call this for questions about diversification,
        concentration, or where the portfolio is over/underweight.
        """
        with _tool_context() as (user_id, db):
            breakdown: AllocationBreakdown = compute_allocation(db, user_id, force_refresh=refresh)
            return breakdown.model_dump(mode="json", by_alias=True)

    @mcp.tool()
    async def get_ticker_detail(ticker: str) -> dict:
        """Get full detail for one ticker already tracked in the portfolio:
        transaction history, realized gains, current shares, cost basis, and
        live market value. Call this when a question is about one specific
        holding rather than the whole portfolio.
        """
        with _tool_context() as (user_id, db):
            detail: TickerDetail | None = compute_ticker_detail(db, ticker, user_id)
            if detail is None:
                return TickerNotFound(message=f"{ticker} isn't tracked in your portfolio.").model_dump(
                    mode="json", by_alias=True
                )
            return detail.model_dump(mode="json", by_alias=True)

    @mcp.tool()
    async def get_passive_investments(refresh: bool = False) -> dict:
        """Get all passive investments (e.g. savings accounts, funds tracked
        by a manually-updated gain/loss percentage rather than live pricing),
        with cost basis and market value in CHF. Call this for questions that
        should include passive holdings alongside or instead of securities.
        """
        with _tool_context() as (user_id, db):
            rollup: PassiveRollup = compute_passive_rollup(db, user_id, force_refresh=refresh)
            return rollup.model_dump(mode="json", by_alias=True)

    # On an upstream 429 the fundamentals cache does a short bounded
    # backoff (see app/services/fundamentals/cache.py) before falling back
    # to stale/rate_limited — kept to a couple of seconds precisely
    # because this runs inline like the tools above, not off-loop.
    @mcp.tool()
    async def get_portfolio_fundamentals() -> dict:
        """Get company fundamentals for every open holding — valuation
        multiples (P/E, P/B, P/S, PEG, EV/EBITDA), quality metrics (ROE,
        operating/profit margin, FCF yield), leverage (debt/equity), and
        growth — plus a per-metric value-investing verdict and
        value-weighted portfolio aggregates. Call this for any question
        about whether holdings are cheap/expensive/high-quality, or to
        evaluate the portfolio "from a value investing perspective". ETFs,
        gold, and crypto have no fundamentals and are reported as
        `unavailable` and excluded from the aggregates (see
        `coveredPercent`). Data is cached once per day; `stale` / `asOfDate`
        say how fresh each holding is.
        """
        with _tool_context() as (user_id, db):
            return portfolio_fundamentals(db, user_id)

    @mcp.tool()
    async def get_ticker_fundamentals(ticker: str) -> dict:
        """Get company fundamentals and a value-investing metric-by-metric
        verdict for one ticker. Works for an existing holding or a
        candidate the user is considering buying — check `heldInPortfolio`
        rather than assuming. Call this when a question is about the
        valuation or business quality of a single company. Returns
        `unavailable` for a security with no published fundamentals
        (ETF / gold / crypto).
        """
        with _tool_context() as (user_id, db):
            return ticker_fundamentals(db, ticker, user_id)

    @mcp.tool()
    async def get_intrinsic_value(ticker: str) -> dict:
        """Estimate the intrinsic (fair) value of one company with a
        scenario-weighted 2-stage DCF, and compare it to the current price
        as a margin of safety. Works for an existing holding or a
        candidate the user is considering buying — check `heldInPortfolio`
        rather than assuming. Call this for "is X worth its price / how
        much is X really worth / what's my margin of safety on X" style
        questions. Returns the intrinsic value, the % gap vs price
        (positive = overvalued), the valuation basis used (EPS / FCF /
        Dividend / Revenue), and the model's own assessment text. Reports
        `unavailable` for an ETF / gold / crypto, and an unavailable
        intrinsic value when the model has no usable basis for the
        company. All figures are in the security's own trading currency.
        """
        with _tool_context() as (user_id, db):
            return ticker_intrinsic_value(db, ticker, user_id)

    @mcp.tool()
    async def get_ticker_news(ticker: str, limit: int = 5) -> dict:
        """Get recent news headlines about one company, filtered down to
        meaningful coverage — auto-generated 13F-filing spam, "here's why
        the stock moved" pieces and listicle bait are removed rather than
        returned. Works for an existing holding or a candidate the user is
        considering buying — check `heldInPortfolio` rather than assuming.
        Call this for "what's going on with X / any news on X / why has X
        been in the headlines" style questions, and alongside the
        fundamentals tools when a valuation question needs recent
        context. The search starts at the past week and widens only
        if nothing meaningful turns up, so check `windowDays` before
        calling anything "recent": a value of 90 or 365 means the company
        has genuinely been quiet. `status` is "no_news" when even the
        widest window found nothing. Returns headlines only, not article
        text — never treat a headline as a verified fact or derive a
        number from it.
        """
        # The one tool that doesn't use _tool_context(): its work is long
        # (a fundamentals lookup plus up to one RSS fetch per search
        # window) so it runs off the event loop, and a session opened out
        # here would then be closed by _tool_context's `finally` on the
        # loop while the worker thread was still using it — cancelling the
        # awaiting task does not stop that thread. The worker owns its own
        # session instead; see news_service's module docstring.
        user_id = resolve_user_id()
        return await asyncio.to_thread(ticker_news, _get_session, ticker, user_id, limit)

    @mcp.tool()
    async def compute_whatif(
        ticker: str,
        action: Literal["BUY", "SELL"],
        quantity: float,
        price_per_share: float | None = None,
    ) -> dict:
        """Simulate a hypothetical BUY or SELL and report the before/after
        impact on shares, cost basis, market value, and that ticker's
        percentage of the total portfolio. A SELL only works for a ticker
        already tracked in the portfolio (even if fully sold) — there's
        nothing to sell otherwise. A BUY also works for a ticker NOT yet
        tracked, to evaluate it as a brand-new candidate position:
        sharesBefore is 0 and heldInPortfolio is false in that case. If
        price_per_share is omitted, today's live price is used. Call this
        for "what if I bought/sold X shares of Y" questions — never
        estimate this math yourself, always call this tool.
        """
        ticker = ticker.upper()
        # quantity/price_per_share come from the LLM's tool-call arguments,
        # an external trust boundary like any other user input. An unchecked
        # non-positive, NaN, or infinite value doesn't error, it flows
        # straight into process_ticker and produces a confident-looking but
        # nonsense WhatIfImpact instead (an infinite value survives as far
        # as json.dumps, which emits the non-standard `Infinity` token a
        # strict JSON parser like the frontend's then chokes on). `not (x > 0)`
        # rather than `x <= 0` so NaN (which the MCP SDK's schema validation
        # doesn't reject) is also caught — every comparison against NaN is
        # False, so `x <= 0` lets it slip through where `not (x > 0)` doesn't.
        if not (quantity > 0) or not math.isfinite(quantity):
            return {"error": "quantity must be a positive number of shares."}
        if price_per_share is not None and (not (price_per_share > 0) or not math.isfinite(price_per_share)):
            return {"error": "price_per_share must be a positive number."}

        with _tool_context() as (user_id, db):
            metadata = (
                db.query(TickerMetadata)
                .filter(TickerMetadata.ticker == ticker, TickerMetadata.user_id == user_id)
                .first()
            )
            if metadata is None and action == "SELL":
                return {
                    "error": (
                        f"{ticker} isn't tracked in your portfolio, so there's nothing to sell. "
                        'What-if SELL only works for a ticker already held (even if fully sold) — '
                        f'use action="BUY" to evaluate {ticker} as a new position instead.'
                    )
                }

            try:
                processed, quote, fx_rate = _resolve_transactions_and_quote(db, ticker, user_id, metadata)
            except _WhatIfPriceError as exc:
                return {"error": exc.message}

            resolved_price = price_per_share if price_per_share is not None else quote.price
            portfolio_value_chf = compute_portfolio_rollup(db, user_id).total_market_value_chf

            impact: WhatIfImpact = simulate_whatif(
                existing_transactions=processed,
                hypothetical=WhatIfTransaction(
                    ticker=ticker, type=action, quantity=quantity, price_per_share=resolved_price
                ),
                current_price_native=quote.price,
                fx_rate_to_chf=fx_rate,
                portfolio_market_value_chf_before=portfolio_value_chf,
            )
            return WhatIfImpactOut(**asdict(impact), held_in_portfolio=metadata is not None).model_dump(
                mode="json", by_alias=True
            )
