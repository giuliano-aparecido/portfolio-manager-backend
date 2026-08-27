"""Pure hypothetical-trade simulator, modeled on fifo.py's style: dataclasses
in, dataclass out, no DB/HTTP. Unlike fifo.py, an over-sell here is reported
via the `error` field rather than raised — this feeds a chat answer, not an
admin form, so it should read as "you can't do that, here's why" instead of
crashing the request.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from app.services.fifo import ProcessedTransaction, TOLERANCE, process_ticker


@dataclass
class WhatIfTransaction:
    ticker: str
    type: Literal["BUY", "SELL"]
    quantity: float
    price_per_share: float  # native currency


@dataclass
class WhatIfImpact:
    ticker: str
    action: str
    shares_before: float
    shares_after: float
    cost_basis_chf_before: float
    cost_basis_chf_after: float
    market_value_chf_before: float
    market_value_chf_after: float
    realized_gain_chf: float
    portfolio_value_chf_before: float
    portfolio_value_chf_after: float
    ticker_allocation_percent_before: float
    ticker_allocation_percent_after: float
    error: str | None = None


def _pct(part: float, whole: float) -> float:
    return (part / whole * 100) if whole > TOLERANCE else 0.0


def simulate_whatif(
    existing_transactions: list[ProcessedTransaction],
    hypothetical: WhatIfTransaction,
    current_price_native: float,
    fx_rate_to_chf: float,
    portfolio_market_value_chf_before: float,
) -> WhatIfImpact:
    """`existing_transactions` need not be pre-sorted; this function sorts
    them (fifo.process_ticker requires ascending order, unlike its other
    callers which rely on the caller's DB query ordering).

    Ignores where BUY cash comes from / where SELL proceeds go — this
    simulates the position and portfolio-percentage impact of one trade in
    isolation, not a full cash-flow-balanced rebalancing plan.
    """
    sorted_existing = sorted(existing_transactions, key=lambda t: t.date)

    before = process_ticker(sorted_existing)
    shares_before = before.current_shares
    cost_basis_chf_before = before.current_cost_basis_chf
    market_value_chf_before = shares_before * current_price_native * fx_rate_to_chf
    allocation_before = _pct(market_value_chf_before, portfolio_market_value_chf_before)

    hypothetical_txn = ProcessedTransaction(
        ticker=hypothetical.ticker,
        date=datetime.now(timezone.utc),
        type=hypothetical.type,
        native_currency="",  # unused beyond fx_rate_to_chf, passed explicitly below
        fx_rate_to_chf=fx_rate_to_chf,
        quantity=hypothetical.quantity,
        price_per_share=hypothetical.price_per_share,
    )

    try:
        after = process_ticker([*sorted_existing, hypothetical_txn])
    except ValueError as exc:
        return WhatIfImpact(
            ticker=hypothetical.ticker,
            action=hypothetical.type,
            shares_before=shares_before,
            shares_after=shares_before,
            cost_basis_chf_before=cost_basis_chf_before,
            cost_basis_chf_after=cost_basis_chf_before,
            market_value_chf_before=market_value_chf_before,
            market_value_chf_after=market_value_chf_before,
            realized_gain_chf=0.0,
            portfolio_value_chf_before=portfolio_market_value_chf_before,
            portfolio_value_chf_after=portfolio_market_value_chf_before,
            ticker_allocation_percent_before=allocation_before,
            ticker_allocation_percent_after=allocation_before,
            error=str(exc),
        )

    shares_after = after.current_shares
    cost_basis_chf_after = after.current_cost_basis_chf
    market_value_chf_after = shares_after * current_price_native * fx_rate_to_chf
    realized_gain_chf = after.total_realized_gain_chf - before.total_realized_gain_chf
    portfolio_value_chf_after = portfolio_market_value_chf_before - market_value_chf_before + market_value_chf_after

    return WhatIfImpact(
        ticker=hypothetical.ticker,
        action=hypothetical.type,
        shares_before=shares_before,
        shares_after=shares_after,
        cost_basis_chf_before=cost_basis_chf_before,
        cost_basis_chf_after=cost_basis_chf_after,
        market_value_chf_before=market_value_chf_before,
        market_value_chf_after=market_value_chf_after,
        realized_gain_chf=realized_gain_chf,
        portfolio_value_chf_before=portfolio_market_value_chf_before,
        portfolio_value_chf_after=portfolio_value_chf_after,
        ticker_allocation_percent_before=allocation_before,
        ticker_allocation_percent_after=_pct(market_value_chf_after, portfolio_value_chf_after),
        error=None,
    )
