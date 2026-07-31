"""Ported from lib/portfolio/rollup.ts. FX/price fetches are genuinely
parallelized with a thread pool (yfinance calls are blocking I/O),
matching the original's Promise.all concurrency.
"""

from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy.orm import Session

from app.models import PortfolioTransaction, TickerMetadata
from app.schemas.portfolio import ClosedTickerRollup, OpenTickerRollup, PortfolioRollup, TickerPriceError
from app.services.fifo import process_ticker
from app.services.mappers import portfolio_transaction_to_processed
from app.services.price_service import fetch_current_price, fetch_fx_rate_to_chf
from app.services.ticker_config import derive_yahoo_ticker

TOLERANCE = 1e-9


def compute_portfolio_rollup(db: Session, user_id: str | None = None) -> PortfolioRollup:
    txn_query = db.query(PortfolioTransaction).order_by(PortfolioTransaction.date.asc())
    metadata_query = db.query(TickerMetadata)
    if user_id:
        txn_query = txn_query.filter(PortfolioTransaction.user_id == user_id)
        metadata_query = metadata_query.filter(TickerMetadata.user_id == user_id)

    metadata_by_ticker = {m.ticker: m for m in metadata_query.all()}
    by_ticker: dict[str, list[PortfolioTransaction]] = defaultdict(list)
    for t in txn_query.all():
        by_ticker[t.ticker].append(t)

    closed_tickers: list[ClosedTickerRollup] = []
    open_candidates: list[dict] = []
    total_dividends_chf = 0.0
    total_realized_gain_chf = 0.0

    # --- first pass (no I/O) ---
    for ticker, txns in by_ticker.items():
        processed = [portfolio_transaction_to_processed(t) for t in txns]
        non_dividend = [p for p in processed if p.type != "DIVIDEND"]
        dividends = [p for p in processed if p.type == "DIVIDEND"]

        fifo = process_ticker(non_dividend)
        total_realized_gain_chf += fifo.total_realized_gain_chf

        dividends_chf = sum((p.cash_amount or 0) * p.fx_rate_to_chf for p in dividends)
        total_dividends_chf += dividends_chf

        if fifo.current_shares <= TOLERANCE:
            closed_tickers.append(
                ClosedTickerRollup(
                    ticker=ticker, dividends_chf=dividends_chf, realized_gain_chf=fifo.total_realized_gain_chf
                )
            )
            continue

        metadata = metadata_by_ticker.get(ticker)
        if metadata is None:
            # Hard fail for the whole rollup — unlike FX/price failures
            # below, which are caught per-ticker. An open position with no
            # metadata is a data-integrity problem, not a transient one.
            raise RuntimeError(f"No TickerMetadata for {ticker} — cannot resolve market/category/live price symbol")

        open_candidates.append(
            {
                "ticker": ticker,
                "category": metadata.category,
                "native_currency": metadata.native_currency,
                "yahoo_ticker": derive_yahoo_ticker(ticker, metadata.market),
                "current_shares": fifo.current_shares,
                "cost_basis_native": fifo.current_cost_basis_native,
                "cost_basis_chf": fifo.current_cost_basis_chf,
                "dividends_chf": dividends_chf,
            }
        )

    # --- second pass (I/O), fetched in parallel. FX and price fetches run in
    # the same pool — fetch_fx_rate_to_chf is itself cached/deduplicated per
    # currency (see price_service.py), so there's no need for a separate
    # pre-fetch stage before this one. ---
    open_tickers: list[OpenTickerRollup] = []
    price_errors: list[TickerPriceError] = []

    def fetch_open_ticker(candidate: dict) -> tuple[OpenTickerRollup | None, TickerPriceError | None]:
        try:
            fx_rate = fetch_fx_rate_to_chf(candidate["native_currency"])
            quote = fetch_current_price(candidate["yahoo_ticker"])
            market_value_native = candidate["current_shares"] * quote.price
            market_value_chf = market_value_native * fx_rate
            row = OpenTickerRollup(
                ticker=candidate["ticker"],
                category=candidate["category"],
                native_currency=candidate["native_currency"],
                current_shares=candidate["current_shares"],
                cost_basis_native=candidate["cost_basis_native"],
                cost_basis_chf=candidate["cost_basis_chf"],
                current_price_native=quote.price,
                current_fx_rate_to_chf=fx_rate,
                market_value_native=market_value_native,
                market_value_chf=market_value_chf,
                unrealized_gain_native=market_value_native - candidate["cost_basis_native"],
                unrealized_gain_chf=market_value_chf - candidate["cost_basis_chf"],
                dividends_chf=candidate["dividends_chf"],
                price_timestamp=quote.timestamp,
                price_source=quote.source,
                daily_change_percent=quote.daily_change_percent,
                daily_change=quote.daily_change,
            )
            return row, None
        # A failed price/FX fetch must never fall back to a stale/fabricated
        # value — excluded from open_tickers (and thus from all CHF totals),
        # recorded in price_errors instead.
        except Exception as exc:  # noqa: BLE001
            return None, TickerPriceError(ticker=candidate["ticker"], error=str(exc))

    if open_candidates:
        with ThreadPoolExecutor(max_workers=len(open_candidates)) as pool:
            for row, error in pool.map(fetch_open_ticker, open_candidates):
                if row is not None:
                    open_tickers.append(row)
                if error is not None:
                    price_errors.append(error)

    open_tickers.sort(key=lambda r: r.ticker)
    closed_tickers.sort(key=lambda r: r.ticker)

    return PortfolioRollup(
        open_tickers=open_tickers,
        closed_tickers=closed_tickers,
        price_errors=price_errors,
        total_cost_basis_chf=sum(r.cost_basis_chf for r in open_tickers),
        total_market_value_chf=sum(r.market_value_chf for r in open_tickers),
        total_unrealized_gain_chf=sum(r.unrealized_gain_chf for r in open_tickers),
        total_dividends_chf=total_dividends_chf,
        total_realized_gain_chf=total_realized_gain_chf,
    )
