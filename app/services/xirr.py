"""Money-weighted annualized return (XIRR) over dated cash flows.

Pure function, no I/O. The rate `r` solves

    sum(cf_i / (1 + r) ** ((d_i - d_0) / 365)) == 0

i.e. the single annual rate at which the dated deposits (negative) and
withdrawals / terminal value (positive) balance out. Unlike a plain
`(value - cost) / cost`, it accounts for *when* money went in, which is
what matters for regular monthly investing.
"""

from datetime import datetime, timezone

# Below this span the rate is pure noise (a few days of movement compounded
# to a year), so it is withheld. Between this and a year it IS shown, but
# callers should say how much history it rests on (see span_days): +4% over
# three months annualizes to ~+17%, which is correct but a projection.
MIN_SPAN_DAYS = 30

_LOWER_RATE = -0.99  # -99% / year
_UPPER_RATE = 1000.0  # +100000% / year
_ITERATIONS = 200
_TOLERANCE = 1e-9

CashFlow = tuple[datetime, float]


def _aware(when: datetime) -> datetime:
    return when if when.tzinfo is not None else when.replace(tzinfo=timezone.utc)


def _npv(rate: float, flows: list[CashFlow], t0: datetime) -> float:
    total = 0.0
    for when, amount in flows:
        years = (when - t0).total_seconds() / (365 * 86400)
        try:
            total += amount / (1 + rate) ** years
        except OverflowError:
            total += float("inf") if amount > 0 else float("-inf")
    return total


def _normalized(flows: list[CashFlow]) -> list[CashFlow]:
    """Non-zero flows, timezone-aware (naive = UTC), oldest first."""
    return sorted(
        ((_aware(when), amount) for when, amount in flows if amount != 0),
        key=lambda f: f[0],
    )


def _span_days(flows: list[CashFlow]) -> int | None:
    return (flows[-1][0] - flows[0][0]).days if len(flows) >= 2 else None


def span_days(flows: list[CashFlow]) -> int | None:
    """Days between the first and last non-zero flow, i.e. how much history
    an XIRR over these flows rests on; None with fewer than two flows."""
    return _span_days(_normalized(flows))


def _is_meaningful(flows: list[CashFlow], min_span_days: int) -> bool:
    """Needs money both in and out, over at least `min_span_days`."""
    has_outflow = any(a < 0 for _, a in flows)
    has_inflow = any(a > 0 for _, a in flows)
    span = _span_days(flows)
    return has_outflow and has_inflow and span is not None and span >= min_span_days


def _solve_rate(flows: list[CashFlow]) -> float | None:
    """Bisection for the rate where NPV is zero, as a fraction; None when
    NPV doesn't change sign within the searched range."""
    t0 = flows[0][0]
    low, high = _LOWER_RATE, _UPPER_RATE
    f_low, f_high = _npv(low, flows, t0), _npv(high, flows, t0)
    if f_low == 0:
        return low
    if f_high == 0:
        return high
    if (f_low > 0) == (f_high > 0):
        return None

    for _ in range(_ITERATIONS):
        mid = (low + high) / 2
        f_mid = _npv(mid, flows, t0)
        if abs(f_mid) < _TOLERANCE or (high - low) < _TOLERANCE:
            return mid
        if (f_mid > 0) == (f_low > 0):
            low, f_low = mid, f_mid
        else:
            high = mid
    return (low + high) / 2


def xirr_pct(flows: list[CashFlow], *, min_span_days: int = MIN_SPAN_DAYS) -> float | None:
    """Annualized return in percent, or None when it isn't meaningful: no
    flows, flows of only one sign (no money in or no value out), a history
    shorter than `min_span_days`, or no root in the searched range.

    Naive datetimes are treated as UTC.
    """
    flows = _normalized(flows)
    if not _is_meaningful(flows, min_span_days):
        return None
    rate = _solve_rate(flows)
    return None if rate is None else rate * 100
