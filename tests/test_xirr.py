from datetime import datetime, timedelta, timezone

import pytest

from app.services.xirr import span_days, xirr_pct

T0 = datetime(2024, 1, 1, tzinfo=timezone.utc)
HALF_YEAR_PLUS = 200


def test_single_deposit_matches_simple_annual_return() -> None:
    # 1000 in, 1100 out exactly one year later -> 10%.
    result = xirr_pct([(T0, -1000), (T0 + timedelta(days=365), 1100)])
    assert result == pytest.approx(10.0, abs=1e-4)


def test_monthly_deposits_differ_from_simple_return() -> None:
    # 12 monthly deposits of 100, worth 1300 after a year. Simple return on
    # cost is 8.3%, but the average dollar was invested ~half a year, so the
    # annualized figure is roughly double.
    flows = [(T0 + timedelta(days=30 * i), -100.0) for i in range(12)]
    flows.append((T0 + timedelta(days=365), 1300.0))
    result = xirr_pct(flows)
    assert result is not None
    assert 15 < result < 20


def test_negative_return() -> None:
    result = xirr_pct([(T0, -1000), (T0 + timedelta(days=365), 900)])
    assert result == pytest.approx(-10.0, abs=1e-4)


def test_withdrawal_in_the_middle() -> None:
    flows = [(T0, -1000), (T0 + timedelta(days=180), 500), (T0 + timedelta(days=365), 600)]
    result = xirr_pct(flows)
    assert result is not None and result > 0


def test_none_when_history_too_short() -> None:
    assert xirr_pct([(T0, -1000), (T0 + timedelta(days=10), 1100)]) is None


def test_short_but_over_minimum_history_is_shown() -> None:
    # ~4 months: the rate is computed (annualized), not withheld.
    result = xirr_pct([(T0, -1000), (T0 + timedelta(days=120), 1040)])
    assert result is not None and 12 < result < 14


def test_none_when_only_one_sign() -> None:
    assert xirr_pct([(T0, -1000), (T0 + timedelta(days=365), -50)]) is None
    assert xirr_pct([(T0, 1000), (T0 + timedelta(days=365), 50)]) is None


def test_none_when_empty_or_single_flow() -> None:
    assert xirr_pct([]) is None
    assert xirr_pct([(T0, -1000)]) is None


def test_total_loss_is_none_or_deeply_negative() -> None:
    result = xirr_pct([(T0, -1000), (T0 + timedelta(days=365), 1)])
    assert result is None  # beyond the searched range, withheld rather than wrong


def test_unsorted_input_is_handled() -> None:
    a = xirr_pct([(T0 + timedelta(days=365), 1100), (T0, -1000)])
    assert a == pytest.approx(10.0, abs=1e-4)


def test_naive_datetimes_treated_as_utc() -> None:
    naive = [(datetime(2024, 1, 1), -1000), (datetime(2025, 1, 1) - timedelta(days=1), 1100)]
    aware_mixed = [(T0, -1000), (datetime(2025, 1, 1) - timedelta(days=1), 1100)]
    assert xirr_pct(naive) == pytest.approx(xirr_pct(aware_mixed))


def test_span_days() -> None:
    assert span_days([(T0, -1000), (T0 + timedelta(days=HALF_YEAR_PLUS), 1050)]) == HALF_YEAR_PLUS
    assert span_days([(T0, -1000)]) is None
    assert span_days([]) is None


def test_minimum_history_boundary() -> None:
    assert xirr_pct([(T0, -1000), (T0 + timedelta(days=29), 1010)]) is None
    assert xirr_pct([(T0, -1000), (T0 + timedelta(days=30), 1010)]) is not None


def test_span_days_ignores_zero_flows() -> None:
    flows = [(T0, -1000), (T0 + timedelta(days=400), 0), (T0 + timedelta(days=100), 1050)]
    assert span_days(flows) == 100
