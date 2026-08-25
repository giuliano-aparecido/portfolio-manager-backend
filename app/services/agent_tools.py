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
from app.services.mappers import portfolio_transaction_to_processed
from app.services.passive_rollup_service import compute_passive_rollup
from app.services.portfolio_rollup_service import compute_portfolio_rollup
from app.services.price_service import fetch_current_price, fetch_fx_rate_to_chf
from app.services.ticker_config import derive_yahoo_ticker
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
        .order_by(PortfolioTransaction.date.asc())
        .all()
    )
    return [portfolio_transaction_to_processed(t) for t in txns if t.type != "DIVIDEND"]


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

    @mcp.tool()
    async def compute_whatif(
        ticker: str,
        action: Literal["BUY", "SELL"],
        quantity: float,
        price_per_share: float | None = None,
    ) -> dict:
        """Simulate a hypothetical BUY or SELL of a ticker already tracked in
        the portfolio (it must already exist in the portfolio's ticker list,
        even if fully sold) and report the before/after impact on shares,
        cost basis, market value, and that ticker's percentage of the total
        portfolio. If price_per_share is omitted, today's live price is
        used. Call this for "what if I bought/sold X shares of Y" questions
        — never estimate this math yourself, always call this tool.
        """
        with _tool_context() as (user_id, db):
            metadata = (
                db.query(TickerMetadata)
                .filter(TickerMetadata.ticker == ticker, TickerMetadata.user_id == user_id)
                .first()
            )
            if metadata is None:
                return {
                    "error": (
                        f"{ticker} isn't tracked in your portfolio. What-if simulations only work for "
                        "tickers that already exist in your portfolio's ticker list."
                    )
                }

            processed = _load_existing_transactions(db, ticker, user_id)
            yahoo_ticker = derive_yahoo_ticker(ticker, metadata.market)
            quote = fetch_current_price(yahoo_ticker)
            fx_rate = fetch_fx_rate_to_chf(metadata.native_currency)
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
            return WhatIfImpactOut(**asdict(impact)).model_dump(mode="json", by_alias=True)
