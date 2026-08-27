from collections.abc import AsyncIterator
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.main import app
from app.models import PortfolioTransaction, TickerMetadata, User
from app.routers.agent import get_llm_provider
from app.services.llm.base import AgentEvent, AssistantTurn, LLMProvider, ToolCallRequest, ToolResultsTurn, Turn
from app.services.price_service import PriceQuote


class FakeLLMProvider(LLMProvider):
    """Replays one canned event sequence per call to stream_turn, in order —
    the test controls exactly what the "model" does each turn instead of
    calling a real API.
    """

    def __init__(self, turns: list[list[AgentEvent]]) -> None:
        self._turns = turns
        self.calls: list[list[Turn]] = []

    async def stream_turn(self, history, tools, system) -> AsyncIterator[AgentEvent]:
        # Snapshot, not a reference — event_stream() keeps appending to
        # `history` after this call returns (the AssistantTurn from this
        # very turn, then a ToolResultsTurn), so storing the list itself
        # would make every entry in self.calls silently reflect the final
        # post-loop state instead of what history looked like at call time.
        self.calls.append(list(history))
        turn = self._turns[len(self.calls) - 1]
        for event in turn:
            yield event


class RaisingLLMProvider(LLMProvider):
    """Raises instead of yielding any event — covers the generic
    `except Exception` path in _stream_llm_turn (a provider crash, as
    opposed to its own reported AgentEvent(type="error")).
    """

    async def stream_turn(self, history, tools, system) -> AsyncIterator[AgentEvent]:
        raise RuntimeError("boom")
        yield  # pragma: no cover - unreachable; makes this an async generator


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

        # Second turn's history includes the first turn's assistant tool
        # call and its result.
        assert len(provider.calls) == 2
        second_turn_history = provider.calls[1]
        assert isinstance(second_turn_history[-2], AssistantTurn)
        assert second_turn_history[-2].tool_calls[0].name == "get_holdings"
        assert isinstance(second_turn_history[-1], ToolResultsTurn)
        assert second_turn_history[-1].results[0].tool_call_id == "tc1"
        assert "AAPL" in second_turn_history[-1].results[0].content

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

    def test_stops_reading_events_as_soon_as_turn_end_arrives(self, authed_client: TestClient) -> None:
        # A well-behaved provider never yields after turn_end, but the loop
        # shouldn't just happen to work by relying on that - it explicitly
        # breaks on turn_end (app/routers/agent.py), so a stray event after
        # it must never reach the stream even from a misbehaving provider.
        provider = FakeLLMProvider(
            turns=[
                [
                    AgentEvent(type="text_delta", text="Hello!"),
                    AgentEvent(type="turn_end"),
                    AgentEvent(type="text_delta", text="should never be sent"),
                ]
            ]
        )
        override_llm_provider(provider)

        response = authed_client.post("/agent/ask", json={"messages": [{"role": "user", "content": "hi"}]})

        assert response.status_code == 200
        assert "should never be sent" not in response.text
        assert "event: done" in response.text

    def test_provider_reported_error_yields_exactly_one_error_frame(self, authed_client: TestClient) -> None:
        provider = FakeLLMProvider(turns=[[AgentEvent(type="error", error_message="boom")]])
        override_llm_provider(provider)

        response = authed_client.post("/agent/ask", json={"messages": [{"role": "user", "content": "hi"}]})

        assert response.status_code == 200
        assert response.text.count("event: error") == 1
        assert "boom" in response.text
        assert "event: done" not in response.text

    def test_provider_exception_yields_exactly_one_error_frame(self, authed_client: TestClient) -> None:
        override_llm_provider(RaisingLLMProvider())

        response = authed_client.post("/agent/ask", json={"messages": [{"role": "user", "content": "hi"}]})

        assert response.status_code == 200
        assert response.text.count("event: error") == 1
        assert "event: done" not in response.text


class TestAgentAskToolCallFailure:
    def test_a_raising_tool_yields_a_graceful_error_frame_not_a_truncated_stream(
        self, authed_client: TestClient, db_session: Session, test_user: User, agent_session_factory, monkeypatch
    ) -> None:
        # compute_whatif's live price/FX fetch isn't wrapped in the
        # per-ticker try/except that get_holdings/get_allocation get from
        # compute_portfolio_rollup - a raise here must still surface as a
        # clean SSE error frame, not silently truncate the stream.
        add_ticker_and_buy(db_session, test_user)

        def raise_price_error(yahoo_ticker: str, force_refresh: bool = False) -> PriceQuote:
            raise RuntimeError("No valid price from Yahoo Finance for AAPL")

        monkeypatch.setattr("app.services.agent_tools.fetch_current_price", raise_price_error)

        provider = FakeLLMProvider(
            turns=[
                [
                    AgentEvent(
                        type="tool_call_start",
                        tool_call=ToolCallRequest(
                            id="tc1",
                            name="compute_whatif",
                            input={"ticker": "AAPL", "action": "SELL", "quantity": 1},
                        ),
                    ),
                    AgentEvent(type="turn_end"),
                ]
            ]
        )
        override_llm_provider(provider)

        response = authed_client.post("/agent/ask", json={"messages": [{"role": "user", "content": "sell 1 AAPL?"}]})

        assert response.status_code == 200
        body = response.text
        assert "event: tool_call" in body
        assert "event: error" in body
        assert "unexpected error running a tool" in body
        assert "event: done" not in body
        assert "event: tool_result" not in body


class TestAgentAskRateLimiting:
    def test_returns_429_after_exceeding_the_six_per_minute_limit(self, authed_client: TestClient) -> None:
        provider = FakeLLMProvider(turns=[[AgentEvent(type="text_delta", text="hi"), AgentEvent(type="turn_end")]] * 10)
        override_llm_provider(provider)

        for _ in range(6):
            response = authed_client.post("/agent/ask", json={"messages": [{"role": "user", "content": "hi"}]})
            assert response.status_code == 200

        response = authed_client.post("/agent/ask", json={"messages": [{"role": "user", "content": "hi"}]})
        assert response.status_code == 429
