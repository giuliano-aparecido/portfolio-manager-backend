from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import PassiveInvestment, PassiveTransaction, PortfolioTransaction, TickerMetadata, User
from app.services.portfolio_rollup_service import securities_cash_flows_chf
from app.services.price_service import PriceQuote

YEAR_AGO = datetime.now(timezone.utc) - timedelta(days=365)


def _mock_prices(monkeypatch, price: float = 110.0) -> None:
    monkeypatch.setattr(
        "app.services.portfolio_rollup_service.fetch_current_price",
        lambda yahoo_ticker, force_refresh=False: PriceQuote(
            price=price, currency="CHF", timestamp=datetime.now(timezone.utc), source="yahoo",
            daily_change_percent=0.0, daily_change=0.0,
        ),
    )
    monkeypatch.setattr("app.services.portfolio_rollup_service.fetch_fx_rate_to_chf", lambda ccy, force_refresh=False: 1.0)
    monkeypatch.setattr("app.services.passive_rollup_service.fetch_fx_rate_to_chf", lambda ccy, force_refresh=False: 1.0)


def _buy(db: Session, user: User, qty: float = 10, price: float = 100, ticker: str = "ABC") -> None:
    if not db.query(TickerMetadata).filter_by(user_id=user.id, ticker=ticker).first():
        db.add(TickerMetadata(user_id=user.id, ticker=ticker, market="SIX", category="Stock", native_currency="CHF"))
    db.add(
        PortfolioTransaction(
            user_id=user.id, ticker=ticker, date=YEAR_AGO, type="BUY", native_currency="CHF",
            quantity=qty, price_per_share=price, fx_rate_to_chf=1.0,
        )
    )
    db.flush()


def test_securities_rollup_exposes_xirr(authed_client: TestClient, db_session: Session, test_user: User, monkeypatch) -> None:
    _buy(db_session, test_user)  # 1000 a year ago, worth 1100 now
    _mock_prices(monkeypatch)
    body = authed_client.get("/portfolio-rollup").json()
    assert abs(body["xirrPct"] - 10.0) < 0.1


def test_securities_xirr_null_when_history_too_short(
    authed_client: TestClient, db_session: Session, test_user: User, monkeypatch
) -> None:
    _buy(db_session, test_user)
    db_session.query(PortfolioTransaction).update({"date": datetime.now(timezone.utc) - timedelta(days=10)})
    db_session.flush()
    _mock_prices(monkeypatch)
    assert authed_client.get("/portfolio-rollup").json()["xirrPct"] is None


def test_combined_pools_securities_and_passive(
    authed_client: TestClient, db_session: Session, test_user: User, monkeypatch
) -> None:
    _buy(db_session, test_user)  # +10%
    inv = PassiveInvestment(user_id=test_user.id, name="Pillar 3a", type="Pillar 3a", currency="CHF", gain_loss_pct=30.0)
    db_session.add(inv)
    db_session.flush()
    db_session.add(PassiveTransaction(passive_investment_id=inv.id, type="DEPOSIT", date=YEAR_AGO, amount_native=1000))
    db_session.flush()
    _mock_prices(monkeypatch)

    combined = authed_client.get("/combined-return").json()["xirrPct"]
    # 2000 in a year ago, 1100 + 1300 now -> 20%; sits between 10% and 30%.
    assert abs(combined - 20.0) < 0.1


def test_failed_price_fetch_is_excluded_not_a_total_loss(
    authed_client: TestClient, db_session: Session, test_user: User, monkeypatch
) -> None:
    _buy(db_session, test_user, ticker="GOOD")
    _buy(db_session, test_user, ticker="BAD")
    _mock_prices(monkeypatch)

    def flaky(yahoo_ticker, force_refresh=False):
        if "BAD" in yahoo_ticker:
            raise RuntimeError("no quote")
        return PriceQuote(price=110.0, currency="CHF", timestamp=datetime.now(timezone.utc), source="yahoo",
                          daily_change_percent=0.0, daily_change=0.0)

    monkeypatch.setattr("app.services.portfolio_rollup_service.fetch_current_price", flaky)
    body = authed_client.get("/portfolio-rollup").json()
    assert len(body["priceErrors"]) == 1
    assert abs(body["xirrPct"] - 10.0) < 0.1


def test_cash_flow_signs() -> None:
    d = YEAR_AGO

    def txn(type_, **kw):
        return PortfolioTransaction(ticker="X", date=d, type=type_, native_currency="USD", fx_rate_to_chf=0.5, **kw)

    flows = securities_cash_flows_chf(
        [
            txn("BUY", quantity=10, price_per_share=10),
            txn("SELL", quantity=2, price_per_share=20),
            txn("DIVIDEND", cash_amount=4),
            txn("DRIP", quantity=1, price_per_share=10),
        ]
    )
    assert [round(a, 6) for _, a in flows] == [-50.0, 20.0, 2.0, -5.0]


def _passive(db: Session, user: User, gain_loss_pct, deposit: float = 1000, withdrawal: float = 0) -> PassiveInvestment:
    inv = PassiveInvestment(user_id=user.id, name="P", type="Pillar 3a", currency="CHF", gain_loss_pct=gain_loss_pct)
    db.add(inv)
    db.flush()
    db.add(PassiveTransaction(passive_investment_id=inv.id, type="DEPOSIT", date=YEAR_AGO, amount_native=deposit))
    if withdrawal:
        db.add(PassiveTransaction(
            passive_investment_id=inv.id, type="WITHDRAWAL", date=YEAR_AGO + timedelta(days=180), amount_native=withdrawal))
    db.flush()
    return inv


def test_passive_without_gain_loss_is_excluded_not_treated_as_flat(
    authed_client: TestClient, db_session: Session, test_user: User, monkeypatch
) -> None:
    _buy(db_session, test_user)  # +10%
    _passive(db_session, test_user, gain_loss_pct=None)  # unknown, must not drag toward 0%
    _mock_prices(monkeypatch)
    assert abs(authed_client.get("/combined-return").json()["xirrPct"] - 10.0) < 0.1


def test_passive_only_without_gain_loss_gives_null(
    authed_client: TestClient, db_session: Session, test_user: User, monkeypatch
) -> None:
    _passive(db_session, test_user, gain_loss_pct=None)
    _mock_prices(monkeypatch)
    assert authed_client.get("/combined-return").json()["xirrPct"] is None


def test_passive_withdrawal_is_an_inflow(
    authed_client: TestClient, db_session: Session, test_user: User, monkeypatch
) -> None:
    # Net balance 1000-400 = 600, +50% -> 900 now; 400 came back mid-year.
    _passive(db_session, test_user, gain_loss_pct=50.0, withdrawal=400)
    _mock_prices(monkeypatch)
    result = authed_client.get("/combined-return").json()["xirrPct"]
    assert result is not None and result > 30


def test_closed_ticker_still_counts(
    authed_client: TestClient, db_session: Session, test_user: User, monkeypatch
) -> None:
    _buy(db_session, test_user, ticker="OLD")
    db_session.add(PortfolioTransaction(
        user_id=test_user.id, ticker="OLD", date=YEAR_AGO + timedelta(days=364), type="SELL", native_currency="CHF",
        quantity=10, price_per_share=120, fx_rate_to_chf=1.0))
    db_session.flush()
    _mock_prices(monkeypatch)
    body = authed_client.get("/portfolio-rollup").json()
    assert body["openTickers"] == []
    assert abs(body["xirrPct"] - 20.0) < 0.5
