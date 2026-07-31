from datetime import datetime, timezone

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import PortfolioTransaction, TickerMetadata, User
from app.services.price_service import PriceQuote


def add_ticker_and_buy(db_session: Session, user: User, ticker: str = "AAPL", currency: str = "USD") -> None:
    db_session.add(TickerMetadata(user_id=user.id, ticker=ticker, market="NASDAQ", category="Stock", native_currency=currency))
    db_session.add(
        PortfolioTransaction(
            user_id=user.id, ticker=ticker, date=datetime(2024, 1, 1, tzinfo=timezone.utc), type="BUY",
            native_currency=currency, quantity=10, price_per_share=100, fx_rate_to_chf=0.9,
        )
    )
    db_session.flush()


class TestPortfolioRollupRoute:
    def test_computes_rollup_with_mocked_price_and_fx(
        self, authed_client: TestClient, db_session: Session, test_user: User, monkeypatch
    ) -> None:
        add_ticker_and_buy(db_session, test_user)
        monkeypatch.setattr(
            "app.services.portfolio_rollup_service.fetch_current_price",
            lambda yahoo_ticker, force_refresh=False: PriceQuote(
                price=150.0, currency="USD", timestamp=datetime.now(timezone.utc), source="yahoo",
                daily_change_percent=1.0, daily_change=1.5,
            ),
        )
        monkeypatch.setattr("app.services.portfolio_rollup_service.fetch_fx_rate_to_chf", lambda ccy, force_refresh=False: 0.9)

        response = authed_client.get("/portfolio-rollup")
        assert response.status_code == 200
        body = response.json()
        assert len(body["openTickers"]) == 1
        assert body["openTickers"][0]["marketValueNative"] == 1500.0
        assert body["totalMarketValueCHF"] == 1500.0 * 0.9

    def test_500_hard_fails_when_open_ticker_has_no_metadata(
        self, authed_client: TestClient, db_session: Session, test_user: User
    ) -> None:
        # No TickerMetadata registered at all — an open position with
        # transactions but no metadata is a hard failure for the whole
        # rollup, unlike per-ticker price/FX errors.
        db_session.add(
            PortfolioTransaction(
                user_id=test_user.id, ticker="ORPHAN", date=datetime(2024, 1, 1, tzinfo=timezone.utc), type="BUY",
                native_currency="USD", quantity=10, price_per_share=100, fx_rate_to_chf=0.9,
            )
        )
        db_session.flush()

        response = authed_client.get("/portfolio-rollup")
        assert response.status_code == 500
        assert "No TickerMetadata for ORPHAN" in response.json()["error"]

    def test_excludes_ticker_from_totals_on_price_fetch_failure_but_keeps_others(
        self, authed_client: TestClient, db_session: Session, test_user: User, monkeypatch
    ) -> None:
        add_ticker_and_buy(db_session, test_user, ticker="AAPL")
        add_ticker_and_buy(db_session, test_user, ticker="MSFT")
        monkeypatch.setattr("app.services.portfolio_rollup_service.fetch_fx_rate_to_chf", lambda ccy, force_refresh=False: 0.9)

        def fake_price(yahoo_ticker: str, force_refresh: bool = False) -> PriceQuote:
            if yahoo_ticker == "MSFT":
                raise RuntimeError("No valid price from Yahoo Finance for MSFT")
            return PriceQuote(price=150.0, currency="USD", timestamp=datetime.now(timezone.utc), source="yahoo", daily_change_percent=1.0, daily_change=1.5)

        monkeypatch.setattr("app.services.portfolio_rollup_service.fetch_current_price", fake_price)

        response = authed_client.get("/portfolio-rollup")
        assert response.status_code == 200
        body = response.json()
        assert [t["ticker"] for t in body["openTickers"]] == ["AAPL"]
        assert len(body["priceErrors"]) == 1
        assert body["priceErrors"][0]["ticker"] == "MSFT"

    def test_returns_all_zeros_for_empty_portfolio(self, authed_client: TestClient) -> None:
        response = authed_client.get("/portfolio-rollup")
        assert response.status_code == 200
        body = response.json()
        assert body["openTickers"] == []
        assert body["closedTickers"] == []
        assert body["totalCostBasisCHF"] == 0

    def test_refresh_query_param_is_threaded_through_as_force_refresh(
        self, authed_client: TestClient, monkeypatch
    ) -> None:
        received: dict = {}

        def fake_compute(db, user_id, *, force_refresh=False):
            received["force_refresh"] = force_refresh
            from app.schemas.portfolio import PortfolioRollup

            return PortfolioRollup(
                open_tickers=[], closed_tickers=[], price_errors=[],
                total_cost_basis_chf=0, total_market_value_chf=0, total_unrealized_gain_chf=0,
                total_dividends_chf=0, total_realized_gain_chf=0,
            )

        monkeypatch.setattr("app.routers.portfolio_rollup.compute_portfolio_rollup", fake_compute)

        authed_client.get("/portfolio-rollup")
        assert received["force_refresh"] is False

        authed_client.get("/portfolio-rollup?refresh=true")
        assert received["force_refresh"] is True
