"""GET /portfolio/tickers/{ticker} always requires and scopes by user_id
to prevent a cross-tenant data leak, so this function's `user_id`
parameter is non-optional (unlike the rollup functions, which keep an
optional user_id for internal/unscoped use).
"""

from sqlalchemy.orm import Session

from app.models import PortfolioTransaction, TickerMetadata
from app.schemas.portfolio import RealizedGainRow, TickerDetail, TransactionRow
from app.services.fifo import process_ticker
from app.services.mappers import portfolio_transaction_to_processed
from app.services.price_service import fetch_current_price, fetch_fx_rate_to_chf
from app.services.ticker_config import derive_yahoo_ticker

TOLERANCE = 1e-9


def compute_ticker_detail(db: Session, ticker: str, user_id: str) -> TickerDetail | None:
    metadata = (
        db.query(TickerMetadata).filter(TickerMetadata.ticker == ticker, TickerMetadata.user_id == user_id).first()
    )
    if metadata is None:
        return None

    txns = (
        db.query(PortfolioTransaction)
        .filter(PortfolioTransaction.ticker == ticker, PortfolioTransaction.user_id == user_id)
        .order_by(PortfolioTransaction.date.asc())
        .all()
    )

    processed = [portfolio_transaction_to_processed(t) for t in txns]
    non_dividend = [p for p in processed if p.type != "DIVIDEND"]
    dividends = [p for p in processed if p.type == "DIVIDEND"]

    fifo = process_ticker(non_dividend)

    dividends_native = sum(p.cash_amount or 0 for p in dividends)
    dividends_chf = sum((p.cash_amount or 0) * p.fx_rate_to_chf for p in dividends)

    average_cost_per_share_native = (
        fifo.current_cost_basis_native / fifo.current_shares if fifo.current_shares > 0 else 0.0
    )

    detail = TickerDetail(
        ticker=ticker,
        market=metadata.market,
        category=metadata.category,
        native_currency=metadata.native_currency,
        current_shares=fifo.current_shares,
        cost_basis_native=fifo.current_cost_basis_native,
        cost_basis_chf=fifo.current_cost_basis_chf,
        average_cost_per_share_native=average_cost_per_share_native,
        dividends_native=dividends_native,
        dividends_chf=dividends_chf,
        total_realized_gain_native=fifo.total_realized_gain_native,
        total_realized_gain_chf=fifo.total_realized_gain_chf,
        realized_gains=[
            RealizedGainRow(
                date=r.date,
                qty_sold=r.qty_sold,
                proceeds_native=r.proceeds_native,
                cost_basis_native=r.cost_basis_native,
                gain_native=r.gain_native,
                gain_chf=r.gain_chf,
            )
            for r in fifo.realized_gains
        ],
        transactions=[
            TransactionRow(
                id=t.id,
                date=t.date,
                type=t.type,
                native_currency=t.native_currency,
                quantity=t.quantity,
                price_per_share=t.price_per_share,
                cash_amount=t.cash_amount,
                fx_rate_to_chf=t.fx_rate_to_chf,
                notes=t.notes,
            )
            for t in txns
        ],
    )

    if fifo.current_shares > TOLERANCE:
        try:
            yahoo_ticker = derive_yahoo_ticker(ticker, metadata.market)
            quote = fetch_current_price(yahoo_ticker)
            fx_rate = fetch_fx_rate_to_chf(metadata.native_currency)

            market_value_native = fifo.current_shares * quote.price
            market_value_chf = market_value_native * fx_rate

            detail.current_price_native = quote.price
            detail.current_fx_rate_to_chf = fx_rate
            detail.market_value_native = market_value_native
            detail.market_value_chf = market_value_chf
            detail.unrealized_gain_native = market_value_native - fifo.current_cost_basis_native
            detail.unrealized_gain_chf = market_value_chf - fifo.current_cost_basis_chf
            detail.price_timestamp = quote.timestamp
            detail.price_source = quote.source
        except Exception as exc:  # noqa: BLE001 — never throws; price fields just stay null
            detail.price_error = str(exc)

    return detail
