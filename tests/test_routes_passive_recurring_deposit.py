from datetime import datetime, timezone

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import PassiveInvestment, PassiveRecurringDeposit, PassiveTransaction, User


def make_investment(db_session: Session, user: User) -> PassiveInvestment:
    inv = PassiveInvestment(user_id=user.id, name="Pension", type="PENSION_FUND", currency="CHF")
    db_session.add(inv)
    db_session.flush()
    return inv


class TestCreateRecurringDeposit:
    def test_creates_valid_rule(self, authed_client: TestClient, db_session: Session, test_user: User) -> None:
        inv = make_investment(db_session, test_user)
        response = authed_client.post(
            f"/passive-investments/{inv.id}/recurring-deposit",
            json={"amountNative": 500, "frequency": "monthly", "startDate": "2024-01-01"},
        )
        assert response.status_code == 201
        body = response.json()
        assert body["frequency"] == "MONTHLY"

        # Materializing the detail response should now include due deposits.
        detail = authed_client.get(f"/passive-investments/{inv.id}").json()
        assert len(detail["transactions"]) > 0
        assert detail["recurringDeposit"]["frequency"] == "MONTHLY"

    def test_returns_409_when_rule_already_exists(self, authed_client: TestClient, db_session: Session, test_user: User) -> None:
        inv = make_investment(db_session, test_user)
        db_session.add(PassiveRecurringDeposit(passive_investment_id=inv.id, amount_native=100, start_date=datetime(2024, 1, 1, tzinfo=timezone.utc), frequency="MONTHLY"))
        db_session.flush()

        response = authed_client.post(
            f"/passive-investments/{inv.id}/recurring-deposit",
            json={"amountNative": 100, "frequency": "MONTHLY", "startDate": "2024-01-01"},
        )
        assert response.status_code == 409

    def test_returns_400_when_end_date_before_start_date(self, authed_client: TestClient, db_session: Session, test_user: User) -> None:
        inv = make_investment(db_session, test_user)
        response = authed_client.post(
            f"/passive-investments/{inv.id}/recurring-deposit",
            json={"amountNative": 100, "frequency": "MONTHLY", "startDate": "2024-06-01", "endDate": "2024-01-01"},
        )
        assert response.status_code == 400
        assert "endDate must be on or after startDate" in response.json()["error"]


class TestUpdateRecurringDeposit:
    def test_updates_all_mutable_fields_including_schedule(self, authed_client: TestClient, db_session: Session, test_user: User) -> None:
        inv = make_investment(db_session, test_user)
        rule = PassiveRecurringDeposit(passive_investment_id=inv.id, amount_native=100, start_date=datetime(2024, 1, 1, tzinfo=timezone.utc), frequency="MONTHLY", last_generated_date=datetime(2024, 3, 1, tzinfo=timezone.utc))
        db_session.add(rule)
        db_session.flush()

        response = authed_client.put(
            f"/passive-investments/{inv.id}/recurring-deposit",
            json={"amountNative": 200, "frequency": "weekly", "startDate": "2024-06-01"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["amountNative"] == 200
        assert body["frequency"] == "WEEKLY"

        db_session.refresh(rule)
        # last_generated_date must be untouched by PUT.
        assert rule.last_generated_date.date().isoformat() == "2024-03-01"

    def test_returns_404_when_no_rule_exists(self, authed_client: TestClient, db_session: Session, test_user: User) -> None:
        inv = make_investment(db_session, test_user)
        response = authed_client.put(
            f"/passive-investments/{inv.id}/recurring-deposit",
            json={"amountNative": 100, "frequency": "MONTHLY", "startDate": "2024-01-01"},
        )
        assert response.status_code == 404


class TestDeleteRecurringDeposit:
    def test_deletes_rule_but_keeps_generated_deposits(self, authed_client: TestClient, db_session: Session, test_user: User) -> None:
        inv = make_investment(db_session, test_user)
        db_session.add(PassiveRecurringDeposit(passive_investment_id=inv.id, amount_native=100, start_date=datetime(2024, 1, 1, tzinfo=timezone.utc), frequency="MONTHLY"))
        db_session.add(PassiveTransaction(passive_investment_id=inv.id, type="DEPOSIT", date=datetime(2024, 1, 1, tzinfo=timezone.utc), amount_native=100, notes="Recurring deposit"))
        db_session.flush()

        response = authed_client.delete(f"/passive-investments/{inv.id}/recurring-deposit")
        assert response.status_code == 200

        remaining_txns = db_session.query(PassiveTransaction).filter(PassiveTransaction.passive_investment_id == inv.id).count()
        assert remaining_txns == 1

    def test_returns_404_when_no_rule_exists(self, authed_client: TestClient, db_session: Session, test_user: User) -> None:
        inv = make_investment(db_session, test_user)
        response = authed_client.delete(f"/passive-investments/{inv.id}/recurring-deposit")
        assert response.status_code == 404
