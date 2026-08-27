from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models import PortfolioTransaction, TickerMetadata, User
from app.services.allocation_service import compute_allocation
from app.services.price_service import PriceQuote


def add_ticker_and_buy(
    db_session: Session,
    user: User,
    ticker: str,
    category: str,
    currency: str,
    quantity: float = 10,
    price_per_share: float = 100,
) -> None:
    db_session.add(TickerMetadata(user_id=user.id, ticker=ticker, market="NASDAQ", category=category, native_currency=currency))
    db_session.add(
        PortfolioTransaction(
            user_id=user.id, ticker=ticker, date=datetime(2024, 1, 1, tzinfo=timezone.utc), type="BUY",
            native_currency=currency, quantity=quantity, price_per_share=price_per_share, fx_rate_to_chf=1.0,
        )
    )
    db_session.flush()


class TestComputeAllocation:
    def test_groups_by_category_and_currency_with_percentages(
        self, db_session: Session, test_user: User, monkeypatch
    ) -> None:
        add_ticker_and_buy(db_session, test_user, "AAPL", category="Stock", currency="USD")
        add_ticker_and_buy(db_session, test_user, "GOLD", category="Gold", currency="CHF")

        monkeypatch.setattr(
            "app.services.portfolio_rollup_service.fetch_current_price",
            lambda yahoo_ticker, force_refresh=False: PriceQuote(
                price=100.0, currency="USD", timestamp=datetime.now(timezone.utc), source="yahoo",
                daily_change_percent=0.0, daily_change=0.0,
            ),
        )
        monkeypatch.setattr(
            "app.services.portfolio_rollup_service.fetch_fx_rate_to_chf",
            lambda ccy, force_refresh=False: 1.0,
        )

        breakdown = compute_allocation(db_session, test_user.id)

        assert breakdown.total_market_value_chf == 2000  # 10*100 + 10*100, both fx=1.0
        by_category = {row.category: row for row in breakdown.by_category}
        assert by_category["Stock"].market_value_chf == 1000
        assert by_category["Stock"].percent_of_portfolio == 50.0
        assert by_category["Gold"].percent_of_portfolio == 50.0

        by_currency = {row.currency: row for row in breakdown.by_currency}
        assert by_currency["USD"].market_value_chf == 1000
        assert by_currency["CHF"].market_value_chf == 1000

    def test_empty_portfolio_returns_zero_total_and_no_rows(self, db_session: Session, test_user: User) -> None:
        breakdown = compute_allocation(db_session, test_user.id)

        assert breakdown.total_market_value_chf == 0
        assert breakdown.by_category == []
        assert breakdown.by_currency == []

    def test_price_errors_are_surfaced_not_silently_dropped(
        self, db_session: Session, test_user: User, monkeypatch
    ) -> None:
        add_ticker_and_buy(db_session, test_user, "BROKEN", category="Stock", currency="USD")

        def failing_price(yahoo_ticker: str, force_refresh: bool = False) -> PriceQuote:
            raise RuntimeError("No valid price from Yahoo Finance for BROKEN")

        monkeypatch.setattr("app.services.portfolio_rollup_service.fetch_current_price", failing_price)
        monkeypatch.setattr("app.services.portfolio_rollup_service.fetch_fx_rate_to_chf", lambda ccy, force_refresh=False: 1.0)

        breakdown = compute_allocation(db_session, test_user.id)

        assert breakdown.by_category == []
        assert len(breakdown.price_errors) == 1
        assert breakdown.price_errors[0].ticker == "BROKEN"
