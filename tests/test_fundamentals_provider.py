"""YahooFundamentalsProvider — the yfinance `.info` adapter. yfinance is
faked at its two entry points (Ticker / Search), matching the repo's
monkeypatch style; no network, no DB."""

import pandas as pd
import pytest

import app.services.fundamentals.yahoo_provider as yp
from app.services.fundamentals.base import FundamentalsRateLimited, FundamentalsUnavailable
from app.services.fundamentals.yahoo_provider import (
    YahooFundamentalsProvider,
    _fetch_growth_consensus,
    _fetch_recent_eps_surprise,
    _is_rate_limit_error,
    _to_data,
)


class _FakeTicker:
    def __init__(self, info, *, earnings_estimate=None, earnings_dates=None):
        self._info = info
        self._earnings_estimate = earnings_estimate
        self._earnings_dates = earnings_dates

    @property
    def info(self):
        if isinstance(self._info, Exception):
            raise self._info
        return self._info

    @property
    def earnings_estimate(self):
        if isinstance(self._earnings_estimate, Exception):
            raise self._earnings_estimate
        return self._earnings_estimate

    @property
    def earnings_dates(self):
        if isinstance(self._earnings_dates, Exception):
            raise self._earnings_dates
        return self._earnings_dates


def _estimate_df(*, growth_0y=0.08, growth_1y=0.09, year_ago=6.0, low=5.7, high=6.5):
    return pd.DataFrame(
        {"growth": [growth_0y, growth_1y], "yearAgoEps": [year_ago, year_ago], "low": [low, low], "high": [high, high]},
        index=["0y", "+1y"],
    )


def _dates_df(*, estimate=1.5, reported=1.6):
    return pd.DataFrame({"EPS Estimate": [estimate], "Reported EPS": [reported]})


class _FakeSearch:
    def __init__(self, quotes):
        self.quotes = quotes


FULL_INFO = {
    "shortName": "Apple Inc.",
    "sector": "Technology",
    "industry": "Consumer Electronics",
    "currency": "USD",
    "financialCurrency": "USD",
    "currentPrice": 189.30,
    "marketCap": 2.95e12,
    "trailingPE": 31.2,
    "forwardPE": 27.8,
    "trailingPegRatio": 2.1,
    "priceToBook": 44.5,
    "priceToSalesTrailing12Months": 7.6,
    "enterpriseToEbitda": 22.0,
    "trailingEps": 6.07,
    "bookValue": 4.25,
    "returnOnEquity": 1.42,
    "operatingMargins": 0.30,
    "profitMargins": 0.25,
    "grossMargins": 0.45,
    "revenueGrowth": 0.08,
    "earningsGrowth": 0.11,
    "freeCashflow": 100e9,
    "totalRevenue": 390e9,
    "totalDebt": 110e9,
    "totalCash": 60e9,
    "ebitda": 130e9,
    "debtToEquity": 140.0,
    "currentRatio": 0.95,
    "dividendYield": 0.55,
    "dividendRate": 1.0,
    "payoutRatio": 0.15,
    "fiftyTwoWeekLow": 164.08,
    "fiftyTwoWeekHigh": 237.23,
}


def test_to_data_maps_every_info_field():
    data = _to_data("AAPL", FULL_INFO)
    assert data.symbol == "AAPL"
    assert data.company_name == "Apple Inc."
    assert data.pe_trailing == 31.2
    assert data.peg_ratio == 2.1
    assert data.return_on_equity == 1.42
    assert data.free_cash_flow == 100e9
    assert data.debt_to_equity == 140.0
    assert data.dividend_rate == 1.0
    assert data.year_high == 237.23


def test_to_data_drops_nan_values():
    data = _to_data("AAPL", {**FULL_INFO, "trailingPE": float("nan")})
    assert data.pe_trailing is None


def test_to_data_drops_non_numeric_strings_in_numeric_fields():
    data = _to_data("AAPL", {**FULL_INFO, "trailingPE": "N/A", "returnOnEquity": None})
    assert data.pe_trailing is None
    assert data.return_on_equity is None
    assert data.sector == "Technology"  # string fields are untouched


def test_fetch_succeeds_directly(monkeypatch):
    monkeypatch.setattr(yp.yf, "Ticker", lambda s: _FakeTicker(FULL_INFO))
    data = YahooFundamentalsProvider().fetch("AAPL")
    assert data.symbol == "AAPL"
    assert data.market_cap == 2.95e12


