from datetime import datetime, timezone

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import PortfolioTransaction, TickerMetadata, User


def register_ticker(db_session: Session, user: User, ticker: str = "AAPL", native_currency: str = "USD") -> None:
    db_session.add(TickerMetadata(user_id=user.id, ticker=ticker, market="NASDAQ", category="Stock", native_currency=native_currency))
    db_session.flush()


class TestCreateTransaction:
    def test_creates_a_buy_with_valid_input(self, authed_client: TestClient, db_session: Session, test_user: User, monkeypatch) -> None:
        register_ticker(db_session, test_user)
        monkeypatch.setattr("app.routers.portfolio_transactions.fetch_historical_fx_rate", lambda ccy, date: 0.9)

        response = authed_client.post(
            "/portfolio/transactions",
            json={"ticker": "AAPL", "type": "buy", "date": "2024-01-01", "quantity": 10, "pricePerShare": 100},
        )
        assert response.status_code == 201
        body = response.json()
        assert body["type"] == "BUY"
        assert body["fxRateToCHF"] == 0.9

    def test_creates_a_dividend_with_cash_amount(self, authed_client: TestClient, db_session: Session, test_user: User, monkeypatch) -> None:
        register_ticker(db_session, test_user)
        monkeypatch.setattr("app.routers.portfolio_transactions.fetch_historical_fx_rate", lambda ccy, date: 1.0)

        response = authed_client.post(
            "/portfolio/transactions",
            json={"ticker": "AAPL", "type": "dividend", "date": "2024-01-01", "cashAmount": 50},
        )
        assert response.status_code == 201
        assert response.json()["cashAmount"] == 50

    def test_returns_400_for_invalid_type(self, authed_client: TestClient) -> None:
        response = authed_client.post(
            "/portfolio/transactions", json={"ticker": "AAPL", "type": "INVALID", "date": "2024-01-01"}
        )
        assert response.status_code == 400
        assert "type must be one of" in response.json()["error"]

    def test_returns_400_for_invalid_date(self, authed_client: TestClient) -> None:
        response = authed_client.post(
            "/portfolio/transactions", json={"ticker": "AAPL", "type": "BUY", "date": "not-a-date", "quantity": 1, "pricePerShare": 1}
        )
        assert response.status_code == 400
        assert "date is invalid" in response.json()["error"]

    def test_returns_400_for_non_positive_quantity(self, authed_client: TestClient, db_session: Session, test_user: User) -> None:
        register_ticker(db_session, test_user)
        response = authed_client.post(
            "/portfolio/transactions", json={"ticker": "AAPL", "type": "BUY", "date": "2024-01-01", "quantity": 0, "pricePerShare": 100}
        )
        assert response.status_code == 400
        assert "quantity must be a positive number" in response.json()["error"]

    def test_returns_404_for_unregistered_ticker(self, authed_client: TestClient) -> None:
        response = authed_client.post(
            "/portfolio/transactions", json={"ticker": "AAPL", "type": "BUY", "date": "2024-01-01", "quantity": 1, "pricePerShare": 1}
        )
        assert response.status_code == 404
        assert "add it as an investment first" in response.json()["error"]

    def test_returns_502_when_historical_fx_fetch_fails(self, authed_client: TestClient, db_session: Session, test_user: User, monkeypatch) -> None:
        register_ticker(db_session, test_user)

        def _raise(ccy, date):
            raise RuntimeError("Yahoo is down")

        monkeypatch.setattr("app.routers.portfolio_transactions.fetch_historical_fx_rate", _raise)

        response = authed_client.post(
            "/portfolio/transactions", json={"ticker": "AAPL", "type": "BUY", "date": "2024-01-01", "quantity": 1, "pricePerShare": 1}
        )
        assert response.status_code == 502
        assert "Could not fetch historical FX rate" in response.json()["error"]

    def test_returns_400_when_fifo_integrity_violated(self, authed_client: TestClient, db_session: Session, test_user: User, monkeypatch) -> None:
        register_ticker(db_session, test_user)
        monkeypatch.setattr("app.routers.portfolio_transactions.fetch_historical_fx_rate", lambda ccy, date: 1.0)

        response = authed_client.post(
            "/portfolio/transactions", json={"ticker": "AAPL", "type": "SELL", "date": "2024-01-01", "quantity": 10, "pricePerShare": 100}
        )
        assert response.status_code == 400
        assert "Selling more shares than held" in response.json()["error"]


class TestUpdateAndDeleteTransaction:
    def _create_buy(self, db_session: Session, user: User, quantity: float = 10, date_str: str = "2024-01-01") -> PortfolioTransaction:
        txn = PortfolioTransaction(
            user_id=user.id,
            ticker="AAPL",
            date=datetime.fromisoformat(date_str).replace(tzinfo=timezone.utc),
            type="BUY",
            native_currency="USD",
            quantity=quantity,
            price_per_share=100,
            fx_rate_to_chf=0.9,
        )
        db_session.add(txn)
        db_session.flush()
        return txn

    def test_returns_400_for_non_numeric_id(self, authed_client: TestClient) -> None:
        response = authed_client.put("/portfolio/transactions/abc", json={"type": "BUY", "date": "2024-01-01", "quantity": 1, "pricePerShare": 1})
        assert response.status_code == 400
        assert response.json()["error"] == "Invalid transaction id"

    def test_returns_404_if_not_found_or_not_owned(self, authed_client: TestClient) -> None:
        response = authed_client.put("/portfolio/transactions/999999", json={"type": "BUY", "date": "2024-01-01", "quantity": 1, "pricePerShare": 1})
        assert response.status_code == 404

    def test_updates_successfully(self, authed_client: TestClient, db_session: Session, test_user: User, monkeypatch) -> None:
        register_ticker(db_session, test_user)
        txn = self._create_buy(db_session, test_user)
        monkeypatch.setattr("app.routers.portfolio_transactions.fetch_historical_fx_rate", lambda ccy, date: 0.95)

        response = authed_client.put(
            f"/portfolio/transactions/{txn.id}",
            json={"type": "BUY", "date": "2024-01-05", "quantity": 15, "pricePerShare": 110},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["quantity"] == 15
        assert body["fxRateToCHF"] == 0.95

    def test_delete_blocks_when_a_later_sell_depends_on_it(self, authed_client: TestClient, db_session: Session, test_user: User) -> None:
        register_ticker(db_session, test_user)
        buy = self._create_buy(db_session, test_user, quantity=10)
        db_session.add(
            PortfolioTransaction(
                user_id=test_user.id, ticker="AAPL", date=datetime(2024, 2, 1, tzinfo=timezone.utc), type="SELL",
                native_currency="USD", quantity=10, price_per_share=120, fx_rate_to_chf=0.9,
            )
        )
        db_session.flush()

        response = authed_client.delete(f"/portfolio/transactions/{buy.id}")
        assert response.status_code == 400
        assert "Cannot delete" in response.json()["error"]

    def test_delete_succeeds_when_safe(self, authed_client: TestClient, db_session: Session, test_user: User) -> None:
        register_ticker(db_session, test_user)
        txn = self._create_buy(db_session, test_user)

        response = authed_client.delete(f"/portfolio/transactions/{txn.id}")
        assert response.status_code == 200
        assert response.json()["success"] is True
