"""The two value-investing MCP tools (get_portfolio_fundamentals /
get_ticker_fundamentals), called directly through mcp_server.call_tool
against seeded Postgres data, with a fake fundamentals provider.
"""

import json
from datetime import datetime, timezone

import pytest
from sqlalchemy.orm import Session

import app.services.fundamentals.cache as cache_module
from app.mcp_server import mcp_server
from app.models import PortfolioTransaction, TickerMetadata, User
from app.services.agent_context import set_current_user_id
from app.services.fundamentals.base import FundamentalsData, FundamentalsUnavailable
from app.services.price_service import PriceQuote


def _text(result) -> str:
    return "".join(getattr(c, "text", "") for c in result.content)


def _payload(result) -> dict:
    return json.loads(_text(result))


class FakeProvider:
    name = "yahoo"

    def __init__(self, data: dict[str, FundamentalsData], unavailable: set[str] = frozenset()) -> None:
        self._data = data
        self._unavailable = set(unavailable)
        self.calls: list[str] = []

    def fetch(self, symbol: str) -> FundamentalsData:
        self.calls.append(symbol)
        if symbol in self._unavailable:
            raise FundamentalsUnavailable("no fundamentals for an ETF")
        return self._data[symbol]


AAPL_FUNDAMENTALS = FundamentalsData(
    symbol="AAPL",
    company_name="Apple Inc.",
    sector="Technology",
    currency="USD",
    price=189.30,
    market_cap=2.95e12,
    pe_trailing=24.0,
    pe_forward=22.0,
    peg_ratio=1.8,
    price_to_book=40.0,
    price_to_sales=7.6,
    return_on_equity=1.4,
    operating_margin=0.30,
    profit_margin=0.25,
    eps_trailing=6.0,
    book_value_per_share=4.0,
    free_cash_flow=100e9,
    total_revenue=390e9,
    debt_to_equity=140.0,
    dividend_yield=0.55,
    dividend_rate=1.0,
    payout_ratio=0.15,
    financial_currency="USD",
    growth_0y=0.08,
    growth_1y=0.09,
)


@pytest.fixture
def wire_tools(db_session: Session, monkeypatch):
    """agent_tools opens its own session (see agent_context.py) — point it
    at the test transaction, mock live price/FX for the rollup, and swap in
    a fake fundamentals provider."""

    def factory() -> Session:
        return Session(bind=db_session.connection(), join_transaction_mode="create_savepoint")

    monkeypatch.setattr("app.services.agent_tools._get_session", factory)
    monkeypatch.setattr(
        "app.services.portfolio_rollup_service.fetch_current_price",
        lambda symbol, force_refresh=False: PriceQuote(
            price=150.0, currency="USD", timestamp=datetime.now(timezone.utc), source="yahoo",
            daily_change_percent=1.0, daily_change=1.5,
        ),
    )
    monkeypatch.setattr(
        "app.services.portfolio_rollup_service.fetch_fx_rate_to_chf", lambda ccy, force_refresh=False: 0.9
    )

    def install_provider(provider: FakeProvider) -> None:
        monkeypatch.setattr(cache_module, "get_fundamentals_provider", lambda: provider)

    return install_provider


def _seed_holding(db: Session, user: User, ticker: str, market: str, currency: str, qty: float = 10.0) -> None:
    db.add(TickerMetadata(user_id=user.id, ticker=ticker, market=market, category="Stock", native_currency=currency))
    db.add(
        PortfolioTransaction(
            user_id=user.id, ticker=ticker, date=datetime(2024, 1, 1, tzinfo=timezone.utc), type="BUY",
            native_currency=currency, quantity=qty, price_per_share=100.0, fx_rate_to_chf=0.9,
        )
    )
    db.flush()


class TestGetTickerFundamentals:
    async def test_returns_metrics_and_a_screen_for_a_tracked_holding(
        self, db_session: Session, test_user: User, wire_tools
    ) -> None:
        _seed_holding(db_session, test_user, "AAPL", "NASDAQ", "USD")
        wire_tools(FakeProvider({"AAPL": AAPL_FUNDAMENTALS}))
        set_current_user_id(test_user.id)

        body = _payload(await mcp_server.call_tool("get_ticker_fundamentals", {"ticker": "aapl"}))

        assert body["ticker"] == "AAPL"
        assert body["yahooSymbol"] == "AAPL"
        assert body["status"] == "ok"
        assert body["peTrailing"] == 24.0
        assert body["screenOverall"]
        assert any(m["metric"] == "pe_trailing" for m in body["metrics"])

    async def test_reports_not_found_for_an_untracked_ticker(
        self, db_session: Session, test_user: User, wire_tools
    ) -> None:
        wire_tools(FakeProvider({}))
        set_current_user_id(test_user.id)

        body = _payload(await mcp_server.call_tool("get_ticker_fundamentals", {"ticker": "TSLA"}))

        assert body["found"] is False
        assert "isn't tracked" in body["message"]

    async def test_reports_unavailable_for_an_etf(self, db_session: Session, test_user: User, wire_tools) -> None:
        _seed_holding(db_session, test_user, "VWRA", "SIX", "CHF")
        wire_tools(FakeProvider({}, unavailable={"VWRA.SW"}))
        set_current_user_id(test_user.id)

        body = _payload(await mcp_server.call_tool("get_ticker_fundamentals", {"ticker": "VWRA"}))

        assert body["status"] == "unavailable"
        assert body["yahooSymbol"] == "VWRA.SW"


