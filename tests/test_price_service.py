from datetime import datetime, timezone
from unittest.mock import MagicMock

import pandas as pd
import pytest

from app.services import price_service


def fake_ticker(fast_info: dict | None = None, history_df: pd.DataFrame | None = None) -> MagicMock:
    ticker = MagicMock()
    ticker.fast_info = fast_info or {}
    ticker.history.return_value = history_df if history_df is not None else pd.DataFrame()
    return ticker


class TestFetchCurrentPrice:
    def test_normal_quote_passes_through(self, monkeypatch) -> None:
        ticker = fake_ticker({"lastPrice": 150.0, "currency": "USD", "previousClose": 145.0})
        monkeypatch.setattr(price_service.yf, "Ticker", lambda _: ticker)

        quote = price_service.fetch_current_price("AAPL")

        assert quote.price == 150.0
        assert quote.currency == "USD"
        assert quote.daily_change == 5.0
        assert quote.daily_change_percent == pytest.approx(5.0 / 145.0 * 100)
        assert quote.source == "yahoo"

    def test_gbp_pence_marker_divides_price_and_change_by_100(self, monkeypatch) -> None:
        ticker = fake_ticker({"lastPrice": 11440.0, "currency": "GBp", "previousClose": 11400.0})
        monkeypatch.setattr(price_service.yf, "Ticker", lambda _: ticker)

        quote = price_service.fetch_current_price("THRL.L")

        assert quote.price == 114.4
        assert quote.currency == "GBP"
        assert quote.daily_change == pytest.approx(0.4)

    def test_raises_when_no_valid_price_and_history_empty(self, monkeypatch) -> None:
        ticker = fake_ticker({"lastPrice": None, "currency": "USD"}, history_df=pd.DataFrame())
        monkeypatch.setattr(price_service.yf, "Ticker", lambda _: ticker)

        with pytest.raises(RuntimeError, match="No valid price from Yahoo Finance for BADTICKER"):
            price_service.fetch_current_price("BADTICKER")

    def test_falls_back_to_history_when_fast_info_price_missing(self, monkeypatch) -> None:
        hist = pd.DataFrame({"Close": [95.0, 100.0]}, index=pd.to_datetime(["2024-01-01", "2024-01-02"], utc=True))
        ticker = fake_ticker({"lastPrice": None, "currency": "USD"}, history_df=hist)
        monkeypatch.setattr(price_service.yf, "Ticker", lambda _: ticker)

        quote = price_service.fetch_current_price("AAPL")
        assert quote.price == 100.0


class TestQuoteCache:
    def test_repeated_price_fetch_within_ttl_reuses_cached_quote(self, monkeypatch) -> None:
        ticker_ctor = MagicMock(return_value=fake_ticker({"lastPrice": 150.0, "currency": "USD", "previousClose": 145.0}))
        monkeypatch.setattr(price_service.yf, "Ticker", ticker_ctor)

        first = price_service.fetch_current_price("AAPL")
        second = price_service.fetch_current_price("AAPL")

        assert first.price == second.price == 150.0
        ticker_ctor.assert_called_once()

    def test_repeated_fx_fetch_within_ttl_reuses_cached_rate(self, monkeypatch) -> None:
        ticker_ctor = MagicMock(return_value=fake_ticker({"lastPrice": 0.91}))
        monkeypatch.setattr(price_service.yf, "Ticker", ticker_ctor)

        assert price_service.fetch_fx_rate_to_chf("USD") == 0.91
        assert price_service.fetch_fx_rate_to_chf("USD") == 0.91
        ticker_ctor.assert_called_once()

    def test_expired_cache_entry_triggers_a_fresh_fetch(self, monkeypatch) -> None:
        ticker_ctor = MagicMock(return_value=fake_ticker({"lastPrice": 150.0, "currency": "USD", "previousClose": 145.0}))
        monkeypatch.setattr(price_service.yf, "Ticker", ticker_ctor)
        monkeypatch.setattr(price_service, "_CACHE_TTL_SECONDS", 0.0)

        price_service.fetch_current_price("AAPL")
        price_service.fetch_current_price("AAPL")

        assert ticker_ctor.call_count == 2

    def test_different_tickers_are_cached_independently(self, monkeypatch) -> None:
        tickers = {
            "AAPL": fake_ticker({"lastPrice": 150.0, "currency": "USD", "previousClose": 145.0}),
            "MSFT": fake_ticker({"lastPrice": 300.0, "currency": "USD", "previousClose": 295.0}),
        }
        monkeypatch.setattr(price_service.yf, "Ticker", lambda t: tickers[t])

        assert price_service.fetch_current_price("AAPL").price == 150.0
        assert price_service.fetch_current_price("MSFT").price == 300.0

    def test_force_refresh_bypasses_a_warm_price_cache(self, monkeypatch) -> None:
        ticker_ctor = MagicMock(return_value=fake_ticker({"lastPrice": 150.0, "currency": "USD", "previousClose": 145.0}))
        monkeypatch.setattr(price_service.yf, "Ticker", ticker_ctor)

        price_service.fetch_current_price("AAPL")
        price_service.fetch_current_price("AAPL", force_refresh=True)

        assert ticker_ctor.call_count == 2

    def test_force_refresh_bypasses_a_warm_fx_cache(self, monkeypatch) -> None:
        ticker_ctor = MagicMock(return_value=fake_ticker({"lastPrice": 0.91}))
        monkeypatch.setattr(price_service.yf, "Ticker", ticker_ctor)

        price_service.fetch_fx_rate_to_chf("USD")
        price_service.fetch_fx_rate_to_chf("USD", force_refresh=True)

        assert ticker_ctor.call_count == 2

    def test_force_refresh_still_repopulates_the_cache(self, monkeypatch) -> None:
        # A force-refreshed value should still be cached afterward, so a
        # subsequent normal (non-forced) call within the TTL reuses it
        # rather than always re-fetching.
        ticker_ctor = MagicMock(return_value=fake_ticker({"lastPrice": 150.0, "currency": "USD", "previousClose": 145.0}))
        monkeypatch.setattr(price_service.yf, "Ticker", ticker_ctor)

        price_service.fetch_current_price("AAPL", force_refresh=True)
        price_service.fetch_current_price("AAPL")

        assert ticker_ctor.call_count == 1


