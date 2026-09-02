"""The value-investing overlay on a FundamentalsData record.

Encodes financial-sentiment-api's value-investing-checklist.md as a fixed
rubric so every evaluation applies the same lens: each metric maps to a
graded verdict ("good" / "fair" / "poor" / "n/a"), NOT a hard pass/fail,
and P/E is judged against its sector median rather than a flat 20. The
LLM interprets the verdicts; this module just makes them consistent.

SECTOR_MEDIAN_PE / REIT_SECTORS are ported verbatim from
financial-sentiment-api's app/services/valuation.py.
"""

from dataclasses import dataclass

from app.services.fundamentals.base import FundamentalsData

# Rough, illustrative per-sector median trailing P/E — not fetched live
# (no free, reliable "sector median P/E today" endpoint). Sector strings
# match yfinance's Ticker.info["sector"] values exactly. Real Estate is
# deliberately omitted: REITs are judged on Price/Book, not P/E.
SECTOR_MEDIAN_PE: dict[str, float] = {
    "Technology": 28.0,
    "Healthcare": 22.0,
    "Financial Services": 13.0,
    "Consumer Cyclical": 19.0,
    "Consumer Defensive": 21.0,
    "Communication Services": 18.0,
    "Industrials": 19.0,
    "Energy": 12.0,
    "Basic Materials": 15.0,
    "Utilities": 17.0,
}

REIT_SECTORS = {"Real Estate"}

Verdict = str  # "good" | "fair" | "poor" | "n/a"


@dataclass(frozen=True)
class MetricAssessment:
    metric: str
    value: float | None
    verdict: Verdict
    note: str

    def to_dict(self) -> dict:
        return {"metric": self.metric, "value": self.value, "verdict": self.verdict, "note": self.note}


def _na(metric: str, note: str = "no data") -> MetricAssessment:
    return MetricAssessment(metric, None, "n/a", note)


def _band(metric: str, value: float | None, good: bool, fair: bool, note: str) -> MetricAssessment:
    if value is None:
        return _na(metric)
    verdict = "good" if good else "fair" if fair else "poor"
    return MetricAssessment(metric, value, verdict, note)


def _assess_pe(data: FundamentalsData) -> MetricAssessment:
    pe = data.pe_trailing
    if pe is None:
        return _na("pe_trailing", "no trailing P/E (loss-making or missing)")
    if pe < 0:
        return MetricAssessment("pe_trailing", pe, "poor", "negative — company is loss-making on a trailing basis")
    median = SECTOR_MEDIAN_PE.get(data.sector or "")
    if median is not None:
        ratio = pe / median
        return _band(
            "pe_trailing", pe, good=ratio <= 0.85, fair=ratio <= 1.15,
            note=f"{pe:.1f} vs {data.sector} sector median {median:.0f}",
        )
    return _band("pe_trailing", pe, good=pe < 20, fair=pe <= 30, note="no sector median — judged against a flat 20/30")


def _assess_reit_pb(data: FundamentalsData) -> MetricAssessment:
    pb = data.price_to_book
    if pb is None:
        return _na("price_to_book", "no book value")
    return _band("price_to_book", pb, good=pb < 1.0, fair=pb <= 1.5, note="REIT — Price/Book is the primary value gauge")


def value_screen(data: FundamentalsData) -> dict:
    """A structured value-investing read of one holding. `is_reit` switches
    the primary valuation gauge to Price/Book (checklist's REIT section).
    """
    is_reit = (data.sector or "") in REIT_SECTORS

    metrics: list[MetricAssessment] = []
    if is_reit:
        metrics.append(_assess_reit_pb(data))
    else:
        metrics.append(_assess_pe(data))

    ps = data.price_to_sales
    metrics.append(_band("price_to_sales", ps, good=(ps is not None and ps < 1.0),
                         fair=(ps is not None and ps <= 3.0), note="revenue-relative valuation"))

    peg = data.peg_ratio
    if peg is not None and peg > 0:
        metrics.append(_band("peg_ratio", peg, good=peg < 1.0, fair=peg <= 2.0,
                             note="P/E relative to growth (Yahoo trailing PEG)"))
    else:
        metrics.append(_na("peg_ratio", "no positive growth rate — PEG not meaningful"))

    roe = data.return_on_equity
    metrics.append(_band("return_on_equity", roe, good=(roe is not None and roe >= 0.15),
                         fair=(roe is not None and roe >= 0.10), note="capital efficiency (>12-15% is the checklist bar)"))

    om = data.operating_margin
    metrics.append(_band("operating_margin", om, good=(om is not None and om >= 0.15),
                         fair=(om is not None and om >= 0.10), note="turning revenue into operating income (>10%)"))

    fcf_yield = None
    if data.free_cash_flow is not None and data.market_cap:
        fcf_yield = data.free_cash_flow / data.market_cap
    metrics.append(_band("fcf_yield", fcf_yield, good=(fcf_yield is not None and fcf_yield >= 0.05),
                         fair=(fcf_yield is not None and fcf_yield >= 0.03),
                         note="FCF / market cap — harder to game than P/E"))

    de = data.debt_to_equity
    # yfinance reports debt/equity as a percentage (e.g. 45.0 == 0.45x).
    if de is not None and de < 0:
        metrics.append(MetricAssessment("debt_to_equity", de, "poor", "negative shareholder equity"))
    else:
        metrics.append(_band("debt_to_equity", de, good=(de is not None and de < 50.0),
                             fair=(de is not None and de <= 150.0),
                             note="leverage (yfinance reports this as a percent)"))

    if data.dividend_rate:
        payout = data.payout_ratio
        metrics.append(_band("payout_ratio", payout, good=(payout is not None and payout < 0.7),
                             fair=(payout is not None and payout <= 0.9),
                             note="dividend sustainability (checklist bar: <70%)"))

    good = sum(1 for m in metrics if m.verdict == "good")
    poor = sum(1 for m in metrics if m.verdict == "poor")
    scored = [m for m in metrics if m.verdict != "n/a"]
    if not scored:
        overall = "insufficient-data"
    elif good >= poor * 2 and good >= 3:
        overall = "screens-well"
    elif poor > good:
        overall = "screens-poorly"
    else:
        overall = "mixed"

    return {
        "isReit": is_reit,
        "overall": overall,
        "goodCount": good,
        "poorCount": poor,
        "scoredCount": len(scored),
        "metrics": [m.to_dict() for m in metrics],
    }
