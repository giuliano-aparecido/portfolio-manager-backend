"""Systematic cross-tenant isolation coverage: one user (`owner`) has one
row of every resource type, and every endpoint that can read/write/delete
by ID is exercised as a *different* user (`intruder`) to confirm it 404s
rather than exposing or mutating the owner's data.

Existing route-test files already spot-check this for one or two endpoints
each (e.g. test_routes_portfolio_tickers.py's
test_does_not_leak_another_users_ticker_data). This file exists to cover
the full matrix in one place, so a new endpoint that forgets a user_id
filter is caught immediately rather than depending on someone remembering
to add a one-off regression test for it.
"""

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.dependencies.auth import get_authenticated_user_id
from app.main import app
from app.models import (
    PassiveInvestment,
    PassiveRecurringDeposit,
    PassiveTransaction,
    PortfolioTransaction,
    TickerMetadata,
    User,
)


class OwnerData:
    def __init__(self, owner_id: str, ticker: str, txn_id: int, investment_id: int, passive_txn_id: int) -> None:
        self.owner_id = owner_id
        self.ticker = ticker
        self.txn_id = txn_id
        self.investment_id = investment_id
        self.passive_txn_id = passive_txn_id


@pytest.fixture
def owner_data(db_session: Session) -> OwnerData:
    owner = User(email="isolation-owner@example.com", name="Owner")
    db_session.add(owner)
    db_session.flush()

    db_session.add(TickerMetadata(user_id=owner.id, ticker="AAPL", market="NASDAQ", category="Stock", native_currency="USD"))
    txn = PortfolioTransaction(
        user_id=owner.id, ticker="AAPL", date=datetime(2024, 1, 1, tzinfo=timezone.utc), type="BUY",
        native_currency="USD", quantity=10, price_per_share=100, fx_rate_to_chf=0.9,
    )
    db_session.add(txn)

    investment = PassiveInvestment(user_id=owner.id, name="Owner Fund", type="CASH", currency="USD")
    db_session.add(investment)
    db_session.flush()

    passive_txn = PassiveTransaction(
        passive_investment_id=investment.id, type="DEPOSIT", date=datetime(2024, 1, 1, tzinfo=timezone.utc), amount_native=1000
    )
    db_session.add(passive_txn)
    db_session.add(
        PassiveRecurringDeposit(
            passive_investment_id=investment.id, amount_native=50, start_date=datetime(2024, 1, 1, tzinfo=timezone.utc),
            frequency="MONTHLY", end_date=None, last_generated_date=None,
        )
    )
    db_session.flush()

    return OwnerData(
        owner_id=owner.id, ticker="AAPL", txn_id=txn.id, investment_id=investment.id, passive_txn_id=passive_txn.id
    )


@pytest.fixture
def intruder_client(client: TestClient, db_session: Session):
    """A second, unrelated user - every request through this client is
    authenticated, just as someone who isn't the owner of any of the data
    set up by `owner_data`.
    """
    intruder = User(email="isolation-intruder@example.com", name="Intruder")
    db_session.add(intruder)
    db_session.flush()

    app.dependency_overrides[get_authenticated_user_id] = lambda: intruder.id
    try:
        yield client
    finally:
        app.dependency_overrides.pop(get_authenticated_user_id, None)


class TestPortfolioTickerIsolation:
    def test_get_detail_404s(self, intruder_client: TestClient, owner_data: OwnerData) -> None:
        response = intruder_client.get(f"/portfolio/tickers/{owner_data.ticker}")
        assert response.status_code == 404

    def test_update_404s(self, intruder_client: TestClient, owner_data: OwnerData) -> None:
        response = intruder_client.put(
            f"/portfolio/tickers/{owner_data.ticker}",
            json={"market": "NYSE", "category": "Stock", "nativeCurrency": "USD"},
        )
        assert response.status_code == 404

    def test_delete_404s(self, intruder_client: TestClient, owner_data: OwnerData) -> None:
        response = intruder_client.delete(f"/portfolio/tickers/{owner_data.ticker}")
        assert response.status_code == 404

    def test_list_excludes_owners_ticker(self, intruder_client: TestClient, owner_data: OwnerData) -> None:
        response = intruder_client.get("/portfolio/tickers")
        assert response.status_code == 200
        assert owner_data.ticker not in [row["ticker"] for row in response.json()]


