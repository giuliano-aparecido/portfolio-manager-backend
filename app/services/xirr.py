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


def span_days(flows: list[CashFlow]) -> int | None:
    """Days between the first and last non-zero flow, i.e. how much history
    an XIRR over these flows rests on; None with fewer than two flows."""
    dates = sorted(_aware(when) for when, amount in flows if amount != 0)
    return (dates[-1] - dates[0]).days if len(dates) >= 2 else None


def xirr_pct(flows: list[CashFlow], *, min_span_days: int = MIN_SPAN_DAYS) -> float | None:
    """Annualized return in percent, or None when it isn't meaningful: no
    flows, flows of only one sign (no money in or no value out), a history
    shorter than `min_span_days`, or no root in the searched range.

    Naive datetimes are treated as UTC.
    """
    flows = sorted(
        ((_aware(when), amount) for when, amount in flows if amount != 0),
        key=lambda f: f[0],
    )
    if len(flows) < 2:
        return None
    if not (any(a < 0 for _, a in flows) and any(a > 0 for _, a in flows)):
        return None

    t0 = flows[0][0]
    if (flows[-1][0] - t0).days < min_span_days:
        return None

    low, high = _LOWER_RATE, _UPPER_RATE
    f_low, f_high = _npv(low, flows, t0), _npv(high, flows, t0)
    if f_low == 0:
        return low * 100
    if f_high == 0:
        return high * 100
    if (f_low > 0) == (f_high > 0):
        return None  # no sign change: no root in range

    for _ in range(_ITERATIONS):
        mid = (low + high) / 2
        f_mid = _npv(mid, flows, t0)
        if abs(f_mid) < _TOLERANCE or (high - low) < _TOLERANCE:
            return mid * 100
        if (f_mid > 0) == (f_low > 0):
            low, f_low = mid, f_mid
        else:
            high = mid
    return ((low + high) / 2) * 100
