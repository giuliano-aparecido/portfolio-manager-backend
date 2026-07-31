"""Proves the advisory-lock fix in app/services/locking.py actually
serializes the three read-validate-write races the Fable 5 review flagged
(portfolio transactions, passive transactions, recurring-deposit
materialization) instead of asserting it in the abstract.

Deliberately bypasses the shared db_session/client fixtures from
conftest.py: those all share one connection/transaction so two "requests"
through them never really run concurrently. Each test here opens its own
independent engine connections (real, separate Postgres transactions) and
drives two real threads through the actual router/service functions
directly — router functions are plain Python functions once you supply
real args, so this needs no HTTP layer. Setup/teardown use their own
short-lived connections and clean up explicitly, since nothing here is
wrapped in the rollback-on-teardown fixture.
"""

import threading
from datetime import datetime, timezone

import pytest
from sqlalchemy.orm import Session

from app.db.session import engine
from app.exceptions import AppError
from app.models import (
    PassiveInvestment,
    PassiveRecurringDeposit,
    PassiveTransaction,
    PortfolioTransaction,
    TickerMetadata,
    User,
)
from app.routers.passive_transactions import create_passive_transaction
from app.routers.portfolio_transactions import create_transaction
from app.schemas.passive import PassiveTransactionCreateRequest
from app.schemas.portfolio import TransactionCreateRequest
from app.services.recurring import materialize_due_recurring_deposits


@pytest.fixture
def setup_session():
    conn = engine.connect()
    session = Session(bind=conn)
    yield session
    session.close()
    conn.close()


def _delete_user_cascade(user_id: str) -> None:
    """DB-level ON DELETE CASCADE (verified in models/*.py) takes care of
    every dependent row transitively, so deleting the user is enough.
    """
    conn = engine.connect()
    session = Session(bind=conn)
    try:
        session.query(User).filter(User.id == user_id).delete()
        session.commit()
    finally:
        session.close()
        conn.close()


class TestPortfolioTransactionRace:
    def test_two_concurrent_sells_cannot_jointly_oversell(self, setup_session: Session, monkeypatch) -> None:
        user = User(email="race-portfolio@example.com", name="Race Test")
        setup_session.add(user)
        setup_session.flush()
        setup_session.add(
            TickerMetadata(user_id=user.id, ticker="RACE", market="NASDAQ", category="Stock", native_currency="USD")
        )
        setup_session.add(
            PortfolioTransaction(
                user_id=user.id, ticker="RACE", date=datetime(2024, 1, 1, tzinfo=timezone.utc), type="BUY",
                native_currency="USD", quantity=10, price_per_share=100, fx_rate_to_chf=0.9,
            )
        )
        user_id = user.id
        setup_session.commit()

        try:
            monkeypatch.setattr("app.routers.portfolio_transactions.fetch_historical_fx_rate", lambda ccy, date: 0.9)

            barrier = threading.Barrier(2)
            results: dict[str, tuple[str, str]] = {}

            def sell(label: str) -> None:
                conn = engine.connect()
                session = Session(bind=conn)
                try:
                    body = TransactionCreateRequest(
                        ticker="RACE", type="SELL", date="2024-06-01", quantity=8, pricePerShare=110
                    )
                    barrier.wait()
                    try:
                        row = create_transaction(body=body, db=session, user_id=user_id)
                        results[label] = ("ok", str(row.id))
                    except AppError as exc:
                        results[label] = ("error", exc.message)
                finally:
                    session.close()
                    conn.close()

            t1 = threading.Thread(target=sell, args=("a",))
            t2 = threading.Thread(target=sell, args=("b",))
            t1.start()
            t2.start()
            t1.join(timeout=15)
            t2.join(timeout=15)

            outcomes = [results["a"], results["b"]]
            successes = [o for o in outcomes if o[0] == "ok"]
            failures = [o for o in outcomes if o[0] == "error"]
            assert len(successes) == 1, f"expected exactly one concurrent SELL to succeed, got {outcomes}"
            assert len(failures) == 1
            assert "shares" in failures[0][1].lower() or "sell" in failures[0][1].lower()
        finally:
            _delete_user_cascade(user_id)