class TestPortfolioTransactionIsolation:
    def test_update_404s(self, intruder_client: TestClient, owner_data: OwnerData) -> None:
        response = intruder_client.put(
            f"/portfolio/transactions/{owner_data.txn_id}",
            json={"type": "BUY", "date": "2024-01-01", "quantity": 5, "pricePerShare": 50},
        )
        assert response.status_code == 404

    def test_delete_404s(self, intruder_client: TestClient, owner_data: OwnerData) -> None:
        response = intruder_client.delete(f"/portfolio/transactions/{owner_data.txn_id}")
        assert response.status_code == 404


class TestPortfolioRollupIsolation:
    def test_rollup_excludes_owners_positions(self, intruder_client: TestClient, owner_data: OwnerData) -> None:
        response = intruder_client.get("/portfolio-rollup")
        assert response.status_code == 200
        body = response.json()
        assert body["openTickers"] == []
        assert body["closedTickers"] == []
        assert body["totalCostBasisCHF"] == 0


class TestPassiveInvestmentIsolation:
    def test_get_detail_404s(self, intruder_client: TestClient, owner_data: OwnerData) -> None:
        response = intruder_client.get(f"/passive-investments/{owner_data.investment_id}")
        assert response.status_code == 404

    def test_update_404s(self, intruder_client: TestClient, owner_data: OwnerData) -> None:
        response = intruder_client.put(
            f"/passive-investments/{owner_data.investment_id}",
            json={"name": "Hijacked", "type": "CASH", "currency": "USD"},
        )
        assert response.status_code == 404

    def test_delete_404s(self, intruder_client: TestClient, owner_data: OwnerData) -> None:
        response = intruder_client.delete(f"/passive-investments/{owner_data.investment_id}")
        assert response.status_code == 404

    def test_list_excludes_owners_investment(self, intruder_client: TestClient, owner_data: OwnerData) -> None:
        response = intruder_client.get("/passive-investments")
        assert response.status_code == 200
        assert owner_data.investment_id not in [row["id"] for row in response.json()]


class TestPassiveTransactionIsolation:
    def test_create_under_owners_investment_404s(self, intruder_client: TestClient, owner_data: OwnerData) -> None:
        # Investment ownership is checked before anything else in this
        # route, so even creating a new transaction under someone else's
        # investment id must 404, not succeed or 400.
        response = intruder_client.post(
            f"/passive-investments/{owner_data.investment_id}/transactions",
            json={"type": "DEPOSIT", "date": "2024-06-01", "amountNative": 100},
        )
        assert response.status_code == 404

    def test_update_404s(self, intruder_client: TestClient, owner_data: OwnerData) -> None:
        response = intruder_client.put(
            f"/passive-investments/{owner_data.investment_id}/transactions/{owner_data.passive_txn_id}",
            json={"type": "DEPOSIT", "date": "2024-06-01", "amountNative": 100},
        )
        assert response.status_code == 404

    def test_delete_404s(self, intruder_client: TestClient, owner_data: OwnerData) -> None:
        response = intruder_client.delete(
            f"/passive-investments/{owner_data.investment_id}/transactions/{owner_data.passive_txn_id}"
        )
        assert response.status_code == 404


class TestPassiveRecurringDepositIsolation:
    def test_create_under_owners_investment_404s(self, intruder_client: TestClient, owner_data: OwnerData) -> None:
        response = intruder_client.post(
            f"/passive-investments/{owner_data.investment_id}/recurring-deposit",
            json={"amountNative": 50, "frequency": "MONTHLY", "startDate": "2024-01-01"},
        )
        assert response.status_code == 404

    def test_update_404s(self, intruder_client: TestClient, owner_data: OwnerData) -> None:
        response = intruder_client.put(
            f"/passive-investments/{owner_data.investment_id}/recurring-deposit",
            json={"amountNative": 999, "frequency": "WEEKLY", "startDate": "2024-01-01"},
        )
        assert response.status_code == 404

    def test_delete_404s(self, intruder_client: TestClient, owner_data: OwnerData) -> None:
        response = intruder_client.delete(f"/passive-investments/{owner_data.investment_id}/recurring-deposit")
        assert response.status_code == 404


class TestPassiveRollupIsolation:
    def test_rollup_excludes_owners_investment(self, intruder_client: TestClient, owner_data: OwnerData) -> None:
        response = intruder_client.get("/passive-rollup")
        assert response.status_code == 200
        body = response.json()
        assert body["rows"] == []
        assert body["totalCostBasisCHF"] == 0
