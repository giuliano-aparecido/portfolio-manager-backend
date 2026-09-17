from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.dependencies.auth import get_authenticated_user_id
from app.exceptions import UnauthorizedError
from app.main import app
from app.models import PortfolioTransaction, TickerMetadata, User
from app.services.price_service import PriceQuote


@pytest.fixture
def unauthenticated_client(client: TestClient):
    """Simulates an unauthenticated production request — overrides the
    auth dependency to raise UnauthorizedError directly, rather than
    relying on dev/test mode's real auto-provision behavior (which would
    otherwise mask the very thing these regression tests check: that each
    route lets an auth failure propagate to a 401, not something else).
    """
    app.dependency_overrides[get_authenticated_user_id] = _raise_unauthorized
    try:
        yield client
    finally:
        app.dependency_overrides.pop(get_authenticated_user_id, None)


def _raise_unauthorized():
    raise UnauthorizedError()


class TestListAndCreateTickers:
    def test_create_ticker_with_valid_input(self, authed_client: TestClient) -> None:
        response = authed_client.post(
            "/portfolio/tickers", json={"ticker": "aapl", "market": "nasdaq", "category": "Stock", "nativeCurrency": "usd"}
        )
        assert response.status_code == 201
        body = response.json()
        assert body["ticker"] == "AAPL"
        assert body["market"] == "NASDAQ"
        assert body["nativeCurrency"] == "USD"

    def test_returns_400_if_ticker_missing(self, authed_client: TestClient) -> None:
        response = authed_client.post(
            "/portfolio/tickers", json={"market": "NASDAQ", "category": "Stock", "nativeCurrency": "USD"}
        )
        assert response.status_code == 400
        assert "ticker is required" in response.json()["error"]

    def test_returns_400_for_invalid_market(self, authed_client: TestClient) -> None:
        response = authed_client.post(
            "/portfolio/tickers", json={"ticker": "AAPL", "market": "MOON", "category": "Stock", "nativeCurrency": "USD"}
        )
        assert response.status_code == 400
        assert "market must be one of" in response.json()["error"]

    def test_returns_409_on_duplicate_ticker_for_same_user(self, authed_client: TestClient) -> None:
        body = {"ticker": "AAPL", "market": "NASDAQ", "category": "Stock", "nativeCurrency": "USD"}
        first = authed_client.post("/portfolio/tickers", json=body)
        assert first.status_code == 201

        second = authed_client.post("/portfolio/tickers", json=body)
        assert second.status_code == 409
        assert "already registered" in second.json()["error"]

    def test_list_returns_only_the_authenticated_users_tickers(
        self, authed_client: TestClient, db_session: Session, test_user: User
    ) -> None:
        other_user = User(email="other@example.com")
        db_session.add(other_user)
        db_session.flush()
        db_session.add(TickerMetadata(user_id=other_user.id, ticker="MSFT", market="NASDAQ", category="Stock", native_currency="USD"))
        db_session.add(TickerMetadata(user_id=test_user.id, ticker="AAPL", market="NASDAQ", category="Stock", native_currency="USD"))
        db_session.flush()

        response = authed_client.get("/portfolio/tickers")
        assert response.status_code == 200
        tickers = [row["ticker"] for row in response.json()]
        assert tickers == ["AAPL"]


