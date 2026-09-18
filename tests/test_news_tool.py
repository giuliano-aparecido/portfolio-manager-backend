"""The get_ticker_news MCP tool, called through mcp_server.call_tool
against seeded Postgres, with the news feed and the fundamentals provider
both stubbed. Mirrors tests/test_fundamentals_tools.py's wiring.
"""

import datetime
import json
from datetime import datetime as dt
from datetime import timezone

import pytest
from sqlalchemy.orm import Session

import app.services.fundamentals.cache as cache_module
import app.services.news as news_module
from app.mcp_server import mcp_server
from app.models import PortfolioTransaction, TickerMetadata, User
from app.services.agent_context import set_current_user_id
from app.services.fundamentals.base import FundamentalsData, FundamentalsUnavailable


def _payload(result) -> dict:
    return json.loads("".join(getattr(c, "text", "") for c in result.content))


class FakeProvider:
    name = "yahoo"

    def __init__(self, data: dict[str, FundamentalsData], unavailable: set[str] = frozenset()) -> None:
        self._data = data
        self._unavailable = set(unavailable)

    def fetch(self, symbol: str) -> FundamentalsData:
        if symbol in self._unavailable:
            raise FundamentalsUnavailable("no fundamentals for an ETF")
        return self._data[symbol]


NESTLE = FundamentalsData(
    symbol="NESN.SW", company_name="Nestle S.A.", sector="Consumer Defensive", currency="CHF",
    price=88.0, market_cap=230e9, pe_trailing=19.0,
)

TODAY = datetime.date(2026, 9, 18)


class FakeFeed:
    def __init__(self, entries):
        self.entries = entries


def _entry(title: str, published: datetime.date = TODAY):
    return {
        "title": title,
        "link": "https://news.example/story",
        "published": published.strftime("%a, %d %b %Y 12:00:00 GMT"),
        "published_parsed": published.timetuple(),
    }


@pytest.fixture
def wire_tools(db_session: Session, monkeypatch):
    """agent_tools opens its own session (see agent_context.py) — point it
    at the test transaction, and hand back installers for the two upstreams
    this tool touches."""

    def factory() -> Session:
        return Session(bind=db_session.connection(), join_transaction_mode="create_savepoint")

    monkeypatch.setattr("app.services.agent_tools._get_session", factory)

    def install(provider: FakeProvider | None = None, fetch=None, session_wrapper=None):
        if provider is not None:
            monkeypatch.setattr(cache_module, "get_fundamentals_provider", lambda: provider)
        if fetch is not None:
            monkeypatch.setattr(news_module, "_fetch_feed", fetch)
        if session_wrapper is not None:
            # Wraps the test-transaction factory rather than replacing it,
            # so a test can observe session lifetime without losing the
            # rollback-on-teardown behavior.
            monkeypatch.setattr(
                "app.services.agent_tools._get_session", lambda: session_wrapper(factory())
            )

    return install


def _seed_holding(db: Session, user: User, ticker: str, market: str, currency: str) -> None:
    db.add(TickerMetadata(user_id=user.id, ticker=ticker, market=market, category="Stock", native_currency=currency))
    db.add(
        PortfolioTransaction(
            user_id=user.id, ticker=ticker, date=dt(2024, 1, 1, tzinfo=timezone.utc), type="BUY",
            native_currency=currency, quantity=10.0, price_per_share=100.0, fx_rate_to_chf=0.9,
        )
    )
    db.flush()


