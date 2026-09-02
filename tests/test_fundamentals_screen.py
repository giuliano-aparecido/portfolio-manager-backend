"""value_screen() — the value-investing rubric in
app/services/fundamentals/screen.py. Pure functions, no DB."""

from app.services.fundamentals.base import FundamentalsData
from app.services.fundamentals.screen import SECTOR_MEDIAN_PE, value_screen


def _data(**overrides) -> FundamentalsData:
    base = dict(
        symbol="TEST",
        sector="Technology",
        pe_trailing=18.0,
        price_to_sales=0.8,
        peg_ratio=0.9,
        return_on_equity=0.22,
        operating_margin=0.25,
        free_cash_flow=6e9,
        market_cap=100e9,
        debt_to_equity=30.0,
    )
    base.update(overrides)
    return FundamentalsData(**base)


def _verdict(screen: dict, metric: str) -> str:
    return next(m["verdict"] for m in screen["metrics"] if m["metric"] == metric)


def test_pe_is_graded_against_the_sector_median_not_a_flat_20():
    # Same P/E of 18, opposite verdicts depending on the sector median:
    # comfortably below Technology's 28 -> "good"; well above Financial
    # Services' 13 -> "poor". A flat-20 rule would grade both identically.
    assert _verdict(value_screen(_data(pe_trailing=18.0, sector="Technology")), "pe_trailing") == "good"
    assert _verdict(value_screen(_data(pe_trailing=18.0, sector="Financial Services")), "pe_trailing") == "poor"
    # Just above the median lands in the middle band.
    assert _verdict(value_screen(_data(pe_trailing=14.0, sector="Financial Services")), "pe_trailing") == "fair"


def test_pe_without_a_known_sector_falls_back_to_flat_bands():
    screen = value_screen(_data(sector="Some New GICS Sector", pe_trailing=15.0))
    assert _verdict(screen, "pe_trailing") == "good"
    assert "flat 20/30" in next(m["note"] for m in screen["metrics"] if m["metric"] == "pe_trailing")


def test_negative_pe_is_poor():
    assert _verdict(value_screen(_data(pe_trailing=-5.0)), "pe_trailing") == "poor"


def test_reits_are_judged_on_price_to_book_not_pe():
    screen = value_screen(_data(sector="Real Estate", price_to_book=0.85, pe_trailing=4.0))
    assert screen["isReit"] is True
    metrics = {m["metric"] for m in screen["metrics"]}
    assert "price_to_book" in metrics
    assert "pe_trailing" not in metrics
    assert _verdict(screen, "price_to_book") == "good"


def test_peg_is_na_when_growth_is_not_positive():
    assert _verdict(value_screen(_data(peg_ratio=None)), "peg_ratio") == "n/a"
    assert _verdict(value_screen(_data(peg_ratio=-1.2)), "peg_ratio") == "n/a"


def test_fcf_yield_is_computed_from_fcf_over_market_cap():
    screen = value_screen(_data(free_cash_flow=6e9, market_cap=100e9))  # 6% -> good
    fcf = next(m for m in screen["metrics"] if m["metric"] == "fcf_yield")
    assert round(fcf["value"], 4) == 0.06
    assert fcf["verdict"] == "good"


def test_fcf_yield_na_without_market_cap():
    assert _verdict(value_screen(_data(market_cap=None)), "fcf_yield") == "n/a"


def test_payout_ratio_only_scored_for_dividend_payers():
    no_div = value_screen(_data(dividend_rate=None, payout_ratio=None))
    assert all(m["metric"] != "payout_ratio" for m in no_div["metrics"])
    payer = value_screen(_data(dividend_rate=2.0, payout_ratio=0.5))
    assert _verdict(payer, "payout_ratio") == "good"


def test_overall_screens_well_when_goods_dominate():
    screen = value_screen(
        _data(pe_trailing=12.0, price_to_sales=0.5, peg_ratio=0.6, return_on_equity=0.3, operating_margin=0.3,
              free_cash_flow=8e9, market_cap=100e9, debt_to_equity=10.0)
    )
    assert screen["overall"] == "screens-well"


def test_overall_screens_poorly_when_poors_dominate():
    screen = value_screen(
        _data(pe_trailing=90.0, price_to_sales=25.0, peg_ratio=8.0, return_on_equity=0.01, operating_margin=0.01,
              free_cash_flow=1e8, market_cap=500e9, debt_to_equity=400.0)
    )
    assert screen["overall"] == "screens-poorly"


def test_overall_insufficient_data_when_nothing_scores():
    screen = value_screen(
        FundamentalsData(symbol="X", sector=None, pe_trailing=None, price_to_sales=None, peg_ratio=None,
                         return_on_equity=None, operating_margin=None, free_cash_flow=None, market_cap=None,
                         debt_to_equity=None)
    )
    assert screen["overall"] == "insufficient-data"


def test_sector_median_pe_table_has_no_real_estate_entry():
    assert "Real Estate" not in SECTOR_MEDIAN_PE
