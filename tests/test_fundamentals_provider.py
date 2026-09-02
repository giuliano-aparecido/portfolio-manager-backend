"""YahooFundamentalsProvider — the yfinance `.info` adapter. yfinance is
faked at its two entry points (Ticker / Search), matching the repo's
monkeypatch style; no network, no DB."""

import pytest

import app.services.fundamentals.yahoo_provider as yp
from app.services.fundamentals.base import FundamentalsRateLimited, FundamentalsUnavailable
from app.services.fundamentals.yahoo_provider import YahooFundamentalsProvider, _is_rate_limit_error, _to_data


class _FakeTicker:
    def __init__(self, info):
        self._info = info

    @property
    def info(self):
        if isinstance(self._info, Exception):
            raise self._info
        return self._info


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
    monkeypatch.setattr(yp.yf, "Ticker", lambda s: _FakeTicker({}))
    monkeypatch.setattr(yp.yf, "Search", lambda q: _FakeSearch([]))
    with pytest.raises(FundamentalsUnavailable):
        YahooFundamentalsProvider().fetch("VWRA.SW")


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