class TestGetTickerNews:
    async def test_returns_filtered_headlines_for_a_tracked_holding(
        self, db_session: Session, test_user: User, wire_tools
    ) -> None:
        _seed_holding(db_session, test_user, "NESN", "SIX", "CHF")
        calls: list[str] = []

        def fetch(query: str):
            calls.append(query)
            return FakeFeed([
                _entry("Nestle S.A. Shares Sold by Someone - MarketBeat"),
                _entry("Nestle raises full-year outlook - Reuters"),
            ])

        wire_tools(FakeProvider({"NESN.SW": NESTLE}), fetch)
        set_current_user_id(test_user.id)

        body = _payload(await mcp_server.call_tool("get_ticker_news", {"ticker": "nesn"}))

        assert body["ticker"] == "NESN"
        assert body["status"] == "ok"
        assert body["companyName"] == "Nestle S.A."
        assert body["windowDays"] == 7
        assert [i["publisher"] for i in body["items"]] == ["Reuters"]
        assert body["items"][0]["publishedDate"] == TODAY.isoformat()
        # The company name from the fundamentals cache drives the query,
        # not the bare ticker.
        assert calls[0].startswith("Nestle S.A. ")

    async def test_works_for_a_candidate_ticker_not_yet_in_the_portfolio(
        self, db_session: Session, test_user: User, wire_tools
    ) -> None:
        """2026-09-18: a ticker not tracked isn't an error — the agent can
        be asked about a candidate buy too. No TickerMetadata row and no
        fundamentals entry means no company name to drive the query, so it
        searches by the bare ticker instead (same fallback the ETF case
        already exercises)."""
        calls: list[str] = []

        def fetch(query: str):
            calls.append(query)
            # No company name known (no fundamentals) — relevance falls
            # back to the ticker as a standalone, case-sensitive token, so
            # the headline must spell out "TSLA", not "Tesla".
            return FakeFeed([_entry("TSLA wins a new supply contract - Reuters")])

        wire_tools(FakeProvider({}), fetch)
        set_current_user_id(test_user.id)

        body = _payload(await mcp_server.call_tool("get_ticker_news", {"ticker": "tsla"}))

        assert body["ticker"] == "TSLA"
        assert body["status"] == "ok"
        assert body["heldInPortfolio"] is False
        assert body["companyName"] is None
        assert calls[0].startswith("TSLA ")

    async def test_an_etf_without_fundamentals_still_gets_news_by_ticker(
        self, db_session: Session, test_user: User, wire_tools
    ) -> None:
        _seed_holding(db_session, test_user, "VWRA", "SIX", "CHF")
        calls: list[str] = []

        def fetch(query: str):
            calls.append(query)
            return FakeFeed([_entry("VWRA tracker sees record inflows - Financial Times")])

        wire_tools(FakeProvider({}, unavailable={"VWRA.SW"}), fetch)
        set_current_user_id(test_user.id)

        body = _payload(await mcp_server.call_tool("get_ticker_news", {"ticker": "VWRA"}))

        assert body["status"] == "ok"
        assert body["companyName"] is None
        assert calls[0].startswith("VWRA ")

    async def test_reports_no_news_rather_than_junk_when_nothing_qualifies(
        self, db_session: Session, test_user: User, wire_tools
    ) -> None:
        _seed_holding(db_session, test_user, "NESN", "SIX", "CHF")
        wire_tools(
            FakeProvider({"NESN.SW": NESTLE}),
            lambda query: FakeFeed([_entry("Should You Buy Nestle Stock? - Motley Fool")]),
        )
        set_current_user_id(test_user.id)

        body = _payload(await mcp_server.call_tool("get_ticker_news", {"ticker": "NESN"}))

        assert body["status"] == "no_news"
        assert body["items"] == []
        assert body["windowDays"] is None

    async def test_a_feed_failure_is_reported_not_raised(
        self, db_session: Session, test_user: User, wire_tools
    ) -> None:
        _seed_holding(db_session, test_user, "NESN", "SIX", "CHF")

        def boom(query: str):
            raise RuntimeError("connection reset")

        wire_tools(FakeProvider({"NESN.SW": NESTLE}), boom)
        set_current_user_id(test_user.id)

        body = _payload(await mcp_server.call_tool("get_ticker_news", {"ticker": "NESN"}))

        assert body["status"] == "error"
        assert body["items"] == []

    async def test_a_fundamentals_failure_does_not_block_the_news(
        self, db_session: Session, test_user: User, wire_tools, monkeypatch
    ) -> None:
        """The company name is a search-quality nicety; an unrelated
        yfinance outage must not take the news tool down with it."""
        _seed_holding(db_session, test_user, "NESN", "SIX", "CHF")

        def broken_lookup(db, symbols):
            raise RuntimeError("yfinance is down")

        monkeypatch.setattr("app.services.news_service.get_fundamentals", broken_lookup)
        wire_tools(fetch=lambda query: FakeFeed([_entry("NESN wins an award - Reuters")]))
        set_current_user_id(test_user.id)

        body = _payload(await mcp_server.call_tool("get_ticker_news", {"ticker": "NESN"}))

        assert body["status"] == "ok"
        assert body["companyName"] is None

    async def test_the_db_session_is_released_before_the_feed_is_fetched(
        self, db_session: Session, test_user: User, wire_tools
    ) -> None:
        """The escalation can spend ~40s in HTTP; holding a pooled
        connection across it would exhaust a default pool of 5 under a
        handful of concurrent questions. Also keeps the session owned by
        one thread — see news_service's module docstring.
        """
        _seed_holding(db_session, test_user, "NESN", "SIX", "CHF")
        opened: list[Session] = []
        closed: list[Session] = []
        closed_when_fetched: list[bool] = []

        def track(session: Session) -> Session:
            original_close = session.close

            def close_and_record() -> None:
                closed.append(session)
                original_close()

            session.close = close_and_record
            opened.append(session)
            return session

        def fetch(query: str):
            closed_when_fetched.append(bool(opened) and len(closed) == len(opened))
            return FakeFeed([_entry("Nestle raises full-year outlook - Reuters")])

        wire_tools(FakeProvider({"NESN.SW": NESTLE}), fetch, session_wrapper=track)
        set_current_user_id(test_user.id)

        body = _payload(await mcp_server.call_tool("get_ticker_news", {"ticker": "NESN"}))

        assert body["status"] == "ok"
        assert opened, "the tool should have opened a session"
        assert closed_when_fetched == [True]
