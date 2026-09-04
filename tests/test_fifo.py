from datetime import datetime, timezone

import pytest

from app.services.fifo import ProcessedTransaction, process_ticker


def txn(
    date_str: str,
    type_: str,
    quantity: float | None = None,
    price_per_share: float | None = None,
    cash_amount: float | None = None,
    fx_rate_to_chf: float = 1.0,
    ticker: str = "AAPL",
    native_currency: str = "USD",
) -> ProcessedTransaction:
    return ProcessedTransaction(
        ticker=ticker,
        date=datetime.fromisoformat(date_str).replace(tzinfo=timezone.utc),
        type=type_,
        native_currency=native_currency,
        fx_rate_to_chf=fx_rate_to_chf,
        quantity=quantity,
        price_per_share=price_per_share,
        cash_amount=cash_amount,
    )


def test_single_buy_sets_shares_and_cost_basis() -> None:
    result = process_ticker([txn("2024-01-01", "BUY", quantity=10, price_per_share=100, fx_rate_to_chf=0.9)])

    assert result.current_shares == 10
    assert result.current_cost_basis_native == 1000
    assert result.current_cost_basis_chf == 900


def test_partial_sell_consumes_oldest_lot_first() -> None:
    result = process_ticker(
        [
            txn("2024-01-01", "BUY", quantity=10, price_per_share=100, fx_rate_to_chf=1.0),
            txn("2024-02-01", "BUY", quantity=10, price_per_share=150, fx_rate_to_chf=1.0),
            txn("2024-03-01", "SELL", quantity=15, price_per_share=200, fx_rate_to_chf=1.0),
        ]
    )

    # Consumes all 10 from the first lot (cost 100) + 5 from the second (cost 150).
    assert result.current_shares == 5
    assert result.current_cost_basis_native == 5 * 150
    assert len(result.realized_gains) == 1
    gain = result.realized_gains[0]
    assert gain.cost_basis_native == 10 * 100 + 5 * 150
    assert gain.proceeds_native == 15 * 200
    assert gain.gain_native == gain.proceeds_native - gain.cost_basis_native


def test_realized_gain_sums_across_multiple_sales() -> None:
    result = process_ticker(
        [
            txn("2024-01-01", "BUY", quantity=10, price_per_share=100, fx_rate_to_chf=1.0),
            txn("2024-02-01", "SELL", quantity=4, price_per_share=150, fx_rate_to_chf=1.0),
            txn("2024-03-01", "SELL", quantity=4, price_per_share=200, fx_rate_to_chf=1.0),
        ]
    )

    assert len(result.realized_gains) == 2
    expected_total = result.realized_gains[0].gain_native + result.realized_gains[1].gain_native
    assert result.total_realized_gain_native == expected_total


def test_realized_gain_chf_uses_each_consumed_lots_own_historical_fx_rate() -> None:
    result = process_ticker(
        [
            txn("2024-01-01", "BUY", quantity=5, price_per_share=100, fx_rate_to_chf=0.8),
            txn("2024-02-01", "BUY", quantity=5, price_per_share=100, fx_rate_to_chf=0.95),
            # Sell's own FX rate (0.5, wildly different) must NOT be used for cost basis.
            txn("2024-03-01", "SELL", quantity=10, price_per_share=120, fx_rate_to_chf=0.5),
        ]
    )

    gain = result.realized_gains[0]
    expected_cost_basis_chf = 5 * 100 * 0.8 + 5 * 100 * 0.95
    assert gain.cost_basis_chf == expected_cost_basis_chf
    # Proceeds DO use the sell transaction's own (realized "now") FX rate.
    assert gain.proceeds_chf == 10 * 120 * 0.5


def test_dividend_does_not_touch_shares_or_cost_basis() -> None:
    result = process_ticker(
        [
            txn("2024-01-01", "BUY", quantity=10, price_per_share=100, fx_rate_to_chf=1.0),
            txn("2024-06-01", "DIVIDEND", cash_amount=50, fx_rate_to_chf=1.0),
        ]
    )

    assert result.current_shares == 10
    assert result.current_cost_basis_native == 1000
    assert result.realized_gains == []


def test_drip_shares_included_in_cost_basis_and_share_count() -> None:
    result = process_ticker(
        [
            txn("2024-01-01", "BUY", quantity=10, price_per_share=100, fx_rate_to_chf=1.0),
            txn("2024-06-01", "DRIP", quantity=2, price_per_share=110, fx_rate_to_chf=1.0),
        ]
    )

    assert result.current_shares == 12
    assert result.current_cost_basis_native == 1220  # 10*100 + 2*110 — DRIP lot cost included


def test_overselling_raises_with_ticker_date_and_shortfall_in_message() -> None:
    with pytest.raises(ValueError, match=r"Selling more shares than held for AAPL on 2024-02-01: 5 shares short"):
        process_ticker(
            [
                txn("2024-01-01", "BUY", quantity=10, price_per_share=100, fx_rate_to_chf=1.0),
                txn("2024-02-01", "SELL", quantity=15, price_per_share=100, fx_rate_to_chf=1.0),
            ]
        )
