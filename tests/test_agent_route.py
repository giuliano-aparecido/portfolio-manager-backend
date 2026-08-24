from collections.abc import AsyncIterator
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.main import app
from app.models import PortfolioTransaction, TickerMetadata, User
from app.routers.agent import get_llm_provider
from app.services.llm.base import AgentEvent, LLMProvider, ToolCallRequest
from app.services.price_service import PriceQuote


class FakeLLMProvider(LLMProvider):
    """Replays one canned event sequence per call to stream_turn, in order —
    the test controls exactly what the "model" does each turn instead of
    calling a real API.
    """

    def __init__(self, turns: list[list[AgentEvent]]) -> None:
        self._turns = turns
        self.calls: list[list[dict]] = []

    async def stream_turn(self, messages, tools, system) -> AsyncIterator[AgentEvent]:
        self.calls.append(messages)
        turn = self._turns[len(self.calls) - 1]
        for event in turn:
            yield event


def add_ticker_and_buy(db_session: Session, user: User, ticker: str = "AAPL") -> None:
    db_session.add(TickerMetadata(user_id=user.id, ticker=ticker, market="NASDAQ", category="Stock", native_currency="USD"))
    db_session.add(
        PortfolioTransaction(
            user_id=user.id, ticker=ticker, date=datetime(2024, 1, 1, tzinfo=timezone.utc), type="BUY",
            native_currency="USD", quantity=10, price_per_share=100, fx_rate_to_chf=0.9,
        )
    )
    db_session.flush()


@pytest.fixture
def agent_session_factory(db_session: Session, monkeypatch):
    """agent_tools.py's tool functions open their own DB session (they run
    outside FastAPI's dependency system — see agent_context.py's docstring)
    — this points that session factory at the same transactional connection
    as db_session, so seeded fixture data is visible to a tool call.
    """

    def factory() -> Session:
        return Session(bind=db_session.connection(), join_transaction_mode="create_savepoint")

    monkeypatch.setattr("app.services.agent_tools._get_session", factory)
    return factory


def override_llm_provider(provider: LLMProvider):
    app.dependency_overrides[get_llm_provider] = lambda: provider


@pytest.fixture(autouse=True)
def _clear_llm_override():
    yield
    app.dependency_overrides.pop(get_llm_provider, None)


class TestAgentAskHappyPath:
    def test_streams_text_then_dispatches_a_real_tool_call_then_answers(
        self, authed_client: TestClient, db_session: Session, test_user: User, agent_session_factory, monkeypatch
    ) -> None:
        add_ticker_and_buy(db_session, test_user)
        monkeypatch.setattr(
            "app.services.portfolio_rollup_service.fetch_current_price",
            lambda yahoo_ticker, force_refresh=False: PriceQuote(
                price=150.0, currency="USD", timestamp=datetime.now(timezone.utc), source="yahoo",
                daily_change_percent=1.0, daily_change=1.5,
            ),
        )
        monkeypatch.setattr(
            "app.services.portfolio_rollup_service.fetch_fx_rate_to_chf", lambda ccy, force_refresh=False: 0.9
        )

        provider = FakeLLMProvider(
            turns=[
                [
                    AgentEvent(type="text_delta", text="Checking your holdings..."),
                    AgentEvent(
                        type="tool_call_start",
                        tool_call=ToolCallRequest(id="tc1", name="get_holdings", input={}),
                    ),
                    AgentEvent(type="turn_end"),
                ],
                [
                    AgentEvent(type="text_delta", text="You hold 10 shares of AAPL."),
                    AgentEvent(type="turn_end"),
                ],
            ]
        )
        override_llm_provider(provider)

        response = authed_client.post("/agent/ask", json={"messages": [{"role": "user", "content": "What do I hold?"}]})

        assert response.status_code == 200
        body = response.text
        assert "event: token" in body
        assert "Checking your holdings" in body
        assert "event: tool_call" in body
        assert '"name": "get_holdings"' in body
        assert "event: tool_result" in body
        assert "event: done" in body
        # The tool call actually hit real seeded data, not a stub.
        assert "AAPL" in body

        # Second turn's messages include the first turn's tool_result.
        assert len(provider.calls) == 2
        second_turn_messages = provider.calls[1]
        assert second_turn_messages[-1]["role"] == "user"
        assert second_turn_messages[-1]["content"][0]["type"] == "tool_result"

    def test_no_tool_call_returns_text_and_done_in_one_turn(self, authed_client: TestClient) -> None:
        provider = FakeLLMProvider(
            turns=[[AgentEvent(type="text_delta", text="Hello!"), AgentEvent(type="turn_end")]]
        )
        override_llm_provider(provider)

        response = authed_client.post("/agent/ask", json={"messages": [{"role": "user", "content": "hi"}]})

        assert response.status_code == 200
        assert "event: token" in response.text
        assert "event: done" in response.text
        assert len(provider.calls) == 1


class TestAgentAskRateLimiting:
    def test_returns_429_after_exceeding_the_six_per_minute_limit(self, authed_client: TestClient) -> None:
        provider = FakeLLMProvider(turns=[[AgentEvent(type="text_delta", text="hi"), AgentEvent(type="turn_end")]] * 10)
        override_llm_provider(provider)

        for _ in range(6):
            response = authed_client.post("/agent/ask", json={"messages": [{"role": "user", "content": "hi"}]})
            assert response.status_code == 200

        response = authed_client.post("/agent/ask", json={"messages": [{"role": "user", "content": "hi"}]})
        assert response.status_code == 429
