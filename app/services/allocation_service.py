"""Thin aggregation over compute_portfolio_rollup — no new I/O, just
grouping its already-fetched open_tickers by category/currency.
"""

from collections import defaultdict

from sqlalchemy.orm import Session

from app.schemas.agent import AllocationBreakdown, CategoryAllocationRow, CurrencyAllocationRow
from app.services.portfolio_rollup_service import compute_portfolio_rollup


def compute_allocation(db: Session, user_id: str, *, force_refresh: bool = False) -> AllocationBreakdown:
    rollup = compute_portfolio_rollup(db, user_id, force_refresh=force_refresh)
    total = rollup.total_market_value_chf

    by_category: dict[str, float] = defaultdict(float)
    by_currency: dict[str, float] = defaultdict(float)
    for row in rollup.open_tickers:
        by_category[row.category] += row.market_value_chf
        by_currency[row.native_currency] += row.market_value_chf

    def pct(value: float) -> float:
        return (value / total * 100) if total > 0 else 0.0

    return AllocationBreakdown(
        total_market_value_chf=total,
        by_category=[
            CategoryAllocationRow(category=cat, market_value_chf=value, percent_of_portfolio=pct(value))
            for cat, value in sorted(by_category.items(), key=lambda kv: kv[1], reverse=True)
        ],
        by_currency=[
            CurrencyAllocationRow(currency=ccy, market_value_chf=value, percent_of_portfolio=pct(value))
            for ccy, value in sorted(by_currency.items(), key=lambda kv: kv[1], reverse=True)
        ],
        price_errors=rollup.price_errors,
    )