class TestGetTickerDetail:
    def test_returns_404_for_unregistered_ticker(self, authed_client: TestClient) -> None:
        response = authed_client.get("/portfolio/tickers/AAPL")
        assert response.status_code == 404
        assert "not registered" in response.json()["error"]

    def test_returns_200_with_closed_position_detail_no_price_fetch_needed(
        self, authed_client: TestClient, db_session: Session, test_user: User
    ) -> None:
        db_session.add(TickerMetadata(user_id=test_user.id, ticker="AAPL", market="NASDAQ", category="Stock", native_currency="USD"))
        db_session.add(
            PortfolioTransaction(
                user_id=test_user.id,
                ticker="AAPL",
                date=datetime(2024, 1, 1, tzinfo=timezone.utc),
                type="BUY",
                native_currency="USD",
                quantity=10,
                price_per_share=100,
                fx_rate_to_chf=0.9,
            )
        )
        db_session.add(
            PortfolioTransaction(
                user_id=test_user.id,
                ticker="AAPL",
                date=datetime(2024, 2, 1, tzinfo=timezone.utc),
                type="SELL",
                native_currency="USD",
                quantity=10,
                price_per_share=120,
                fx_rate_to_chf=0.9,
            )
        )
        db_session.flush()

        response = authed_client.get("/portfolio/tickers/AAPL")
        assert response.status_code == 200
        body = response.json()
        assert body["currentShares"] == 0
        assert body["marketValueNative"] is None  # closed position — no live price attempted
        assert body["totalRealizedGainNative"] == 200  # (120-100)*10

    def test_same_date_lots_are_fifo_consumed_and_displayed_in_ascending_id_order(
        self, authed_client: TestClient, db_session: Session, test_user: User, monkeypatch
    ) -> None:
        """Regression test for the fixed non-deterministic same-date
        tiebreak (see fifo.py's docstring): two BUY lots sharing the exact
        same date must be FIFO-consumed in ascending-id (insertion) order,
        and the displayed transaction list must show that same order —
        not whatever order Postgres happens to return rows in.
        """
        db_session.add(TickerMetadata(user_id=test_user.id, ticker="AAPL", market="NASDAQ", category="Stock", native_currency="USD"))
        same_date = datetime(2024, 1, 1, tzinfo=timezone.utc)

        cheap_lot = PortfolioTransaction(
            user_id=test_user.id, ticker="AAPL", date=same_date, type="BUY",
            native_currency="USD", quantity=5, price_per_share=100, fx_rate_to_chf=1.0,
        )
        db_session.add(cheap_lot)
        db_session.flush()  # assigns the lower id — inserted (and "happened") first

        expensive_lot = PortfolioTransaction(
            user_id=test_user.id, ticker="AAPL", date=same_date, type="BUY",
            native_currency="USD", quantity=5, price_per_share=200, fx_rate_to_chf=1.0,
        )
        db_session.add(expensive_lot)
        db_session.flush()

        partial_sell = PortfolioTransaction(
            user_id=test_user.id, ticker="AAPL", date=same_date, type="SELL",
            native_currency="USD", quantity=5, price_per_share=300, fx_rate_to_chf=1.0,
        )
        db_session.add(partial_sell)
        db_session.flush()

        monkeypatch.setattr(
            "app.services.ticker_detail.fetch_current_price",
            lambda yahoo_ticker: PriceQuote(
                price=300.0, currency="USD", timestamp=datetime.now(timezone.utc), source="yahoo",
                daily_change_percent=0.0, daily_change=0.0,
            ),
        )
        monkeypatch.setattr("app.services.ticker_detail.fetch_fx_rate_to_chf", lambda ccy: 1.0)

        response = authed_client.get("/portfolio/tickers/AAPL")
        assert response.status_code == 200
        body = response.json()

        # FIFO must have consumed the cheaper, earlier-inserted lot (id of
        # cheap_lot) first: gain = (300-100)*5 = 1000, not (300-200)*5 = 500
        # (which is what consuming the expensive lot first would produce).
        assert body["totalRealizedGainNative"] == 1000

        # The displayed transaction order matches insertion (id) order —
        # the same order FIFO validation used — not an independent one.
        displayed_ids = [t["id"] for t in body["transactions"]]
        assert displayed_ids == [cheap_lot.id, expensive_lot.id, partial_sell.id]

    def test_does_not_leak_another_users_ticker_data(
        self, authed_client: TestClient, db_session: Session
    ) -> None:
        # Regression test for the fixed cross-tenant leak: a different
        # user's ticker must 404, not be returned.
        other_user = User(email="other-leak-test@example.com")
        db_session.add(other_user)
        db_session.flush()
        db_session.add(TickerMetadata(user_id=other_user.id, ticker="MSFT", market="NASDAQ", category="Stock", native_currency="USD"))
        db_session.flush()

        response = authed_client.get("/portfolio/tickers/MSFT")
        assert response.status_code == 404

    def test_returns_401_without_authentication(self, unauthenticated_client: TestClient) -> None:
        # Regression test for the fixed inconsistency: this route now goes
        # through the same auth dependency as every other route.
        response = unauthenticated_client.get("/portfolio/tickers/AAPL")
        assert response.status_code == 401


class TestUpdateAndDeleteTicker:
    def test_update_returns_401_without_authentication(self, unauthenticated_client: TestClient) -> None:
        # Regression test for the fixed inconsistency: PUT previously fell
        # through to a 500 on auth failure instead of 401.
        response = unauthenticated_client.put(
            "/portfolio/tickers/AAPL", json={"market": "NASDAQ", "category": "Stock", "nativeCurrency": "USD"}
        )
        assert response.status_code == 401

    def test_delete_returns_401_without_authentication(self, unauthenticated_client: TestClient) -> None:
        response = unauthenticated_client.delete("/portfolio/tickers/AAPL")
        assert response.status_code == 401

    def test_update_blocks_currency_change_when_transactions_exist(
        self, authed_client: TestClient, db_session: Session, test_user: User
    ) -> None:
        db_session.add(TickerMetadata(user_id=test_user.id, ticker="AAPL", market="NASDAQ", category="Stock", native_currency="USD"))
        db_session.add(
            PortfolioTransaction(
                user_id=test_user.id, ticker="AAPL", date=datetime(2024, 1, 1, tzinfo=timezone.utc), type="BUY",
                native_currency="USD", quantity=10, price_per_share=100, fx_rate_to_chf=0.9,
            )
        )
        db_session.flush()

        response = authed_client.put(
            "/portfolio/tickers/AAPL", json={"market": "NASDAQ", "category": "Stock", "nativeCurrency": "CHF"}
        )
        assert response.status_code == 400
        assert "Cannot change currency" in response.json()["error"]

    def test_delete_cascades_transactions(self, authed_client: TestClient, db_session: Session, test_user: User) -> None:
        db_session.add(TickerMetadata(user_id=test_user.id, ticker="AAPL", market="NASDAQ", category="Stock", native_currency="USD"))
        db_session.add(
            PortfolioTransaction(
                user_id=test_user.id, ticker="AAPL", date=datetime(2024, 1, 1, tzinfo=timezone.utc), type="BUY",
                native_currency="USD", quantity=10, price_per_share=100, fx_rate_to_chf=0.9,
            )
        )
        db_session.flush()

        response = authed_client.delete("/portfolio/tickers/AAPL")
        assert response.status_code == 200
        assert response.json()["success"] is True

        remaining_txns = db_session.query(PortfolioTransaction).filter(PortfolioTransaction.ticker == "AAPL").count()
        assert remaining_txns == 0
