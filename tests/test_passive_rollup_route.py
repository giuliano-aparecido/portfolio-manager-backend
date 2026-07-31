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

        monkeypatch.setattr("app.services.passive_rollup_service.fetch_fx_rate_to_chf", lambda ccy, force_refresh=False: 0.9)

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

    def test_refresh_query_param_is_threaded_through_as_force_refresh(
        self, authed_client: TestClient, monkeypatch
    ) -> None:
        received: dict = {}

        def fake_compute(db, user_id, *, force_refresh=False):
            received["force_refresh"] = force_refresh
            from app.schemas.passive import PassiveRollup

            return PassiveRollup(rows=[], fx_errors=[], total_cost_basis_chf=0, total_market_value_chf=0, total_unrealized_gain_chf=0)

        monkeypatch.setattr("app.routers.passive_rollup.compute_passive_rollup", fake_compute)

        authed_client.get("/passive-rollup")
        assert received["force_refresh"] is False

        authed_client.get("/passive-rollup?refresh=true")
        assert received["force_refresh"] is True

    def test_unexpected_exception_returns_generic_message_not_the_raw_text(
        self, authed_client: TestClient, monkeypatch
    ) -> None:
        # This route has no expected-error case of its own (FX failures are
        # already caught per-currency inside compute_passive_rollup), so
        # anything reaching this handler is unexpected and must never leak
        # its raw message - e.g. a DB driver error can embed the connection
        # string.
        def raise_sensitive(db, user_id, force_refresh=False):
            raise ValueError("connection to server at postgresql://user:supersecret@host failed")

        monkeypatch.setattr("app.routers.passive_rollup.compute_passive_rollup", raise_sensitive)

        response = authed_client.get("/passive-rollup")
        assert response.status_code == 500
        assert response.json() == {"error": "Internal server error"}
        assert "supersecret" not in response.text
