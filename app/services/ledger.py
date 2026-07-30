"""Passive-investment cash ledger — ported verbatim from
lib/passive/ledger.ts.
"""

from dataclasses import dataclass
from datetime import datetime


@dataclass
class PassiveLedgerTxn:
    date: datetime
    type: str  # "DEPOSIT" | "WITHDRAWAL"
    amount_native: float


def compute_net_balance(transactions: list[PassiveLedgerTxn]) -> float:
    """Order-independent — used for display (rollup/detail cost basis),
    where only the final total matters.
    """
    return sum(t.amount_native if t.type == "DEPOSIT" else -t.amount_native for t in transactions)


def validate_cash_ledger_integrity(transactions: list[PassiveLedgerTxn]) -> dict:
    """Order-sensitive — a running balance sorted by date must never dip
    negative, even if the final net balance would be non-negative under a
    different ordering. Rejects a mutation up front rather than silently
    accepting bad data.
    """
    sorted_txns = sorted(transactions, key=lambda t: t.date)
    balance = 0.0
    for t in sorted_txns:
        balance += t.amount_native if t.type == "DEPOSIT" else -t.amount_native
        if balance < -1e-9:
            return {
                "valid": False,
                "error": f"Withdrawal on {t.date.date().isoformat()} would bring the balance negative ({balance:.2f})",
            }
    return {"valid": True}