def test_fetch_retries_with_resolved_symbol_when_bare_one_is_empty(monkeypatch):
    tickers = {"NESN": {}, "NESN.SW": {"currentPrice": 92.5, "marketCap": 250e9}}
    monkeypatch.setattr(yp.yf, "Ticker", lambda s: _FakeTicker(tickers[s]))
    monkeypatch.setattr(yp.yf, "Search", lambda q: _FakeSearch([{"symbol": "NESN.SW", "quoteType": "EQUITY"}]))
    data = YahooFundamentalsProvider().fetch("NESN")
    # symbol stays the caller's — resolution is only used to locate data
    assert data.symbol == "NESN"
    assert data.price == 92.5


def test_fetch_raises_unavailable_when_the_call_succeeds_but_carries_no_data(monkeypatch):
    # A genuine no-fundamentals security still carries substantive
    # metadata (quoteType, exchange, etc.) even without a price/market
    # cap - that's what distinguishes it from a degraded response below.
    monkeypatch.setattr(
        yp.yf, "Ticker",
        lambda s: _FakeTicker({"quoteType": "ETF", "shortName": "Vanguard FTSE All-World UCITS ETF", "currency": "USD"}),
    )
    monkeypatch.setattr(yp.yf, "Search", lambda q: _FakeSearch([]))
    with pytest.raises(FundamentalsUnavailable):
        YahooFundamentalsProvider().fetch("VWRA.SW")


def test_fetch_raises_a_retryable_error_on_a_degraded_empty_response(monkeypatch):
    # Regression: yfinance can return a near-empty `.info` dict (no price,
    # no quoteType, no exception raised) on a transient Yahoo hiccup -
    # confirmed live, this misclassified UBER (an ordinary NYSE equity) as
    # permanently "unavailable" for the rest of the day. Without a real
    # quoteType, "no price" must NOT be trusted as a genuine no-
    # fundamentals verdict - it has to stay retryable like any other
    # transient failure.
    monkeypatch.setattr(yp.yf, "Ticker", lambda s: _FakeTicker({}))
    monkeypatch.setattr(yp.yf, "Search", lambda q: _FakeSearch([]))
    with pytest.raises(RuntimeError):
        YahooFundamentalsProvider().fetch("UBER")


def test_fetch_raises_a_retryable_error_on_a_stripped_equity_response(monkeypatch):
    # Regression #2: confirmed live in production, a throttled/shared-IP
    # Yahoo response can carry quoteType="EQUITY" while still stripping
    # the actual price data - a plain quoteType check alone wasn't enough
    # (this is exactly what UBER hit again after the first fix deployed).
    # An EQUITY always trades with a live price when Yahoo has real data,
    # so this must stay retryable too, not become a permanent verdict.
    monkeypatch.setattr(
        yp.yf, "Ticker",
        lambda s: _FakeTicker({"quoteType": "EQUITY", "shortName": "Uber Technologies, Inc.", "currency": "USD"}),
    )
    monkeypatch.setattr(yp.yf, "Search", lambda q: _FakeSearch([]))
    with pytest.raises(RuntimeError):
        YahooFundamentalsProvider().fetch("UBER")


def test_fetch_propagates_a_transient_error_rather_than_calling_it_unavailable(monkeypatch):
    # A timeout / 5xx must not be cached as "this security has no
    # fundamentals" — it has to stay retryable.
    monkeypatch.setattr(yp.yf, "Ticker", lambda s: _FakeTicker(RuntimeError("HTTPSConnectionPool: Read timed out")))
    with pytest.raises(RuntimeError):
        YahooFundamentalsProvider().fetch("AAPL")


def test_fetch_raises_rate_limited_on_429(monkeypatch):
    monkeypatch.setattr(yp.yf, "Ticker", lambda s: _FakeTicker(RuntimeError("429 Client Error: Too Many Requests")))
    with pytest.raises(FundamentalsRateLimited):
        YahooFundamentalsProvider().fetch("AAPL")


def test_fetch_raises_rate_limited_for_yfinance_rate_limit_error_class(monkeypatch):
    class YFRateLimitError(Exception):
        pass

    monkeypatch.setattr(yp.yf, "Ticker", lambda s: _FakeTicker(YFRateLimitError("slow down")))
    with pytest.raises(FundamentalsRateLimited):
        YahooFundamentalsProvider().fetch("AAPL")


@pytest.mark.parametrize(
    "message",
    ["429 Too Many Requests", "HTTP Error 429", "you have hit the rate limit", "Rate-Limit exceeded"],
)
def test_is_rate_limit_error_recognises_common_markers(message):
    assert _is_rate_limit_error(RuntimeError(message)) is True


def test_is_rate_limit_error_false_for_ordinary_failures():
    assert _is_rate_limit_error(RuntimeError("connection reset")) is False


# --- analyst-consensus fetches (scenario-DCF inputs) ---


