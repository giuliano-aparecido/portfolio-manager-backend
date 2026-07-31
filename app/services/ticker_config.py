"""Hardcoded configuration table for 21 tickers, originally imported from
spreadsheet CSVs. Not derived from any market/suffix heuristic for these
specific tickers (verified empirically against Yahoo Finance).
"""

from dataclasses import dataclass

MARKETS = ["NYSE", "NASDAQ", "SIX", "LON", "TSX", "SGX", "CRYPTO"]
CATEGORIES = ["Stock", "Stock Defensive", "REITS", "Gold", "Crypto", "Berkshire", "IBM"]


@dataclass(frozen=True)
class TickerConfig:
    ticker: str  # canonical symbol
    csv_ticker: str  # symbol as it appeared in the original CSV filename
    yahoo_ticker: str  # verified Yahoo Finance symbol
    native_currency: str  # "USD" | "CHF" | "GBP" | "CAD" | "SGD" | "EUR"
    market: str
    category: str
    price_divisor: float  # divide raw CSV price by this to normalize to major units
    dividend_divisor: float  # divide raw CSV dividend-per-share by this

    @property
    def is_chf_native(self) -> bool:
        return self.native_currency == "CHF"


# Notable comments/edge cases baked into this table:
# - CSV header currency text is untrustworthy (THRL's header says "GBP" but
#   values are GBX/pence) — this table is the trusted source, confirmed
#   with the portfolio owner.
# - VITL-UN: file was named after old ticker NWH-UN-T; correct symbol is
#   VITL-UN (Toronto, CAD).
# - BATS: buy/sell prices are in GBP but dividend-per-share values are in
#   GBX (pence) — hence dividend_divisor=100 while price_divisor=1, a
#   mixed-unit quirk within the same source file.
# - CAML/MNDI/THRL: both price and dividend need /100 (both quoted in pence).
_RAW_CONFIGS: list[tuple[str, str, str, str, str, str, float, float]] = [
    ("AMZN", "AMZN", "AMZN", "USD", "NASDAQ", "Stock", 1, 1),
    ("BRK.B", "BRK.B", "BRK-B", "USD", "NYSE", "Berkshire", 1, 1),
    ("GOOGL", "GOOGL", "GOOGL", "USD", "NASDAQ", "Stock", 1, 1),
    ("IBM", "IBM", "IBM", "USD", "NYSE", "IBM", 1, 1),
    ("PBR", "PBR", "PBR", "USD", "NYSE", "Stock", 1, 1),
    ("VALE", "VALE", "VALE", "USD", "NYSE", "Stock", 1, 1),
    ("NVO", "NVO", "NVO", "USD", "NYSE", "Stock", 1, 1),
    ("BTC", "BTC", "BTC-USD", "USD", "CRYPTO", "Crypto", 1, 1),
    ("LND", "LND", "LND", "USD", "NYSE", "Stock Defensive", 1, 1),
    ("IWDP", "IWDP", "IWDP.SW", "USD", "SIX", "REITS", 1, 1),
    ("M1GU", "M1GU", "M1GU.SI", "SGD", "SGX", "REITS", 1, 1),
    ("AUUSI", "AUUSI", "AUUSI.SW", "CHF", "SIX", "Gold", 1, 1),
    ("BKW", "BKW", "BKW.SW", "CHF", "SIX", "Stock Defensive", 1, 1),
    ("NESN", "NESN", "NESN.SW", "CHF", "SIX", "Stock Defensive", 1, 1),
    ("VHYL", "VHYL", "VHYL.SW", "CHF", "SIX", "Stock", 1, 1),
    ("VWRA", "VWRA", "VWRA.SW", "CHF", "SIX", "Stock", 1, 1),
    ("VITL-UN", "NWH", "VITL-UN.TO", "CAD", "TSX", "REITS", 1, 1),
    ("BATS", "BATS", "BATS.L", "GBP", "LON", "Stock Defensive", 1, 100),
    ("CAML", "CAML", "CAML.L", "GBP", "LON", "Stock", 100, 100),
    ("MNDI", "MNDI", "MNDI.L", "GBP", "LON", "Stock", 100, 100),
    ("THRL", "THRL", "THRL.L", "GBP", "LON", "REITS", 100, 100),
]

TICKER_CONFIGS: dict[str, TickerConfig] = {row[0]: TickerConfig(*row) for row in _RAW_CONFIGS}

# Used only for tickers not in TICKER_CONFIGS, i.e. added later via the UI.
MARKET_YAHOO_SUFFIX: dict[str, str] = {
    "NYSE": "",
    "NASDAQ": "",
    "SIX": ".SW",
    "LON": ".L",
    "TSX": ".TO",
    "SGX": ".SI",
    "CRYPTO": "",
}


def derive_yahoo_ticker(ticker: str, market: str) -> str:
    config = TICKER_CONFIGS.get(ticker)
    if config is not None:
        return config.yahoo_ticker
    if market == "CRYPTO":
        return f"{ticker}-USD"
    # Yahoo represents share classes with a hyphen, e.g. BRK.B -> BRK-B.
    base = ticker.replace(".", "-")
    return base + MARKET_YAHOO_SUFFIX[market]
