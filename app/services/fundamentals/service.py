"""Builds the agent-tool response shapes (app/schemas/agent.py) from the
cached fundamentals. The MCP tools in app/services/agent_tools.py are thin
wrappers over the two functions here.
"""

import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import TickerMetadata
from app.schemas.agent import (
    FundamentalsMetric,
    HoldingFundamentals,
    IntrinsicValue,
    PortfolioFundamentals,
    SecurityFundamentals,
    SecurityIntrinsicValue,
    TickerNotFound,
    WeightedAggregates,
)
from app.services.fundamentals.cache import FundamentalsEntry, get_fundamentals
from app.services.fundamentals.intrinsic_value import assess_intrinsic_value
from app.services.fundamentals.screen import value_screen
from app.services.portfolio_rollup_service import compute_portfolio_rollup
from app.services.ticker_config import derive_yahoo_ticker

logger = logging.getLogger(__name__)

_UNAVAILABLE_MESSAGE ="No fundamentals are published for this security (typically an ETF, a gold tracker, or crypto)."
_STALE_MESSAGE = "Today's refresh failed — showing the last successfully fetched data."


def _fcf_yield(free_cash_flow: float | None, market_cap: float | None) -> float | None:
    if free_cash_flow is None or not market_cap:
        return None
    return free_cash_flow / market_cap


def _entry_status(entry: FundamentalsEntry | None, ticker: str, yahoo_symbol: str) -> dict:
    """The identity + status/message fields every fundamentals response
    shape shares, plus `data` (the FundamentalsData, or None) so a caller
    branches once without re-walking the entry. Callers pop `data` before
    handing the rest to a schema."""
    base = {
        "ticker": ticker,
        "yahoo_symbol": yahoo_symbol,
        "as_of_date": entry.as_of_date.isoformat() if entry and entry.as_of_date else None,
        "stale": bool(entry and entry.stale),
    }
    if entry is None:
        return {**base, "status": "error", "message": "fundamentals unavailable", "data": None}
    if entry.unavailable:
        return {**base, "status": "unavailable", "message": _UNAVAILABLE_MESSAGE, "data": None}
    if entry.data is None:
        return {**base, "status": "error", "message": entry.error or "fundamentals unavailable", "data": None}
    return {
        **base,
        "status": "error" if entry.error else "ok",
        "message": _STALE_MESSAGE if entry.error else None,
        "data": entry.data,
    }


def _security_fields(entry: FundamentalsEntry | None, ticker: str, yahoo_symbol: str) -> dict:
    fields = _entry_status(entry, ticker, yahoo_symbol)
    data = fields.pop("data")
    if data is None:
        return fields

    screen = value_screen(data)
    return {
        **fields,
        "company_name": data.company_name,
        "sector": data.sector,
        "industry": data.industry,
        "currency": data.currency,
        "price": data.price,
        "market_cap": data.market_cap,
        "pe_trailing": data.pe_trailing,
        "pe_forward": data.pe_forward,
        "peg_ratio": data.peg_ratio,
        "price_to_book": data.price_to_book,
        "price_to_sales": data.price_to_sales,
        "enterprise_to_ebitda": data.enterprise_to_ebitda,
        "return_on_equity": data.return_on_equity,
        "operating_margin": data.operating_margin,
        "profit_margin": data.profit_margin,
        "revenue_growth": data.revenue_growth,
        "earnings_growth": data.earnings_growth,
        "fcf_yield": _fcf_yield(data.free_cash_flow, data.market_cap),
        "debt_to_equity": data.debt_to_equity,
        "current_ratio": data.current_ratio,
        "dividend_yield": data.dividend_yield,
        "payout_ratio": data.payout_ratio,
        "screen_overall": screen["overall"],
        "screen_good_count": screen["goodCount"],
        "screen_poor_count": screen["poorCount"],
        "metrics": [FundamentalsMetric(**m) for m in screen["metrics"]],
        "valuation": IntrinsicValue(**assess_intrinsic_value(data, ticker)),
    }


def ticker_fundamentals(db: Session, ticker: str, user_id: str) -> dict:
    ticker = ticker.upper()
    metadata = (
        db.query(TickerMetadata)
        .filter(TickerMetadata.ticker == ticker, TickerMetadata.user_id == user_id)
        .first()
    )
    if metadata is None:
        return TickerNotFound(message=f"{ticker} isn't tracked in your portfolio.").model_dump(
            mode="json", by_alias=True
        )
    yahoo_symbol = derive_yahoo_ticker(ticker, metadata.market)
    entry = get_fundamentals(db, [yahoo_symbol]).get(yahoo_symbol)
    fields = _security_fields(entry, ticker, yahoo_symbol)
    return SecurityFundamentals(**fields).model_dump(mode="json", by_alias=True)


