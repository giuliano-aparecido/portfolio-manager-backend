# Value Investing Checklist

The rubric the fundamentals agent tools apply (see
`app/services/fundamentals/screen.py`). Ported from
`financial-sentiment-api`'s `value-investing-checklist.md`.

Not hard cutoffs — each metric maps to a confidence band, not a pass/fail
gate (e.g. P/E < 20 → 0.7–0.9 confidence; 20–30 → 0.3–0.5; >30 →
low/negative). `screen.py` collapses these to `good` / `fair` / `poor`.

## General
- P/E judged against the sector median, not a flat 20 (fixes flat P/E
  punishing software and rewarding banks for no reason)
- Price/Sales < 1
- Operating margin > 10% (turning revenue into operating/net income)
- ROE > 12–15%
- PEG ratio (P/E ÷ growth rate) — catches "cheap but growth is negative
  too" and "expensive but justified by growth", which P/E alone can't
- FCF yield (FCF / market cap) — harder to game with accounting than P/E
- Debt/equity — leverage / solvency check
- Insider ownership / insider buying — behavioral signal, qualitative
  (not available from the Yahoo provider)
- Check the 1st analyst target estimate and the next earnings date
  (not available from the Yahoo provider)

## Dividends
- Payout ratio < 70%
- History: increased over the years, or at least constant every year
  (not available from the Yahoo provider)

## REITs
- Price/Book < 1 (the primary value gauge — P/E is unreliable for the
  sector because of large non-cash depreciation charges)
