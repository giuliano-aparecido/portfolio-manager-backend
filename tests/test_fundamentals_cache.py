"""get_fundamentals() — the once-per-UTC-day Postgres cache, 429-only
retries, and portfolio-composition sweep. Uses the real transactional
Postgres session (db_session) like the rest of the suite.
"""

from datetime import date

import pytest
from sqlalchemy.orm import Session

import app.services.fundamentals.cache as cache_module
from app.models import TickerFundamentalsCache, TickerMetadata, User
from app.services.fundamentals.base import (
    FundamentalsData,
    FundamentalsRateLimited,
    FundamentalsUnavailable,
)
from app.services.fundamentals.cache import get_fundamentals

DAY1 = date(2026, 9, 1)
DAY2 = date(2026, 9, 2)


def _fd(symbol: str, pe: float = 20.0) -> FundamentalsData:
    return FundamentalsData(symbol=symbol, pe_trailing=pe, market_cap=1e9, price=100.0)


class FakeProvider:
    """`script[symbol]` is a list of actions consumed left-to-right; the
    last one repeats. Actions: "ok" | "429" | "unavailable" | "error".
    """

    name = "yahoo"

    def __init__(self, script: dict[str, list[str]] | None = None, default: str = "ok") -> None:
        self.script = {k: list(v) for k, v in (script or {}).items()}
        self.default = default
        self.calls: list[str] = []

    def fetch(self, symbol: str) -> FundamentalsData:
        self.calls.append(symbol)
        actions = self.script.get(symbol)
        action = self.default if not actions else (actions.pop(0) if len(actions) > 1 else actions[0])
        if action == "429":
            raise FundamentalsRateLimited("429 Too Many Requests")
        if action == "unavailable":
            raise FundamentalsUnavailable("etf")
        if action == "error":
            raise RuntimeError("boom")
        return _fd(symbol)


@pytest.fixture
def no_sleep():
    return lambda _seconds: None


def _rows(db: Session) -> dict[str, TickerFundamentalsCache]:
    return {r.yahoo_symbol: r for r in db.query(TickerFundamentalsCache).all()}


class TestDailyGate:
    def test_first_call_fetches_and_persists(self, db_session: Session, no_sleep):
        provider = FakeProvider()
        result = get_fundamentals(db_session, ["AAPL"], provider=provider, today=DAY1, sleep=no_sleep)

        assert provider.calls == ["AAPL"]
        assert result["AAPL"].data.pe_trailing == 20.0
        assert result["AAPL"].as_of_date == DAY1
        row = _rows(db_session)["AAPL"]
        assert row.as_of_date == DAY1
        assert row.payload["pe_trailing"] == 20.0
        assert row.fetch_error is None

    def test_second_call_same_day_serves_from_db_without_refetch(self, db_session: Session, no_sleep):
        provider = FakeProvider()
        get_fundamentals(db_session, ["AAPL"], provider=provider, today=DAY1, sleep=no_sleep)
        cache_module.clear_fundamentals_cache()  # force the DB path, not the in-process layer

        result = get_fundamentals(db_session, ["AAPL"], provider=provider, today=DAY1, sleep=no_sleep)

        assert provider.calls == ["AAPL"]
        assert result["AAPL"].data.pe_trailing == 20.0
        assert result["AAPL"].stale is False

    def test_next_utc_day_refetches(self, db_session: Session, no_sleep):
        provider = FakeProvider()
        get_fundamentals(db_session, ["AAPL"], provider=provider, today=DAY1, sleep=no_sleep)
        cache_module.clear_fundamentals_cache()

        get_fundamentals(db_session, ["AAPL"], provider=provider, today=DAY2, sleep=no_sleep)

        assert provider.calls == ["AAPL", "AAPL"]

    def test_only_missing_symbols_are_fetched_when_others_are_fresh(self, db_session: Session, no_sleep):
        provider = FakeProvider()
        get_fundamentals(db_session, ["AAPL"], provider=provider, today=DAY1, sleep=no_sleep)
        cache_module.clear_fundamentals_cache()
        provider.calls.clear()

        result = get_fundamentals(db_session, ["AAPL", "MSFT"], provider=provider, today=DAY1, sleep=no_sleep)

        assert provider.calls == ["MSFT"]
        assert result["AAPL"].data is not None
        assert result["MSFT"].data is not None

    def test_force_refresh_bypasses_the_daily_gate(self, db_session: Session, no_sleep):
        provider = FakeProvider()
        get_fundamentals(db_session, ["AAPL"], provider=provider, today=DAY1, sleep=no_sleep)
        cache_module.clear_fundamentals_cache()

        get_fundamentals(db_session, ["AAPL"], provider=provider, today=DAY1, force_refresh=True, sleep=no_sleep)

        assert provider.calls == ["AAPL", "AAPL"]

    def test_rows_from_a_different_provider_are_ignored(self, db_session: Session, no_sleep):
        db_session.add(
            TickerFundamentalsCache(
                provider="legacy", yahoo_symbol="AAPL",
                payload={"symbol": "AAPL", "pe_trailing": 9.9}, as_of_date=DAY1, unavailable=False,
            )
        )
        db_session.flush()
        provider = FakeProvider()

        result = get_fundamentals(db_session, ["AAPL"], provider=provider, today=DAY1, sleep=no_sleep)

        assert provider.calls == ["AAPL"]
        assert result["AAPL"].data.pe_trailing == 20.0


