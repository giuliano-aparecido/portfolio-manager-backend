"""Recurring passive-deposit occurrence math and on-demand catch-up
materialization — ported verbatim from lib/passive/recurring.ts.

No real cron: materialize_due_recurring_deposits is a side-effecting
function meant to be called from GET routes (detail page load, rollup
load), so deposits only ever get written to the DB "when the app is
opened."
"""

import calendar
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import PassiveInvestment, PassiveRecurringDeposit, PassiveTransaction

MAX_OCCURRENCES_PER_PASS = 520  # ~10 years of WEEKLY, the densest frequency
MAX_SCAN_ITERATIONS = 20000  # hard backstop on total loop iterations


def occurrence_date(start_date: datetime, frequency: str, n: int) -> datetime:
    """Computes the n-th occurrence (0-indexed; n=0 = start_date itself),
    always directly from start_date — never by iteratively mutating a
    running date (avoids the classic "Jan 31 + 1 month = Mar 3" bug). All
    arithmetic is UTC-only.
    """
    if frequency == "WEEKLY":
        return start_date + timedelta(days=7 * n)

    y = start_date.year
    m0 = start_date.month - 1  # 0-indexed, matches the original's getUTCMonth()
    d = start_date.day

    months_to_add = n if frequency == "MONTHLY" else n * 12
    total_months = m0 + months_to_add
    target_year = y + total_months // 12
    target_month = total_months % 12 + 1  # back to 1-indexed

    # The last real day of target_month, recomputed fresh each call — this
    # correctly handles Feb 28 vs 29 leap years, 30- vs 31-day months, and
    # (critically) makes a Jan-31 rule snap back to Mar 31 the following
    # month rather than drifting, since each n is computed independently.
    last_day_of_target_month = calendar.monthrange(target_year, target_month)[1]
    day = min(d, last_day_of_target_month)

    return datetime(target_year, target_month, day, tzinfo=timezone.utc)


def today_utc_midnight() -> datetime:
    now = datetime.now(timezone.utc)
    return datetime(now.year, now.month, now.day, tzinfo=timezone.utc)


def compute_due_occurrences(
    start_date: datetime,
    frequency: str,
    last_generated_date: datetime | None,
    until: datetime,
    end_date: datetime | None = None,
    max_occurrences: int = MAX_OCCURRENCES_PER_PASS,
) -> list[datetime]:
    """Always scans occurrences from n=0 and decides what's "already
    generated" by comparing each candidate occurrence's actual timestamp to
    last_generated_date — never by recomputing/trusting an occurrence
    index. This is what makes it safe to call after start_date/frequency
    have been edited mid-stream.
    """
    effective_until = end_date if (end_date is not None and end_date < until) else until

    due: list[datetime] = []
    n = 0
    scanned = 0
    while len(due) < max_occurrences and scanned < MAX_SCAN_ITERATIONS:
        occ = occurrence_date(start_date, frequency, n)
        if occ > effective_until:
            break
        if last_generated_date is None or occ > last_generated_date:
            due.append(occ)
        n += 1
        scanned += 1
    return due


def materialize_due_recurring_deposits(
    db: Session,
    *,
    user_id: str | None = None,
    passive_investment_id: int | None = None,
    until: datetime | None = None,
) -> None:
    if passive_investment_id is None and not user_id:
        return
    if until is None:
        until = today_utc_midnight()

    query = select(PassiveRecurringDeposit)
    if passive_investment_id is not None:
        query = query.where(PassiveRecurringDeposit.passive_investment_id == passive_investment_id)
    else:
        query = query.join(PassiveInvestment).where(PassiveInvestment.user_id == user_id)

    rules = db.scalars(query).all()
    for rule in rules:
        due = compute_due_occurrences(rule.start_date, rule.frequency, rule.last_generated_date, until, rule.end_date)
        if not due:
            continue
        for date in due:
            db.add(
                PassiveTransaction(
                    passive_investment_id=rule.passive_investment_id,
                    type="DEPOSIT",
                    date=date,
                    amount_native=rule.amount_native,
                    notes=rule.notes or "Recurring deposit",
                )
            )
        rule.last_generated_date = due[-1]
        db.commit()