class TestFetchFxRateToChf:
    def test_chf_shortcut_makes_no_network_call(self, monkeypatch) -> None:
        ticker_ctor = MagicMock()
        monkeypatch.setattr(price_service.yf, "Ticker", ticker_ctor)

        rate = price_service.fetch_fx_rate_to_chf("CHF")

        assert rate == 1.0
        ticker_ctor.assert_not_called()

    def test_normal_rate(self, monkeypatch) -> None:
        ticker = fake_ticker({"lastPrice": 0.91})
        monkeypatch.setattr(price_service.yf, "Ticker", lambda _: ticker)

        assert price_service.fetch_fx_rate_to_chf("USD") == 0.91

    def test_raises_on_invalid_rate(self, monkeypatch) -> None:
        ticker = fake_ticker({"lastPrice": 0})
        monkeypatch.setattr(price_service.yf, "Ticker", lambda _: ticker)

        with pytest.raises(RuntimeError, match="No valid FX rate from Yahoo Finance for USDCHF=X"):
            price_service.fetch_fx_rate_to_chf("USD")


class TestFetchHistoricalFxRate:
    def test_chf_shortcut_makes_no_network_call(self, monkeypatch) -> None:
        ticker_ctor = MagicMock()
        monkeypatch.setattr(price_service.yf, "Ticker", ticker_ctor)

        rate = price_service.fetch_historical_fx_rate("CHF", datetime(2024, 1, 1, tzinfo=timezone.utc))

        assert rate == 1.0
        ticker_ctor.assert_not_called()

    def test_takes_closest_prior_trading_day_across_a_weekend(self, monkeypatch) -> None:
        # Target is a Sunday; only Fri/Sat/Sun rows exist in the fake
        # history, and the weekend rows shouldn't exist in real data, but
        # this proves the "<=" filter + "last row" logic picks the closest
        # trading day at or before the target, not just the literal last row.
        hist = pd.DataFrame(
            {"Close": [0.85, 0.86, 0.90]},
            index=pd.to_datetime(["2023-12-22", "2023-12-27", "2024-01-05"], utc=True),
        )
        ticker = fake_ticker(history_df=hist)
        monkeypatch.setattr(price_service.yf, "Ticker", lambda _: ticker)

        rate = price_service.fetch_historical_fx_rate("USD", datetime(2023, 12, 29, tzinfo=timezone.utc))

        assert rate == 0.86  # Dec 27 is the closest date <= Dec 29; Jan 5 excluded (after target)

    def test_raises_when_no_quotes_at_or_before_target(self, monkeypatch) -> None:
        hist = pd.DataFrame({"Close": [0.90]}, index=pd.to_datetime(["2024-01-05"], utc=True))
        ticker = fake_ticker(history_df=hist)
        monkeypatch.setattr(price_service.yf, "Ticker", lambda _: ticker)

        with pytest.raises(RuntimeError, match="No historical FX rate found for USDCHF=X near 2023-12-29"):
            price_service.fetch_historical_fx_rate("USD", datetime(2023, 12, 29, tzinfo=timezone.utc))
