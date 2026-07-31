"""Postgres advisory-lock helpers for mutation routes that validate a
whole sibling set (FIFO integrity, cash-ledger integrity) before writing.

That read-validate-write sequence has no single row to take a
SELECT ... FOR UPDATE on — the thing being protected is the *set* of
transactions for a ticker/investment, not one row. Two concurrent
requests can each read the same pre-mutation set, each independently
validate as OK, and then both commit, producing a combined result that
violates the invariant they each individually satisfied (e.g. two
concurrent SELLs that each look fine against the current share count, but
together oversell).

pg_advisory_xact_lock(key) blocks other sessions requesting the same key
until the current transaction commits or rolls back, then releases
automatically — no explicit unlock, and safe even if the request errors
out before committing (the session's connection gets rolled back when
returned to the pool). Call it *before* the "existing rows" query so the
second caller actually blocks until the first commits, then re-reads the
first's committed write as part of its own validation.
"""

from sqlalchemy import text
from sqlalchemy.orm import Session


def lock_portfolio_ticker(db: Session, user_id: str, ticker: str) -> None:
    db.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"portfolio_ticker:{user_id}:{ticker}"},
    )


def lock_passive_investment(db: Session, investment_id: int) -> None:
    db.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"passive_investment:{investment_id}"},
    )
