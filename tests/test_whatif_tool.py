"""The compute_whatif MCP tool's DB-backed behavior (tracked holdings and,
since 2026-09-18, candidate tickers not yet in the portfolio), called
through mcp_server.call_tool against seeded Postgres. Input validation
(quantity/price_per_share) has its own tests in test_agent_tools.py — this
file is about the tracked-vs-candidate branching and price resolution.
"""

import json
from datetime import datetime, timezone

import pytest
from sqlalchemy.orm import Session

from app.mcp_server import mcp_server
from app.models import PortfolioTransaction, TickerMetadata, User
from app.services.agent_context import set_current_user_id
from app.services.fundamentals.base import FundamentalsRateLimited
from app.services.price_service import PriceQuote


def _payload(result) -> dict:
    return json.loads("".join(getattr(c, "text", "") for c in result.content))


@pytest.fixture
def wire_tools(db_session: Session, monkeypatch):
    """agent_tools opens its own session — point it at the test
    transaction. fetch_current_price/fetch_fx_rate_to_chf are imported
    directly into agent_tools' own module namespace (unlike
    portfolio_rollup_service's copies), so they're patched there.
    """

    def factory() -> Session:
        return Session(bind=db_session.connection(), join_transaction_mode="create_savepoint")

    monkeypatch.setattr("app.services.agent_tools._get_session", factory)

    def install(
        prices: dict[str, PriceQuote],
        resolve: dict[str, str] | None = None,
        resolve_raises: Exception | None = None,
    ) -> None:
        def fake_fetch_current_price(yahoo_ticker: str, force_refresh: bool = False) -> PriceQuote:
            if yahoo_ticker not in prices:
                raise RuntimeError(f"No valid price from Yahoo Finance for {yahoo_ticker}")
            return prices[yahoo_ticker]

        def fake_resolve_ticker(ticker: str) -> str:
            if resolve_raises is not None:
                raise resolve_raises
            return (resolve or {}).get(ticker, ticker)

        monkeypatch.setattr("app.services.agent_tools.fetch_current_price", fake_fetch_current_price)
        monkeypatch.setattr("app.services.agent_tools.fetch_fx_rate_to_chf", lambda ccy, force_refresh=False: 0.9)
        if resolve is not None or resolve_raises is not None:
            monkeypatch.setattr("app.services.agent_tools.resolve_ticker", fake_resolve_ticker)

    return install


def _quote(price: float, currency: str = "USD") -> PriceQuote:
    return PriceQuote(
        price=price, currency=currency, timestamp=datetime.now(timezone.utc), source="yahoo",
        daily_change_percent=1.0, daily_change=1.5,
    )


def _seed_holding(db: Session, user: User, ticker: str, market: str, currency: str, qty: float = 10.0) -> None:
    db.add(TickerMetadata(user_id=user.id, ticker=ticker, market=market, category="Stock", native_currency=currency))
    db.add(
        PortfolioTransaction(
            user_id=user.id, ticker=ticker, date=datetime(2024, 1, 1, tzinfo=timezone.utc), type="BUY",
            native_currency=currency, quantity=qty, price_per_share=100.0, fx_rate_to_chf=0.9,
        )
    )
    db.flush()


class TestTrackedHolding:
    async def test_sell_still_works_as_before(self, db_session: Session, test_user: User, wire_tools) -> None:
        _seed_holding(db_session, test_user, "AAPL", "NASDAQ", "USD")
        wire_tools({"AAPL": _quote(150.0)})
        set_current_user_id(test_user.id)

        body = _payload(
            await mcp_server.call_tool("compute_whatif", {"ticker": "aapl", "action": "SELL", "quantity": 5})
        )

        assert body["error"] is None
        assert body["heldInPortfolio"] is True
        assert body["sharesBefore"] == 10.0
        assert body["sharesAfter"] == 5.0

    async def test_buy_still_works_as_before(self, db_session: Session, test_user: User, wire_tools) -> None:
        # The metadata-is-not-None branch of _resolve_transactions_and_quote
        # now shares code with the new candidate path — this pins that a
        # tracked BUY still resolves via TickerMetadata.market/native_currency,
        # not the new best-guess resolution.
        _seed_holding(db_session, test_user, "AAPL", "NASDAQ", "USD")
        wire_tools({"AAPL": _quote(150.0)})
        set_current_user_id(test_user.id)

        body = _payload(
            await mcp_server.call_tool("compute_whatif", {"ticker": "aapl", "action": "BUY", "quantity": 5})
        )

        assert body["error"] is None
        assert body["heldInPortfolio"] is True
        assert body["sharesBefore"] == 10.0
        assert body["sharesAfter"] == 15.0


