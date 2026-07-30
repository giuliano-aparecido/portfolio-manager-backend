from datetime import datetime, timezone

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import PassiveInvestment, PassiveTransaction, User


class TestPassiveRollupRoute:
    def test_computes_rollup_with_mocked_fx(self, authed_client: TestClient, db_session: Session, test_user: User, monkeypatch) -> None:
        inv = PassiveInvestment(user_id=test_user.id, name="Test", type="CASH", currency="USD")
        db_session.add(inv)
        db_session.flush()
        db_session.add(PassiveTransaction(passive_investment_id=inv.id, type="DEPOSIT", date=datetime(2024, 1, 1, tzinfo=timezone.utc), amount_native=1000))
        db_session.flush()

        monkeypatch.setattr("app.services.passive_rollup_service.fetch_fx_rate_to_chf", lambda ccy: 0.9)

        response = authed_client.get("/passive-rollup")
        assert response.status_code == 200
        body = response.json()
        assert len(body["rows"]) == 1
        assert body["rows"][0]["costBasisCHF"] == 900
        assert body["totalMarketValueCHF"] == 900

    def test_returns_all_zeros_for_empty_input(self, authed_client: TestClient) -> None:
        response = authed_client.get("/passive-rollup")
        assert response.status_code == 200
        body = response.json()
        assert body["rows"] == []
        assert body["totalCostBasisCHF"] == 0
