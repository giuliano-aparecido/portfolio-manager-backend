from datetime import datetime, timezone

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import PassiveInvestment, PassiveTransaction, User


def make_investment(db_session: Session, user: User, **overrides) -> PassiveInvestment:
    defaults = {"user_id": user.id, "name": "Emergency bucket", "type": "CASH", "currency": "CHF"}
    defaults.update(overrides)
    row = PassiveInvestment(**defaults)
    db_session.add(row)
    db_session.flush()
    return row


class TestCreateAndList:
    def test_creates_with_valid_input(self, authed_client: TestClient) -> None:
        response = authed_client.post("/passive-investments", json={"name": "Emergency bucket", "type": "cash", "currency": "chf"})
        assert response.status_code == 201
        body = response.json()
        assert body["type"] == "CASH"
        assert body["currency"] == "CHF"

    def test_returns_400_if_name_missing(self, authed_client: TestClient) -> None:
        response = authed_client.post("/passive-investments", json={"type": "CASH", "currency": "CHF"})
        assert response.status_code == 400
        assert "name is required" in response.json()["error"]

    def test_ignores_gain_loss_pct_sent_on_create(self, authed_client: TestClient, db_session: Session) -> None:
        response = authed_client.post(
            "/passive-investments", json={"name": "Test", "type": "CASH", "currency": "CHF", "gainLossPct": 12.5}
        )
        assert response.status_code == 201
        assert response.json()["gainLossPct"] is None


class TestGetDetail:
    def test_returns_404_for_missing_investment(self, authed_client: TestClient) -> None:
        response = authed_client.get("/passive-investments/999999")
        assert response.status_code == 404

    def test_returns_400_for_non_numeric_id(self, authed_client: TestClient) -> None:
        response = authed_client.get("/passive-investments/abc")
        assert response.status_code == 400
        assert response.json()["error"] == "invalid id"

    def test_returns_detail_with_deposits(self, authed_client: TestClient, db_session: Session, test_user: User) -> None:
        inv = make_investment(db_session, test_user)
        db_session.add(
            PassiveTransaction(passive_investment_id=inv.id, type="DEPOSIT", date=datetime(2024, 1, 1, tzinfo=timezone.utc), amount_native=1000)
        )
        db_session.flush()

        response = authed_client.get(f"/passive-investments/{inv.id}")
        assert response.status_code == 200
        body = response.json()
        assert body["costBasisNative"] == 1000
        assert body["marketValueNative"] == 1000  # no gainLossPct set -> equals cost basis
        assert body["recurringDeposit"] is None


class TestUpdate:
    def test_stores_valid_gain_loss_pct_and_bumps_updated_at(self, authed_client: TestClient, db_session: Session, test_user: User) -> None:
        inv = make_investment(db_session, test_user)
        response = authed_client.put(
            f"/passive-investments/{inv.id}",
            json={"name": inv.name, "type": inv.type, "currency": inv.currency, "gainLossPct": 5.3},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["gainLossPct"] == 5.3
        assert body["gainLossUpdatedAt"] is not None

    def test_rejects_gain_loss_pct_below_negative_100(self, authed_client: TestClient, db_session: Session, test_user: User) -> None:
        inv = make_investment(db_session, test_user)
        response = authed_client.put(
            f"/passive-investments/{inv.id}",
            json={"name": inv.name, "type": inv.type, "currency": inv.currency, "gainLossPct": -150},
        )
        assert response.status_code == 400
        assert "-100" in response.json()["error"]

    def test_allows_exactly_negative_100(self, authed_client: TestClient, db_session: Session, test_user: User) -> None:
        inv = make_investment(db_session, test_user)
        response = authed_client.put(
            f"/passive-investments/{inv.id}",
            json={"name": inv.name, "type": inv.type, "currency": inv.currency, "gainLossPct": -100},
        )
        assert response.status_code == 200

    def test_does_not_bump_updated_at_when_unchanged(self, authed_client: TestClient, db_session: Session, test_user: User) -> None:
        inv = make_investment(db_session, test_user, gain_loss_pct=5.0)
        response = authed_client.put(
            f"/passive-investments/{inv.id}",
            json={"name": inv.name, "type": inv.type, "currency": inv.currency, "gainLossPct": 5.0},
        )
        assert response.status_code == 200
        assert response.json()["gainLossUpdatedAt"] is None


class TestDelete:
    def test_cascades_transactions(self, authed_client: TestClient, db_session: Session, test_user: User) -> None:
        inv = make_investment(db_session, test_user)
        db_session.add(
            PassiveTransaction(passive_investment_id=inv.id, type="DEPOSIT", date=datetime(2024, 1, 1, tzinfo=timezone.utc), amount_native=100)
        )
        db_session.flush()

        response = authed_client.delete(f"/passive-investments/{inv.id}")
        assert response.status_code == 200
        assert response.json()["success"] is True

        remaining = db_session.query(PassiveTransaction).filter(PassiveTransaction.passive_investment_id == inv.id).count()
        assert remaining == 0