class TestCandidateNotYetTracked:
    async def test_sell_is_rejected_with_an_actionable_message(
        self, db_session: Session, test_user: User, wire_tools
    ) -> None:
        wire_tools({})
        set_current_user_id(test_user.id)

        body = _payload(
            await mcp_server.call_tool("compute_whatif", {"ticker": "NVDA", "action": "SELL", "quantity": 1})
        )

        assert "nothing to sell" in body["error"]
        assert 'action="BUY"' in body["error"]

    async def test_buy_resolves_a_live_price_with_no_existing_history(
        self, db_session: Session, test_user: User, wire_tools
    ) -> None:
        # No TickerMetadata row at all — the bare ticker resolves directly,
        # the common case for a plain US-listed symbol.
        wire_tools({"NVDA": _quote(120.0)})
        set_current_user_id(test_user.id)

        body = _payload(
            await mcp_server.call_tool("compute_whatif", {"ticker": "nvda", "action": "BUY", "quantity": 10})
        )

        assert body["error"] is None
        assert body["ticker"] == "NVDA"
        assert body["heldInPortfolio"] is False
        assert body["sharesBefore"] == 0.0
        assert body["sharesAfter"] == 10.0
        assert body["marketValueCHFBefore"] == 0.0

    async def test_buy_falls_back_to_search_resolution_for_a_suffixed_symbol(
        self, db_session: Session, test_user: User, wire_tools
    ) -> None:
        # The bare ticker fails direct lookup (no TickerMetadata.market to
        # build ".SW" from); resolve_ticker's yf.Search fallback finds the
        # correctly-suffixed symbol, mirroring what
        # YahooFundamentalsProvider.fetch already does for fundamentals.
        wire_tools({"NESN.SW": _quote(90.0, currency="CHF")}, resolve={"NESN": "NESN.SW"})
        set_current_user_id(test_user.id)

        body = _payload(
            await mcp_server.call_tool("compute_whatif", {"ticker": "NESN", "action": "BUY", "quantity": 2})
        )

        assert body["error"] is None
        assert body["heldInPortfolio"] is False
        assert body["sharesAfter"] == 2.0

    async def test_buy_on_an_unresolvable_ticker_reports_a_graceful_error(
        self, db_session: Session, test_user: User, wire_tools
    ) -> None:
        # Neither the direct nor the resolved attempt finds a price -
        # resolve_ticker's own fallback (unpatched here) returns the
        # symbol unchanged when its search finds nothing, so this must not
        # crash the tool call.
        wire_tools({})
        set_current_user_id(test_user.id)

        result = await mcp_server.call_tool(
            "compute_whatif", {"ticker": "NOTATICKER", "action": "BUY", "quantity": 1}
        )
        body = _payload(result)

        assert "Could not find live price data for NOTATICKER" in body["error"]

    async def test_buy_uses_the_candidate_s_own_currency_for_fx(
        self, db_session: Session, test_user: User, wire_tools, monkeypatch
    ) -> None:
        # No TickerMetadata.native_currency to fall back on - the FX basis
        # must come from the resolved quote itself.
        seen_currency = []
        wire_tools({"NESN.SW": _quote(90.0, currency="CHF")}, resolve={"NESN": "NESN.SW"})
        monkeypatch.setattr(
            "app.services.agent_tools.fetch_fx_rate_to_chf",
            lambda ccy, force_refresh=False: seen_currency.append(ccy) or 1.0,
        )
        set_current_user_id(test_user.id)

        await mcp_server.call_tool("compute_whatif", {"ticker": "NESN", "action": "BUY", "quantity": 1})

        assert seen_currency == ["CHF"]

    async def test_buy_reports_a_rate_limit_distinctly_from_not_found(
        self, db_session: Session, test_user: User, wire_tools
    ) -> None:
        # A 429 from resolve_ticker's yf.Search means Yahoo is throttling
        # requests in general, not that this specific ticker is bogus -
        # must not collapse into the same "could not find" message a
        # genuine typo gets, or the model reports a global outage as if
        # the user asked about a nonexistent company.
        wire_tools({}, resolve_raises=FundamentalsRateLimited("429"))
        set_current_user_id(test_user.id)

        body = _payload(await mcp_server.call_tool("compute_whatif", {"ticker": "NVDA", "action": "BUY", "quantity": 1}))

        assert "rate-limited" in body["error"]
        assert "Could not find" not in body["error"]

    async def test_buy_leaves_an_already_suffixed_candidate_alone(
        self, db_session: Session, test_user: User, wire_tools
    ) -> None:
        # The LLM can now name a non-US ticker already in Yahoo-suffixed
        # form ("NESN.SW") since it isn't limited to held tickers anymore -
        # the "." -> "-" share-class guess must not mangle that into the
        # invalid "NESN-SW" before a direct lookup ever gets a chance.
        wire_tools({"NESN.SW": _quote(90.0, currency="CHF")})
        set_current_user_id(test_user.id)

        body = _payload(
            await mcp_server.call_tool("compute_whatif", {"ticker": "NESN.SW", "action": "BUY", "quantity": 1})
        )

        assert body["error"] is None
        assert body["heldInPortfolio"] is False
