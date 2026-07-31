from app.services.ticker_config import CATEGORIES, MARKETS

CURRENCIES = ["USD", "CHF", "GBP", "CAD", "SGD", "EUR"]
TRANSACTION_TYPES = ["BUY", "SELL", "DIVIDEND", "DRIP"]

# Passive investment enums — lib/passive/validation.ts
PASSIVE_TYPES = ["CASH", "PENSION_FUND", "INVESTMENT_FUND", "OTHER"]
PASSIVE_TXN_TYPES = ["DEPOSIT", "WITHDRAWAL"]
RECURRING_FREQUENCIES = ["WEEKLY", "MONTHLY", "YEARLY"]
MIN_GAIN_LOSS_PCT = -100  # a fund can't lose more than 100% of contributed capital


def is_valid_market(value: str) -> bool:
    return value in MARKETS


def is_valid_category(value: str) -> bool:
    return value in CATEGORIES


def is_valid_currency(value: str) -> bool:
    return value in CURRENCIES


def is_valid_transaction_type(value: str) -> bool:
    return value in TRANSACTION_TYPES


def is_valid_passive_type(value: str) -> bool:
    return value in PASSIVE_TYPES


def is_valid_passive_txn_type(value: str) -> bool:
    return value in PASSIVE_TXN_TYPES


def is_valid_recurring_frequency(value: str) -> bool:
    return value in RECURRING_FREQUENCIES


def is_valid_gain_loss_pct(value: float | None) -> bool:
    return value is not None and value >= MIN_GAIN_LOSS_PCT
