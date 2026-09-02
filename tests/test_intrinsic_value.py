"""assess_intrinsic_value — the thin structuring layer over the vendored
scenario-DCF model. Pure, no DB. Also a smoke test that the vendored
valuation.py still exposes and runs its public entry point."""

import pytest

from app.services.fundamentals.base import FundamentalsData
from app.services.fundamentals.intrinsic_value import assess_intrinsic_value
from app.services.fundamentals import valuation


def _data(**overrides) -> FundamentalsData:
    base = dict(
        symbol="TEST",
        sector="Technology",
        price=100.0,
        market_cap=50e9,
        pe_trailing=18.0,
        pe_forward=16.0,
        eps_trailing=6.0,
        book_value_per_share=20.0,
        free_cash_flow=3e9,
        total_revenue=20e9,
        dividend_rate=1.0,
        payout_ratio=0.15,
        currency="USD",
        financial_currency="USD",
        growth_0y=0.10,
        growth_1y=0.10,
    )
    base.update(overrides)
    return FundamentalsData(**base)


def test_available_result_has_intrinsic_gap_and_margin_of_safety():
    result = assess_intrinsic_value(_data(), "TEST")
    assert result["available"] is True
    assert result["currency"] == "USD"
    assert isinstance(result["intrinsic_value"], float)
    # gap and margin of safety are inverses of each other
    assert round(result["gap_percent"] + result["margin_of_safety_percent"], 6) == 0.0
    # gap is (price - intrinsic) / intrinsic * 100
    expected_gap = (100.0 - result["intrinsic_value"]) / result["intrinsic_value"] * 100
    assert abs(result["gap_percent"] - round(expected_gap, 1)) < 0.2
    assert result["valuation_basis"] in {"EPS-based", "FCF-based", "Dividend-based", "Revenue-based"}
    assert result["verdict"] in {"undervalued", "overvalued", "near fair value"}


def test_verdict_is_undervalued_when_price_is_well_below_intrinsic():
    # A cheap, growing, profitable company: price far under any reasonable DCF.
    result = assess_intrinsic_value(_data(price=20.0, eps_trailing=8.0, pe_trailing=2.5), "CHEAP")
    assert result["available"] is True
    assert result["verdict"] == "undervalued"
    assert result["margin_of_safety_percent"] > 0


def test_verdict_is_overvalued_when_price_is_well_above_intrinsic():
    result = assess_intrinsic_value(_data(price=900.0, eps_trailing=2.0, pe_trailing=450.0), "RICH")
    assert result["available"] is True
    assert result["verdict"] == "overvalued"
    assert result["gap_percent"] > 0
    assert result["margin_of_safety_percent"] < 0


def test_unavailable_when_there_is_no_price():
    result = assess_intrinsic_value(_data(price=None), "NOPRICE")
    assert result["available"] is False
    assert result["reason"] == "Data unavailable."
    assert "intrinsic_value" not in result


def test_unavailable_for_a_zero_price_without_dividing_by_zero():
    # gap_pct is exactly -100 at price 0; the intrinsic-recovery divide
    # would be 0/0 without the guard.
    result = assess_intrinsic_value(_data(price=0.0), "ZERO")
    assert result["available"] is False


def test_unavailable_when_the_model_has_no_usable_basis():
    # Loss-making, no revenue, no book value, no FCF -> revenue basis with
    # nothing to project -> "Not applicable".
    result = assess_intrinsic_value(
        _data(eps_trailing=-1.0, pe_trailing=None, total_revenue=None, free_cash_flow=None,
              book_value_per_share=None, dividend_rate=None, payout_ratio=None, market_cap=None),
        "NOBASIS",
    )
    assert result["available"] is False
    assert "applicable" in result["reason"].lower()


def test_vendored_valuation_module_exposes_its_public_entry_point():
    assert callable(valuation.valuation_assessment_for)
    block, gap = valuation.valuation_assessment_for(_data().to_payload(), ticker="TEST")
    assert isinstance(block, str) and block
    assert gap is None or isinstance(gap, float)


# A broken/truncated copy of the vendored model would crash or produce
# nonsense on at least one of these; fsa owns the calibration accuracy.
_PROFILES = {
    "profitable-tech": dict(sector="Technology", eps_trailing=6.0, book_value_per_share=20.0, pe_trailing=25.0,
                            pe_forward=22.0, free_cash_flow=8e9, payout_ratio=0.1, dividend_rate=1.0),
    "bank": dict(sector="Financial Services", eps_trailing=5.0, book_value_per_share=45.0, pe_trailing=11.0,
                 pe_forward=10.0, free_cash_flow=None, payout_ratio=0.3, dividend_rate=2.0),
    "mature-dividend-payer": dict(sector="Consumer Defensive", eps_trailing=4.0, book_value_per_share=6.0,
                                  pe_trailing=20.0, pe_forward=19.0, free_cash_flow=5e9, payout_ratio=0.75,
                                  dividend_rate=3.2),
    "reit": dict(sector="Real Estate", eps_trailing=1.5, book_value_per_share=30.0, pe_trailing=35.0,
                 pe_forward=33.0, free_cash_flow=1e9, payout_ratio=0.9, dividend_rate=4.0),
    "unprofitable-growth": dict(sector="Technology", eps_trailing=-2.0, book_value_per_share=5.0, pe_trailing=None,
                                pe_forward=None, free_cash_flow=-5e8, total_revenue=3e9, payout_ratio=None,
                                dividend_rate=None),
}


@pytest.mark.parametrize("name", list(_PROFILES))
def test_vendored_model_runs_across_business_profiles(name):
    result = assess_intrinsic_value(_data(price=100.0, market_cap=40e9, **_PROFILES[name]), name.upper())
    assert set(result) >= {"available"}
    if result["available"]:
        assert result["intrinsic_value"] > 0
        assert result["valuation_basis"] in {"EPS-based", "FCF-based", "Dividend-based", "Revenue-based"}
        assert -100.0 <= result["margin_of_safety_percent"] or result["gap_percent"] is not None