class TestRateLimitRetry:
    def test_retries_only_the_throttled_symbol(self, db_session: Session, no_sleep):
        provider = FakeProvider(script={"MSFT": ["429", "ok"]})

        result = get_fundamentals(db_session, ["AAPL", "MSFT"], provider=provider, today=DAY1, sleep=no_sleep)

        assert provider.calls.count("AAPL") == 1  # never retried — it succeeded first time
        assert provider.calls.count("MSFT") == 2  # initial 429 + one retry
        assert result["AAPL"].data is not None
        assert result["MSFT"].data is not None
        assert result["MSFT"].as_of_date == DAY1

    def test_exhausted_retries_mark_the_row_without_bumping_as_of_date(self, db_session: Session, no_sleep):
        provider = FakeProvider(script={"MSFT": ["429"]})  # always throttled

        result = get_fundamentals(db_session, ["MSFT"], provider=provider, today=DAY1, sleep=no_sleep)

        assert len(provider.calls) == 1 + len(cache_module._RETRY_BACKOFF)
        assert result["MSFT"].data is None
        assert result["MSFT"].error == "rate_limited"
        row = _rows(db_session)["MSFT"]
        assert row.fetch_error == "rate_limited"
        assert row.as_of_date is None

    def test_exhausted_retries_serve_stale_payload_when_one_exists(self, db_session: Session, no_sleep):
        get_fundamentals(db_session, ["AAPL"], provider=FakeProvider(), today=DAY1, sleep=no_sleep)
        cache_module.clear_fundamentals_cache()

        result = get_fundamentals(
            db_session, ["AAPL"], provider=FakeProvider(script={"AAPL": ["429"]}), today=DAY2, sleep=no_sleep
        )

        assert result["AAPL"].stale is True
        assert result["AAPL"].data.pe_trailing == 20.0
        assert result["AAPL"].error == "rate_limited"

    def test_successful_symbols_are_committed_before_the_retry_loop(self, db_session: Session, no_sleep):
        # A 429 on MSFT must not cost AAPL its already-written row.
        provider = FakeProvider(script={"MSFT": ["429"]})
        get_fundamentals(db_session, ["AAPL", "MSFT"], provider=provider, today=DAY1, sleep=no_sleep)

        rows = _rows(db_session)
        assert rows["AAPL"].as_of_date == DAY1
        assert rows["MSFT"].fetch_error == "rate_limited"