class TestPassiveTransactionRace:
    def test_two_concurrent_withdrawals_cannot_jointly_overdraw(self, setup_session: Session) -> None:
        user = User(email="race-passive@example.com", name="Race Test")
        setup_session.add(user)
        setup_session.flush()
        inv = PassiveInvestment(user_id=user.id, name="Race Fund", type="CASH", currency="USD")
        setup_session.add(inv)
        setup_session.flush()
        setup_session.add(
            PassiveTransaction(
                passive_investment_id=inv.id, type="DEPOSIT", date=datetime(2024, 1, 1, tzinfo=timezone.utc),
                amount_native=1000,
            )
        )
        user_id = user.id
        inv_id = inv.id
        setup_session.commit()

        try:
            barrier = threading.Barrier(2)
            results: dict[str, tuple[str, str]] = {}

            def withdraw(label: str) -> None:
                conn = engine.connect()
                session = Session(bind=conn)
                try:
                    body = PassiveTransactionCreateRequest(type="WITHDRAWAL", date="2024-06-01", amountNative=800)
                    barrier.wait()
                    try:
                        row = create_passive_transaction(investment_id=str(inv_id), body=body, db=session, user_id=user_id)
                        results[label] = ("ok", str(row.id))
                    except AppError as exc:
                        results[label] = ("error", exc.message)
                finally:
                    session.close()
                    conn.close()

            t1 = threading.Thread(target=withdraw, args=("a",))
            t2 = threading.Thread(target=withdraw, args=("b",))
            t1.start()
            t2.start()
            t1.join(timeout=15)
            t2.join(timeout=15)

            outcomes = [results["a"], results["b"]]
            successes = [o for o in outcomes if o[0] == "ok"]
            failures = [o for o in outcomes if o[0] == "error"]
            assert len(successes) == 1, f"expected exactly one concurrent WITHDRAWAL to succeed, got {outcomes}"
            assert len(failures) == 1
            assert "negative" in failures[0][1].lower()
        finally:
            _delete_user_cascade(user_id)


class TestRecurringDepositMaterializationRace:
    def test_two_concurrent_materialize_calls_do_not_double_insert(self, setup_session: Session) -> None:
        user = User(email="race-recurring@example.com", name="Race Test")
        setup_session.add(user)
        setup_session.flush()
        inv = PassiveInvestment(user_id=user.id, name="Recurring Fund", type="CASH", currency="USD")
        setup_session.add(inv)
        setup_session.flush()
        setup_session.add(
            PassiveRecurringDeposit(
                passive_investment_id=inv.id, amount_native=100, start_date=datetime(2024, 1, 1, tzinfo=timezone.utc),
                frequency="MONTHLY", end_date=None, last_generated_date=None,
            )
        )
        user_id = user.id
        inv_id = inv.id
        setup_session.commit()

        try:
            barrier = threading.Barrier(2)

            def materialize() -> None:
                conn = engine.connect()
                session = Session(bind=conn)
                try:
                    barrier.wait()
                    materialize_due_recurring_deposits(
                        session, passive_investment_id=inv_id, until=datetime(2024, 6, 1, tzinfo=timezone.utc)
                    )
                finally:
                    session.close()
                    conn.close()

            t1 = threading.Thread(target=materialize)
            t2 = threading.Thread(target=materialize)
            t1.start()
            t2.start()
            t1.join(timeout=15)
            t2.join(timeout=15)

            verify_conn = engine.connect()
            verify = Session(bind=verify_conn)
            try:
                generated = (
                    verify.query(PassiveTransaction)
                    .filter(PassiveTransaction.passive_investment_id == inv_id, PassiveTransaction.type == "DEPOSIT")
                    .all()
                )
            finally:
                verify.close()
                verify_conn.close()

            # Jan through Jun 1st, monthly = 6 occurrences. Without the
            # lock, both threads independently see last_generated_date=None
            # and each insert all 6, producing 12.
            assert len(generated) == 6, f"expected exactly 6 materialized deposits (no double-insert), got {len(generated)}"
        finally:
            _delete_user_cascade(user_id)
