from datetime import datetime, timezone

from app.services.fifo import ProcessedTransaction
from app.services.whatif_service import WhatIfTransaction, simulate_whatif


def txn(
    date_str: str,
    type_: str,
    quantity: float | None = None,
    price_per_share: float | None = None,
    fx_rate_to_chf: float = 1.0,
    ticker: str = "AAPL",
    id: int | None = None,
) -> ProcessedTransaction:
    return ProcessedTransaction(
        ticker=ticker,
        date=datetime.fromisoformat(date_str).replace(tzinfo=timezone.utc),
        type=type_,
        native_currency="USD",
        fx_rate_to_chf=fx_rate_to_chf,
        quantity=quantity,
        price_per_share=price_per_share,
        id=id,
    )


class TestSimulateWhatIfBuyMoreOfExisting:
    def test_buying_more_increases_shares_and_market_value(self) -> None:
        existing = [txn("2024-01-01", "BUY", quantity=10, price_per_share=100, fx_rate_to_chf=0.9)]
        hypothetical = WhatIfTransaction(ticker="AAPL", type="BUY", quantity=5, price_per_share=150)

        result = simulate_whatif(
            existing_transactions=existing,
            hypothetical=hypothetical,
            current_price_native=150,
            fx_rate_to_chf=0.9,
            portfolio_market_value_chf_before=1350,  # 10 * 150 * 0.9, this ticker is the whole portfolio
        )

        assert result.error is None
        assert result.shares_before == 10
        assert result.shares_after == 15
        assert result.market_value_chf_before == 10 * 150 * 0.9
        assert result.market_value_chf_after == 15 * 150 * 0.9
        assert result.realized_gain_chf == 0
        assert result.ticker_allocation_percent_before == 100.0
        assert result.ticker_allocation_percent_after == 100.0

    def test_buying_a_brand_new_ticker_starts_from_zero_shares(self) -> None:
        result = simulate_whatif(
            existing_transactions=[],
            hypothetical=WhatIfTransaction(ticker="MSFT", type="BUY", quantity=5, price_per_share=300),
            current_price_native=300,
            fx_rate_to_chf=0.9,
            portfolio_market_value_chf_before=1000,
        )

        assert result.error is None
        assert result.shares_before == 0
        assert result.shares_after == 5
        assert result.cost_basis_chf_before == 0
        assert result.market_value_chf_after == 5 * 300 * 0.9


class TestSimulateWhatIfSell:
    def test_selling_within_holdings_realizes_a_gain(self) -> None:
        existing = [txn("2024-01-01", "BUY", quantity=10, price_per_share=100, fx_rate_to_chf=0.9)]
        hypothetical = WhatIfTransaction(ticker="AAPL", type="SELL", quantity=4, price_per_share=150)

        result = simulate_whatif(
            existing_transactions=existing,
            hypothetical=hypothetical,
            current_price_native=150,
            fx_rate_to_chf=0.9,
            portfolio_market_value_chf_before=10 * 150 * 0.9,
        )

        assert result.error is None
        assert result.shares_after == 6
        # proceeds 4*150*0.9=540, cost basis 4*100*0.9=360 -> gain 180
        assert result.realized_gain_chf == 180
        assert result.market_value_chf_after == 6 * 150 * 0.9

    def test_over_selling_returns_structured_error_not_an_exception(self) -> None:
        existing = [txn("2024-01-01", "BUY", quantity=10, price_per_share=100, fx_rate_to_chf=0.9)]
        hypothetical = WhatIfTransaction(ticker="AAPL", type="SELL", quantity=20, price_per_share=150)

        result = simulate_whatif(
            existing_transactions=existing,
            hypothetical=hypothetical,
            current_price_native=150,
            fx_rate_to_chf=0.9,
            portfolio_market_value_chf_before=10 * 150 * 0.9,
        )

        assert result.error is not None
        assert "shares short" in result.error
        # State reflects "before" unchanged, not a partial/garbage mutation.
        assert result.shares_after == result.shares_before == 10
        assert result.market_value_chf_after == result.market_value_chf_before


class TestSimulateWhatIfSameDateTiebreak:
    def test_same_date_existing_lots_resolved_by_id_regardless_of_input_order(self) -> None:
        # Two same-date BUY lots at different prices, handed in the REVERSE
        # of their id (insertion) order — simulating an unordered fetch.
        # The lower-id (earlier) lot must still be consumed first.
        expensive_lot = txn("2024-01-01", "BUY", quantity=5, price_per_share=200, id=2)
        cheap_lot = txn("2024-01-01", "BUY", quantity=5, price_per_share=100, id=1)
        hypothetical = WhatIfTransaction(ticker="AAPL", type="SELL", quantity=5, price_per_share=300)

        result = simulate_whatif(
            existing_transactions=[expensive_lot, cheap_lot],
            hypothetical=hypothetical,
            current_price_native=300,
            fx_rate_to_chf=1.0,
            portfolio_market_value_chf_before=3000,
        )

        assert result.error is None
        # Selling into the cheap lot (id=1, cost 100) first: proceeds
        # 5*300=1500, cost basis 5*100=500 -> gain 1000. If the expensive
        # lot (id=2) were wrongly consumed first, the gain would be 500.
        assert result.realized_gain_chf == 1000


class TestSimulateWhatIfPortfolioAllocation:
    def test_allocation_percent_accounts_for_rest_of_portfolio(self) -> None:
        existing = [txn("2024-01-01", "BUY", quantity=10, price_per_share=100, fx_rate_to_chf=1.0)]
        # This ticker is worth 1000 CHF out of a 4000 CHF portfolio (25%)
        # before the trade.
        result = simulate_whatif(
            existing_transactions=existing,
            hypothetical=WhatIfTransaction(ticker="AAPL", type="BUY", quantity=10, price_per_share=100),
            current_price_native=100,
            fx_rate_to_chf=1.0,
            portfolio_market_value_chf_before=4000,
        )

        assert result.ticker_allocation_percent_before == 25.0
        # After buying 10 more at 100, this ticker is worth 2000 out of a
        # 5000 CHF portfolio (the rest, 3000, is unaffected) -> 40%.
        assert result.ticker_allocation_percent_after == 40.0
        assert result.portfolio_value_chf_after == 5000