class TestUnavailableAndErrors:
    def test_unavailable_is_cached_for_the_day(self, db_session: Session, no_sleep):
        provider = FakeProvider(script={"VWRA.SW": ["unavailable"]})
        result = get_fundamentals(db_session, ["VWRA.SW"], provider=provider, today=DAY1, sleep=no_sleep)
        assert result["VWRA.SW"].unavailable is True

        cache_module.clear_fundamentals_cache()
        result2 = get_fundamentals(db_session, ["VWRA.SW"], provider=provider, today=DAY1, sleep=no_sleep)

        assert provider.calls == ["VWRA.SW"]  # not retried
        assert result2["VWRA.SW"].unavailable is True

    def test_transient_error_is_retried_on_the_next_call(self, db_session: Session, no_sleep):
        provider = FakeProvider(script={"AAPL": ["error", "ok"]})
        result = get_fundamentals(db_session, ["AAPL"], provider=provider, today=DAY1, sleep=no_sleep)
        assert result["AAPL"].data is None
        assert result["AAPL"].error

        cache_module.clear_fundamentals_cache()
        result2 = get_fundamentals(db_session, ["AAPL"], provider=provider, today=DAY1, sleep=no_sleep)

        assert provider.calls == ["AAPL", "AAPL"]
        assert result2["AAPL"].data is not None


class TestAdvisoryLockAndMemoryLayer:
    def test_write_path_locks_but_the_all_fresh_path_does_not(self, db_session: Session, monkeypatch, no_sleep):
        locked: list[str] = []
        monkeypatch.setattr(cache_module, "_advisory_lock", lambda db, name: locked.append(name))
        provider = FakeProvider()

        get_fundamentals(db_session, ["AAPL"], provider=provider, today=DAY1, sleep=no_sleep)
        assert locked == ["yahoo"]

        cache_module.clear_fundamentals_cache()
        get_fundamentals(db_session, ["AAPL"], provider=provider, today=DAY1, sleep=no_sleep)
        assert locked == ["yahoo"]  # DB row is fresh — no second lock

    def test_in_process_layer_short_circuits_a_repeat_within_ttl(self, db_session: Session, monkeypatch, no_sleep):
        provider = FakeProvider()
        get_fundamentals(db_session, ["AAPL"], provider=provider, today=DAY1, sleep=no_sleep)

        def boom(*_args):
            raise AssertionError("must not reach the DB write path within the TTL")

        monkeypatch.setattr(cache_module, "_advisory_lock", boom)
        result = get_fundamentals(db_session, ["AAPL"], provider=provider, today=DAY1, sleep=no_sleep)

        assert result["AAPL"].data is not None
        assert provider.calls == ["AAPL"]


class TestPortfolioCompositionSweep:
    def test_sweep_deletes_rows_for_symbols_no_user_holds(self, db_session: Session, test_user: User, no_sleep):
        db_session.add(
            TickerMetadata(
                user_id=test_user.id, ticker="AAPL", market="NASDAQ", category="Stock", native_currency="USD"
            )
        )
        db_session.add(
            TickerMetadata(user_id=test_user.id, ticker="NESN", market="SIX", category="Stock", native_currency="CHF")
        )
        db_session.add(
            TickerFundamentalsCache(
                provider="yahoo", yahoo_symbol="NESN.SW", payload={"symbol": "NESN.SW"},
                as_of_date=DAY1, unavailable=False,
            )
        )
        db_session.add(
            TickerFundamentalsCache(
                provider="yahoo", yahoo_symbol="OLD", payload={"symbol": "OLD"}, as_of_date=DAY1, unavailable=False
            )
        )
        db_session.flush()

        get_fundamentals(db_session, ["AAPL"], provider=FakeProvider(), today=DAY2, sleep=no_sleep)

        symbols = set(_rows(db_session))
        assert "OLD" not in symbols  # not held by anyone -> swept
        assert "NESN.SW" in symbols  # still held -> kept
        assert "AAPL" in symbols  # just fetched

    def test_sweep_does_not_run_on_the_all_fresh_path(self, db_session: Session, test_user: User, monkeypatch, no_sleep):
        db_session.add(
            TickerMetadata(
                user_id=test_user.id, ticker="AAPL", market="NASDAQ", category="Stock", native_currency="USD"
            )
        )
        db_session.flush()
        get_fundamentals(db_session, ["AAPL"], provider=FakeProvider(), today=DAY1, sleep=no_sleep)
        cache_module.clear_fundamentals_cache()

        def boom(*_args, **_kwargs):
            raise AssertionError("sweep should only run inside the refresh path")

        monkeypatch.setattr(cache_module, "_sweep_unheld", boom)
        get_fundamentals(db_session, ["AAPL"], provider=FakeProvider(), today=DAY1, sleep=no_sleep)