def ticker_intrinsic_value(db: Session, ticker: str, user_id: str) -> dict:
    ticker = ticker.upper()
    metadata = (
        db.query(TickerMetadata)
        .filter(TickerMetadata.ticker == ticker, TickerMetadata.user_id == user_id)
        .first()
    )
    if metadata is None:
        return TickerNotFound(message=f"{ticker} isn't tracked in your portfolio.").model_dump(
            mode="json", by_alias=True
        )
    yahoo_symbol = derive_yahoo_ticker(ticker, metadata.market)
    entry = get_fundamentals(db, [yahoo_symbol]).get(yahoo_symbol)

    fields = _entry_status(entry, ticker, yahoo_symbol)
    data = fields.pop("data")
    if data is not None:
        fields["valuation"] = IntrinsicValue(**assess_intrinsic_value(data, ticker))
    return SecurityIntrinsicValue(**fields).model_dump(mode="json", by_alias=True)


def _weighted_average(pairs: list[tuple[float, float]]) -> float | None:
    total_weight = sum(weight for weight, _ in pairs)
    if not total_weight:
        return None
    return sum(weight * value for weight, value in pairs) / total_weight


def portfolio_fundamentals(db: Session, user_id: str) -> dict:
    rollup = compute_portfolio_rollup(db, user_id)
    total_mv = rollup.total_market_value_chf
    market_by_ticker = {
        m.ticker: m.market
        for m in db.query(TickerMetadata.ticker, TickerMetadata.market).filter(TickerMetadata.user_id == user_id).all()
    }
    # compute_portfolio_rollup already hard-fails if an open position has no
    # TickerMetadata, so .get() is belt-and-braces for a row deleted in the
    # gap between that call and this query — skip it rather than KeyError.
    symbol_by_ticker = {
        row.ticker: derive_yahoo_ticker(row.ticker, market_by_ticker[row.ticker])
        for row in rollup.open_tickers
        if row.ticker in market_by_ticker
    }
    entries = get_fundamentals(db, list(symbol_by_ticker.values()))

    holdings: list[HoldingFundamentals] = []
    uncovered: list[str] = []
    covered_mv = 0.0
    pe_pairs: list[tuple[float, float]] = []
    pb_pairs: list[tuple[float, float]] = []
    dy_pairs: list[tuple[float, float]] = []
    fcf_pairs: list[tuple[float, float]] = []
    any_stale = False

    for row in rollup.open_tickers:
        yahoo_symbol = symbol_by_ticker.get(row.ticker)
        if yahoo_symbol is None:
            # compute_portfolio_rollup already hard-fails on missing
            # metadata, so this is a should-never-happen — log it rather
            # than drop the holding silently.
            logger.warning("no TickerMetadata for open holding %s — omitted from fundamentals", row.ticker)
            continue
        entry = entries.get(yahoo_symbol)
        weight = (row.market_value_chf / total_mv * 100) if total_mv else 0.0
        fields = _security_fields(entry, row.ticker, yahoo_symbol)
        fields["weight_percent"] = weight
        holdings.append(HoldingFundamentals(**fields))

        if fields["status"] == "ok" and entry is not None and entry.data is not None:
            covered_mv += row.market_value_chf
            data = entry.data
            mv = row.market_value_chf
            if data.pe_trailing and data.pe_trailing > 0:
                pe_pairs.append((mv, data.pe_trailing))
            if data.price_to_book and data.price_to_book > 0:
                pb_pairs.append((mv, data.price_to_book))
            if data.dividend_yield is not None:
                dy_pairs.append((mv, data.dividend_yield))
            fcf_yield = _fcf_yield(data.free_cash_flow, data.market_cap)
            if fcf_yield is not None:
                fcf_pairs.append((mv, fcf_yield))
        else:
            uncovered.append(row.ticker)
            any_stale = any_stale or bool(entry and entry.stale)

    aggregates = WeightedAggregates(
        covered_percent=(covered_mv / total_mv * 100) if total_mv else 0.0,
        weighted_pe_trailing=_weighted_average(pe_pairs),
        weighted_price_to_book=_weighted_average(pb_pairs),
        weighted_dividend_yield=_weighted_average(dy_pairs),
        weighted_fcf_yield=_weighted_average(fcf_pairs),
    )

    notes = [
        "Figures are in each security's own trading currency; multiples are unitless.",
        "Weighted aggregates cover only holdings with fundamentals data — see coveredPercent.",
    ]
    if uncovered:
        notes.append(
            f"{len(uncovered)} holding(s) excluded from aggregates (no published fundamentals): "
            + ", ".join(uncovered)
        )
    if any_stale or any(h.stale for h in holdings):
        notes.append("Some holdings show last-good data because today's refresh failed (stale=true).")

    valued = [h.valuation for h in holdings if h.valuation is not None]
    verdicts = [v.verdict for v in valued if v.available]
    if valued:
        under = verdicts.count("undervalued")
        over = verdicts.count("overvalued")
        notes.append(
            f"Scenario-DCF: {under} holding(s) screen undervalued, {over} overvalued, "
            f"{len(verdicts) - under - over} near fair value; "
            f"the model could not value {len(valued) - len(verdicts)} of the "
            f"{len(valued)} stock holding(s) it assessed."
        )

    result = PortfolioFundamentals(
        as_of=datetime.now(timezone.utc).date().isoformat(),
        provider=get_settings().fundamentals_provider,
        total_market_value_chf=total_mv,
        holdings=holdings,
        weighted_aggregates=aggregates,
        uncovered_tickers=uncovered,
        price_errors=list(rollup.price_errors),
        notes=notes,
    )
    return result.model_dump(mode="json", by_alias=True)
