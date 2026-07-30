from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models import PassiveInvestment, PassiveRecurringDeposit, PassiveTransaction, User
from app.services.recurring import (
    MAX_OCCURRENCES_PER_PASS,
    compute_due_occurrences,
    materialize_due_recurring_deposits,
    occurrence_date,
)


def d(date_str: str) -> datetime:
    return datetime.fromisoformat(date_str).replace(tzinfo=timezone.utc)


def iso(date: datetime) -> str:
    return date.date().isoformat()


class TestOccurrenceDate:
    def test_monthly_from_jan_31_clamps_and_snaps_back(self) -> None:
        start = d("2023-01-31")
        assert iso(occurrence_date(start, "MONTHLY", 0)) == "2023-01-31"
        assert iso(occurrence_date(start, "MONTHLY", 1)) == "2023-02-28"  # non-leap Feb
        assert iso(occurrence_date(start, "MONTHLY", 2)) == "2023-03-31"  # snaps back, not stuck at 28
        assert iso(occurrence_date(start, "MONTHLY", 12)) == "2024-01-31"
        assert iso(occurrence_date(start, "MONTHLY", 13)) == "2024-02-29"  # leap year Feb

    def test_yearly_from_feb_29_clamps_in_non_leap_years(self) -> None:
        start = d("2024-02-29")
        assert iso(occurrence_date(start, "YEARLY", 0)) == "2024-02-29"
        assert iso(occurrence_date(start, "YEARLY", 1)) == "2025-02-28"
        assert iso(occurrence_date(start, "YEARLY", 4)) == "2028-02-29"

    def test_weekly_advances_in_exact_7_day_increments(self) -> None:
        start = d("2024-03-01")
        assert iso(occurrence_date(start, "WEEKLY", 0)) == "2024-03-01"
        assert iso(occurrence_date(start, "WEEKLY", 1)) == "2024-03-08"
        assert iso(occurrence_date(start, "WEEKLY", 4)) == "2024-03-29"


class TestComputeDueOccurrences:
    def test_full_backlog_from_last_generated_none(self) -> None:
        due = compute_due_occurrences(d("2024-01-01"), "MONTHLY", None, d("2024-04-01"))
        assert [iso(x) for x in due] == ["2024-01-01", "2024-02-01", "2024-03-01", "2024-04-01"]

    def test_idempotent_repeat_call_returns_nothing_new(self) -> None:
        until = d("2024-04-01")
        first = compute_due_occurrences(d("2024-01-01"), "MONTHLY", None, until)
        second = compute_due_occurrences(d("2024-01-01"), "MONTHLY", first[-1], until)
        assert second == []

    def test_caps_at_max_occurrences_and_continues_on_chained_call(self) -> None:
        until = d("2100-01-01")
        first = compute_due_occurrences(d("1980-01-01"), "WEEKLY", None, until)
        assert len(first) == MAX_OCCURRENCES_PER_PASS
        second = compute_due_occurrences(d("1980-01-01"), "WEEKLY", first[-1], until)
        assert len(second) > 0
        assert second[0] > first[-1]

    def test_returns_empty_when_start_after_until(self) -> None:
        assert compute_due_occurrences(d("2025-01-01"), "MONTHLY", None, d("2024-01-01")) == []

    def test_returns_one_occurrence_when_start_equals_until(self) -> None:
        due = compute_due_occurrences(d("2024-01-01"), "MONTHLY", None, d("2024-01-01"))
        assert [iso(x) for x in due] == ["2024-01-01"]

    def test_end_date_clips_the_scan_inclusive(self) -> None:
        due = compute_due_occurrences(d("2024-01-01"), "MONTHLY", None, d("2024-06-01"), d("2024-03-01"))
        assert [iso(x) for x in due] == ["2024-01-01", "2024-02-01", "2024-03-01"]

    def test_stays_correct_across_a_mid_stream_schedule_change(self) -> None:
        until1 = d("2024-04-01")
        monthly = compute_due_occurrences(d("2024-01-01"), "MONTHLY", None, until1)
        last_generated = monthly[-1]

        # Simulate editing the rule's frequency to WEEKLY after this point.
        until2 = d("2024-05-01")
        after_edit = compute_due_occurrences(d("2024-01-01"), "WEEKLY", last_generated, until2)

        for occ in after_edit:
            assert occ > last_generated
        assert len(after_edit) > 0


