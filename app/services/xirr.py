"""Money-weighted annualized return (XIRR) over dated cash flows.

Pure function, no I/O. The rate `r` solves

    sum(cf_i / (1 + r) ** ((d_i - d_0) / 365)) == 0

i.e. the single annual rate at which the dated deposits (negative) and
withdrawals / terminal value (positive) balance out. Unlike a plain
`(value - cost) / cost`, it accounts for *when* money went in, which is
what matters for regular monthly investing.
"""

import math
from datetime import datetime, timezone

# Shorter histories compound a few days of noise into the annual rate, so it is withheld.
MIN_DAYS_FOR_ANNUALIZED_RATE = 30

_LOWER_RATE_PER_YEAR = -0.99
_UPPER_RATE_PER_YEAR = 1000.0
_ITERATIONS = 200
_NPV_TOLERANCE = 1e-9
_RATE_INTERVAL_TOLERANCE = 1e-9
_SECONDS_PER_YEAR = 365 * 86400

CashFlow = tuple[datetime, float]


def _aware(when: datetime) -> datetime:
    return when if when.tzinfo is not None else when.replace(tzinfo=timezone.utc)


def _present_value(amount: float, rate: float, years: float) -> float:
    try:
        return amount / (1 + rate) ** years
    # The float range ends before the term's true size does: an overflowing
    # denominator makes the term vanish, an underflowing one makes it unbounded.
    except OverflowError:
        return math.copysign(0.0, amount)
    except ZeroDivisionError:
        return math.copysign(math.inf, amount)


def _npv(rate: float, flows: list[CashFlow], t0: datetime) -> float:
    return sum(
        _present_value(amount, rate, (when - t0).total_seconds() / _SECONDS_PER_YEAR)
        for when, amount in flows
    )


def _normalized(flows: list[CashFlow]) -> list[CashFlow]:
    """Non-zero flows, timezone-aware (naive = UTC), oldest first."""
    return sorted(
        ((_aware(when), amount) for when, amount in flows if amount != 0),
        key=lambda f: f[0],
    )


def _days_between_first_and_last(flows: list[CashFlow]) -> int | None:
    return (flows[-1][0] - flows[0][0]).days if len(flows) >= 2 else None


def span_days(flows: list[CashFlow]) -> int | None:
    """Days between the first and last non-zero flow, i.e. how much history
    an XIRR over these flows rests on; None with fewer than two flows."""
    return _days_between_first_and_last(_normalized(flows))


def _is_meaningful(flows: list[CashFlow], min_span_days: int) -> bool:
    has_outflow = any(a < 0 for _, a in flows)
    has_inflow = any(a > 0 for _, a in flows)
    span = _days_between_first_and_last(flows)
    return has_outflow and has_inflow and span is not None and span >= min_span_days


def _npv_signs_differ(npv_a: float, npv_b: float) -> bool:
    return (npv_a > 0) != (npv_b > 0)


def _find_rate_where_npv_is_zero(flows: list[CashFlow]) -> float | None:
    """Bisection over the searched range; None when NPV is NaN or
    doesn't change sign there."""
    t0 = flows[0][0]
    low, high = _LOWER_RATE_PER_YEAR, _UPPER_RATE_PER_YEAR
    f_low, f_high = _npv(low, flows, t0), _npv(high, flows, t0)
    if math.isnan(f_low) or math.isnan(f_high):
        return None
    if f_low == 0:
        return low
    if f_high == 0:
        return high
    if not _npv_signs_differ(f_low, f_high):
        return None

    for _ in range(_ITERATIONS):
        mid = (low + high) / 2
        f_mid = _npv(mid, flows, t0)
        if math.isnan(f_mid):
            return None
        if abs(f_mid) < _NPV_TOLERANCE or (high - low) < _RATE_INTERVAL_TOLERANCE:
            return mid
        if _npv_signs_differ(f_mid, f_low):
            high = mid
        else:
            low, f_low = mid, f_mid
    return (low + high) / 2


def xirr_pct(
    flows: list[CashFlow], *, min_span_days: int = MIN_DAYS_FOR_ANNUALIZED_RATE
) -> float | None:
    """Annualized return in percent, or None when it isn't meaningful: no
    flows, flows of only one sign (no money in or no value out), a history
    shorter than `min_span_days`, or no root in the searched range.

    Naive datetimes are treated as UTC.
    """
    flows = _normalized(flows)
    if not _is_meaningful(flows, min_span_days):
        return None
    rate = _find_rate_where_npv_is_zero(flows)
    return None if rate is None else rate * 100
