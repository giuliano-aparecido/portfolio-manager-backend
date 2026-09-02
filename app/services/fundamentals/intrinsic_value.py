"""Turns the vendored scenario-DCF model (valuation.py) into the
structured shape the agent tools return.

The whole model — basis classification, curated overrides, the
payout-threshold blend, the fallback chain — lives in `valuation.py` and
is reached only through its public `valuation_assessment_for`. That
returns `(rendered_block, gap_pct)` where `gap_pct` is the raw, uncapped
`(price - intrinsic) / intrinsic * 100`, so the intrinsic value is
recovered exactly as `price / (1 + gap_pct / 100)` — no re-implementation
of the model here.
"""

import re

from app.services.fundamentals.base import FundamentalsData
from app.services.fundamentals.valuation import valuation_assessment_for

# The block always leads with "Intrinsic Value (EPS-based): $..." — the
# basis label is the only structured field the block carries that the
# (block, gap) pair doesn't already give us numerically.
_BASIS_RE = re.compile(r"Intrinsic Value \(([^)]+)\)")

_NEAR_FAIR_BAND = 1.0  # percent; matches valuation_block's own "trading near fair value" band


def assess_intrinsic_value(data: FundamentalsData, ticker: str) -> dict:
    """Structured scenario-DCF assessment for one security, in its own
    trading currency. `available` is False when the model can't produce a
    number (no price, or the classified basis has no usable input) —
    `reason` then carries the model's own wording.
    """
    block, gap_pct = valuation_assessment_for(data.to_payload(), ticker=ticker)
    price = data.price

    # The intrinsic-recovery divide below is `price / (1 + gap_pct/100)`.
    # Enforce its preconditions here rather than trusting the vendored
    # model to keep guaranteeing gap_pct is None whenever price <= 0: a
    # non-positive price (gap_pct == -100 -> 0/0) or any gap_pct that
    # drives the denominator to ~0 makes the result meaningless.
    if gap_pct is None or price is None or price <= 0 or abs(1 + gap_pct / 100) < 1e-9:
        return {
            "available": False,
            "reason": block,
            "currency": data.currency,
            "current_price": price,
        }

    intrinsic = price / (1 + gap_pct / 100)
    if gap_pct > _NEAR_FAIR_BAND:
        verdict = "overvalued"
    elif gap_pct < -_NEAR_FAIR_BAND:
        verdict = "undervalued"
    else:
        verdict = "near fair value"
    basis_match = _BASIS_RE.search(block)

    return {
        "available": True,
        "currency": data.currency,
        "current_price": round(price, 2),
        "intrinsic_value": round(intrinsic, 2),
        "gap_percent": round(gap_pct, 1),  # positive = overvalued
        "margin_of_safety_percent": round(-gap_pct, 1),  # positive = buying below intrinsic
        "verdict": verdict,
        "valuation_basis": basis_match.group(1) if basis_match else None,
        "assessment": block,
    }