class TestMaterializeDueRecurringDeposits:
    def _make_investment(self, db_session: Session) -> PassiveInvestment:
        user = User(email="recurring-test@example.com")
        db_session.add(user)
        db_session.flush()
        investment = PassiveInvestment(user_id=user.id, name="Pension", type="PENSION_FUND", currency="CHF")
        db_session.add(investment)
        db_session.flush()
        return investment

    def test_creates_deposit_rows_and_advances_last_generated_date(self, db_session: Session) -> None:
        investment = self._make_investment(db_session)
        rule = PassiveRecurringDeposit(
            passive_investment_id=investment.id,
            amount_native=100,
            start_date=d("2024-01-01"),
            frequency="MONTHLY",
        )
        db_session.add(rule)
        db_session.flush()

        materialize_due_recurring_deposits(db_session, passive_investment_id=investment.id, until=d("2024-03-01"))

        txns = (
            db_session.query(PassiveTransaction)
            .filter(PassiveTransaction.passive_investment_id == investment.id)
            .order_by(PassiveTransaction.date)
            .all()
        )
        assert [iso(t.date) for t in txns] == ["2024-01-01", "2024-02-01", "2024-03-01"]
        assert all(t.type == "DEPOSIT" and t.amount_native == 100 and t.notes == "Recurring deposit" for t in txns)

        db_session.refresh(rule)
        assert iso(rule.last_generated_date) == "2024-03-01"

    def test_uses_rule_notes_verbatim_when_set(self, db_session: Session) -> None:
        investment = self._make_investment(db_session)
        rule = PassiveRecurringDeposit(
            passive_investment_id=investment.id,
            amount_native=50,
            start_date=d("2024-01-01"),
            frequency="MONTHLY",
            notes="Salary contribution",
        )
        db_session.add(rule)
        db_session.flush()

        materialize_due_recurring_deposits(db_session, passive_investment_id=investment.id, until=d("2024-01-01"))

        txn = db_session.query(PassiveTransaction).filter(PassiveTransaction.passive_investment_id == investment.id).one()
        assert txn.notes == "Salary contribution"

    def test_no_op_when_nothing_due(self, db_session: Session) -> None:
        investment = self._make_investment(db_session)
        rule = PassiveRecurringDeposit(
            passive_investment_id=investment.id,
            amount_native=50,
            start_date=d("2024-01-01"),
            frequency="MONTHLY",
            last_generated_date=d("2024-05-01"),
        )
        db_session.add(rule)
        db_session.flush()

        materialize_due_recurring_deposits(db_session, passive_investment_id=investment.id, until=d("2024-05-01"))

        count = db_session.query(PassiveTransaction).filter(PassiveTransaction.passive_investment_id == investment.id).count()
        assert count == 0

    def test_no_op_with_neither_user_id_nor_investment_id(self, db_session: Session) -> None:
        # Should not raise and should not query anything.
        materialize_due_recurring_deposits(db_session)

    def test_respects_end_date(self, db_session: Session) -> None:
        investment = self._make_investment(db_session)
        rule = PassiveRecurringDeposit(
            passive_investment_id=investment.id,
            amount_native=10,
            start_date=d("2024-01-01"),
            frequency="MONTHLY",
            end_date=d("2024-02-01"),
        )
        db_session.add(rule)
        db_session.flush()

        materialize_due_recurring_deposits(db_session, passive_investment_id=investment.id, until=d("2024-06-01"))

        txns = db_session.query(PassiveTransaction).filter(PassiveTransaction.passive_investment_id == investment.id).all()
        assert [iso(t.date) for t in txns] == ["2024-01-01", "2024-02-01"]
