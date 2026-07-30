from datetime import datetime, timezone

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import PassiveInvestment, PassiveTransaction, User


def make_investment(db_session: Session, user: User) -> PassiveInvestment:
    inv = PassiveInvestment(user_id=user.id, name="Emergency bucket", type="CASH", currency="CHF")
    db_session.add(inv)
    db_session.flush()
    return inv


class TestCreatePassiveTransaction:
    def test_creates_deposit(self, authed_client: TestClient, db_session: Session, test_user: User) -> None:
        inv = make_investment(db_session, test_user)
        response = authed_client.post(
            f"/passive-investments/{inv.id}/transactions", json={"type": "deposit", "date": "2024-01-01", "amountNative": 1000}
        )
        assert response.status_code == 201
        assert response.json()["type"] == "DEPOSIT"

    def test_returns_400_when_withdrawal_exceeds_balance(self, authed_client: TestClient, db_session: Session, test_user: User) -> None:
        inv = make_investment(db_session, test_user)
        response = authed_client.post(
            f"/passive-investments/{inv.id}/transactions", json={"type": "WITHDRAWAL", "date": "2024-01-01", "amountNative": 100}
        )
        assert response.status_code == 400
        assert "would bring the balance negative" in response.json()["error"]

    def test_returns_404_for_unowned_investment(self, authed_client: TestClient) -> None:
        response = authed_client.post(
            "/passive-investments/999999/transactions", json={"type": "DEPOSIT", "date": "2024-01-01", "amountNative": 100}
        )
        assert response.status_code == 404


class TestUpdateAndDeletePassiveTransaction:
    def test_delete_blocked_when_it_would_break_a_later_withdrawal(self, authed_client: TestClient, db_session: Session, test_user: User) -> None:
        inv = make_investment(db_session, test_user)
        deposit = PassiveTransaction(passive_investment_id=inv.id, type="DEPOSIT", date=datetime(2024, 1, 1, tzinfo=timezone.utc), amount_native=100)
        db_session.add(deposit)
        db_session.add(PassiveTransaction(passive_investment_id=inv.id, type="WITHDRAWAL", date=datetime(2024, 2, 1, tzinfo=timezone.utc), amount_native=100))
        db_session.flush()

        response = authed_client.delete(f"/passive-investments/{inv.id}/transactions/{deposit.id}")
        assert response.status_code == 400
        assert "Cannot delete" in response.json()["error"]

    def test_update_succeeds(self, authed_client: TestClient, db_session: Session, test_user: User) -> None:
        inv = make_investment(db_session, test_user)
        deposit = PassiveTransaction(passive_investment_id=inv.id, type="DEPOSIT", date=datetime(2024, 1, 1, tzinfo=timezone.utc), amount_native=100)
        db_session.add(deposit)
        db_session.flush()

        response = authed_client.put(
            f"/passive-investments/{inv.id}/transactions/{deposit.id}",
            json={"type": "DEPOSIT", "date": "2024-01-02", "amountNative": 200},
        )
        assert response.status_code == 200
        assert response.json()["amountNative"] == 200