def test_fetch_growth_consensus_reads_the_0y_and_1y_rows(monkeypatch):
    monkeypatch.setattr(yp.yf, "Ticker", lambda s: _FakeTicker({}, earnings_estimate=_estimate_df()))
    out = _fetch_growth_consensus("AAPL")
    assert out["growth_0y"] == 0.08
    assert out["growth_1y"] == 0.09
    assert round(out["growth_0y_low"], 4) == round((5.7 - 6.0) / 6.0, 4)
    assert round(out["growth_0y_high"], 4) == round((6.5 - 6.0) / 6.0, 4)


def test_fetch_growth_consensus_empty_when_year_ago_eps_is_zero(monkeypatch):
    monkeypatch.setattr(yp.yf, "Ticker", lambda s: _FakeTicker({}, earnings_estimate=_estimate_df(year_ago=0.0)))
    assert _fetch_growth_consensus("AAPL") == {
        "growth_0y": None, "growth_1y": None, "growth_0y_low": None, "growth_0y_high": None
    }


def test_fetch_growth_consensus_empty_on_ordinary_failure(monkeypatch):
    monkeypatch.setattr(yp.yf, "Ticker", lambda s: _FakeTicker({}, earnings_estimate=RuntimeError("no table")))
    assert _fetch_growth_consensus("AAPL")["growth_0y"] is None


def test_fetch_growth_consensus_raises_rate_limited_on_429(monkeypatch):
    monkeypatch.setattr(
        yp.yf, "Ticker", lambda s: _FakeTicker({}, earnings_estimate=RuntimeError("429 Too Many Requests"))
    )
    with pytest.raises(FundamentalsRateLimited):
        _fetch_growth_consensus("AAPL")


def test_fetch_recent_eps_surprise_is_a_fraction(monkeypatch):
    monkeypatch.setattr(yp.yf, "Ticker", lambda s: _FakeTicker({}, earnings_dates=_dates_df(estimate=1.5, reported=1.6)))
    assert round(_fetch_recent_eps_surprise("AAPL"), 4) == round((1.6 - 1.5) / 1.5, 4)


def test_fetch_recent_eps_surprise_none_when_no_reported_rows(monkeypatch):
    empty = pd.DataFrame({"EPS Estimate": [1.5], "Reported EPS": [float("nan")]})
    monkeypatch.setattr(yp.yf, "Ticker", lambda s: _FakeTicker({}, earnings_dates=empty))
    assert _fetch_recent_eps_surprise("AAPL") is None


def test_fetch_recent_eps_surprise_raises_rate_limited_on_429(monkeypatch):
    monkeypatch.setattr(
        yp.yf, "Ticker", lambda s: _FakeTicker({}, earnings_dates=RuntimeError("HTTP Error 429"))
    )
    with pytest.raises(FundamentalsRateLimited):
        _fetch_recent_eps_surprise("AAPL")


def test_fetch_populates_the_dcf_input_fields(monkeypatch):
    monkeypatch.setattr(
        yp.yf,
        "Ticker",
        lambda s: _FakeTicker(FULL_INFO, earnings_estimate=_estimate_df(), earnings_dates=_dates_df()),
    )
    data = YahooFundamentalsProvider().fetch("AAPL")
    assert data.growth_0y == 0.08
    assert data.growth_1y == 0.09
    assert data.recent_eps_surprise is not None
    assert data.pe_trailing == 31.2  # base .info fields still present


def test_fetch_skips_the_consensus_endpoints_when_there_is_no_eps(monkeypatch):
    # A loss-making / pre-profit name is valued on the revenue basis, which
    # uses neither the EPS growth consensus nor the trailing-EPS surprise —
    # those 2 extra Yahoo calls must not fire.
    calls: list[str] = []
    monkeypatch.setattr(yp, "_fetch_growth_consensus", lambda s: calls.append("growth") or {})
    monkeypatch.setattr(yp, "_fetch_recent_eps_surprise", lambda s: calls.append("surprise"))
    no_eps = {k: v for k, v in FULL_INFO.items() if k != "trailingEps"}  # FULL_INFO has no forwardEps either
    monkeypatch.setattr(yp.yf, "Ticker", lambda s: _FakeTicker(no_eps))

    data = YahooFundamentalsProvider().fetch("LOSSCO")

    assert calls == []
    assert data.growth_0y is None
    assert data.market_cap == 2.95e12  # the .info fields still come through


def test_fetch_survives_missing_consensus_tables(monkeypatch):
    # A stock with .info but no earnings_estimate/earnings_dates must still
    # return a full FundamentalsData, just with the DCF inputs None.
    monkeypatch.setattr(
        yp.yf,
        "Ticker",
        lambda s: _FakeTicker(FULL_INFO, earnings_estimate=RuntimeError("n/a"), earnings_dates=RuntimeError("n/a")),
    )
    data = YahooFundamentalsProvider().fetch("AAPL")
    assert data.market_cap == 2.95e12
    assert data.growth_0y is None
    assert data.recent_eps_surprise is None
