import pytest

from app.services.ticker_config import TICKER_CONFIGS, derive_yahoo_ticker

# ticker -> expected yahoo_ticker, exercising every one of the 21 known
# tickers table-driven, plus the derived (non-table) cases.
KNOWN_TICKER_CASES = [(ticker, cfg.yahoo_ticker) for ticker, cfg in TICKER_CONFIGS.items()]


@pytest.mark.parametrize("ticker,expected_yahoo_ticker", KNOWN_TICKER_CASES)
def test_derive_yahoo_ticker_for_all_known_tickers(ticker: str, expected_yahoo_ticker: str) -> None:
    config = TICKER_CONFIGS[ticker]
    assert derive_yahoo_ticker(ticker, config.market) == expected_yahoo_ticker


def test_all_21_known_tickers_present() -> None:
    assert len(TICKER_CONFIGS) == 21


def test_bats_has_mixed_price_and_dividend_units() -> None:
    cfg = TICKER_CONFIGS["BATS"]
    assert cfg.price_divisor == 1
    assert cfg.dividend_divisor == 100


def test_caml_mndi_thrl_both_divided_by_100() -> None:
    for ticker in ("CAML", "MNDI", "THRL"):
        cfg = TICKER_CONFIGS[ticker]
        assert cfg.price_divisor == 100
        assert cfg.dividend_divisor == 100


def test_is_chf_native_property() -> None:
    assert TICKER_CONFIGS["NESN"].is_chf_native is True
    assert TICKER_CONFIGS["AMZN"].is_chf_native is False


def test_unknown_crypto_ticker_gets_usd_suffix() -> None:
    assert derive_yahoo_ticker("ETH", "CRYPTO") == "ETH-USD"


def test_unknown_ticker_gets_market_suffix() -> None:
    assert derive_yahoo_ticker("NOVN", "SIX") == "NOVN.SW"
    assert derive_yahoo_ticker("TSCO", "LON") == "TSCO.L"
    assert derive_yahoo_ticker("SHOP", "TSX") == "SHOP.TO"
    assert derive_yahoo_ticker("D05", "SGX") == "D05.SI"


def test_unknown_ticker_on_nyse_nasdaq_gets_no_suffix() -> None:
    assert derive_yahoo_ticker("XOM", "NYSE") == "XOM"
    assert derive_yahoo_ticker("MSFT", "NASDAQ") == "MSFT"


def test_unknown_ticker_with_share_class_dot_becomes_hyphen() -> None:
    assert derive_yahoo_ticker("BF.B", "NYSE") == "BF-B"