class TestGetPortfolioFundamentals:
    async def test_lists_holdings_with_weights_and_weighted_aggregates(
        self, db_session: Session, test_user: User, wire_tools
    ) -> None:
        _seed_holding(db_session, test_user, "AAPL", "NASDAQ", "USD")
        wire_tools(FakeProvider({"AAPL": AAPL_FUNDAMENTALS}))
        set_current_user_id(test_user.id)

        body = _payload(await mcp_server.call_tool("get_portfolio_fundamentals", {}))

        assert body["provider"] == "yahoo"
        assert len(body["holdings"]) == 1
        holding = body["holdings"][0]
        assert holding["ticker"] == "AAPL"
        assert holding["weightPercent"] == pytest.approx(100.0)
        assert holding["status"] == "ok"
        assert body["weightedAggregates"]["weightedPeTrailing"] == pytest.approx(24.0)
        assert body["weightedAggregates"]["coveredPercent"] == pytest.approx(100.0)
        assert body["uncoveredTickers"] == []

    async def test_excludes_unavailable_holdings_from_aggregates(
        self, db_session: Session, test_user: User, wire_tools
    ) -> None:
        _seed_holding(db_session, test_user, "AAPL", "NASDAQ", "USD", qty=10.0)
        _seed_holding(db_session, test_user, "VWRA", "SIX", "CHF", qty=10.0)
        wire_tools(FakeProvider({"AAPL": AAPL_FUNDAMENTALS}, unavailable={"VWRA.SW"}))
        set_current_user_id(test_user.id)

        body = _payload(await mcp_server.call_tool("get_portfolio_fundamentals", {}))

        statuses = {h["ticker"]: h["status"] for h in body["holdings"]}
        assert statuses == {"AAPL": "ok", "VWRA": "unavailable"}
        assert body["uncoveredTickers"] == ["VWRA"]
        # AAPL is the only covered name, so the weighted P/E is just its own
        assert body["weightedAggregates"]["weightedPeTrailing"] == pytest.approx(24.0)
        assert body["weightedAggregates"]["coveredPercent"] < 100.0
        assert any("excluded from aggregates" in note for note in body["notes"])

    async def test_holdings_carry_an_intrinsic_value_block(
        self, db_session: Session, test_user: User, wire_tools
    ) -> None:
        _seed_holding(db_session, test_user, "AAPL", "NASDAQ", "USD")
        wire_tools(FakeProvider({"AAPL": AAPL_FUNDAMENTALS}))
        set_current_user_id(test_user.id)

        body = _payload(await mcp_server.call_tool("get_portfolio_fundamentals", {}))

        iv = body["holdings"][0]["valuation"]
        assert iv["available"] is True
        assert isinstance(iv["intrinsicValue"], (int, float))
        assert iv["verdict"] in {"undervalued", "overvalued", "near fair value"}
        assert any("Scenario-DCF" in note for note in body["notes"])


class TestGetIntrinsicValue:
    async def test_returns_a_dcf_estimate_for_a_tracked_holding(
        self, db_session: Session, test_user: User, wire_tools
    ) -> None:
        _seed_holding(db_session, test_user, "AAPL", "NASDAQ", "USD")
        wire_tools(FakeProvider({"AAPL": AAPL_FUNDAMENTALS}))
        set_current_user_id(test_user.id)

        body = _payload(await mcp_server.call_tool("get_intrinsic_value", {"ticker": "aapl"}))

        assert body["ticker"] == "AAPL"
        assert body["status"] == "ok"
        iv = body["valuation"]
        assert iv["available"] is True
        assert iv["valuationBasis"] in {"EPS-based", "FCF-based", "Dividend-based", "Revenue-based"}
        assert "marginOfSafetyPercent" in iv
        assert iv["assessment"].startswith("Intrinsic Value")

    async def test_reports_not_found_for_an_untracked_ticker(
        self, db_session: Session, test_user: User, wire_tools
    ) -> None:
        wire_tools(FakeProvider({}))
        set_current_user_id(test_user.id)

        body = _payload(await mcp_server.call_tool("get_intrinsic_value", {"ticker": "TSLA"}))

        assert body["found"] is False

    async def test_reports_unavailable_for_an_etf(
        self, db_session: Session, test_user: User, wire_tools
    ) -> None:
        _seed_holding(db_session, test_user, "VWRA", "SIX", "CHF")
        wire_tools(FakeProvider({}, unavailable={"VWRA.SW"}))
        set_current_user_id(test_user.id)

        body = _payload(await mcp_server.call_tool("get_intrinsic_value", {"ticker": "VWRA"}))

        assert body["status"] == "unavailable"
        assert body.get("valuation") is None
