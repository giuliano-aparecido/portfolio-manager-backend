# ─────────────────────────────────────────────────────────────────────────
# VENDORED VERBATIM from financial-sentiment-api @ 8283313
#   (app/services/valuation.py). Do NOT edit here — re-sync by re-copying
#   the file when the source changes. See docs/value-investing-checklist.md
#   and this repo's fundamentals package docstring for why this is a copy
#   rather than an MCP call (Yahoo Finance rate-limit isolation: this app's
#   requests must not share a 429 bucket with financial-sentiment-api's).
#   The public entry point used here is `valuation_assessment_for`.
# ─────────────────────────────────────────────────────────────────────────

# Scenario-weighted, 2-stage DCF-style intrinsic-value model. Replaces an
# earlier Graham Number implementation (sqrt(22.5 x EPS x book value/share))
# that was confirmed live to be badly broken for asset-light, buyback-heavy
# companies - it showed Apple at "725% overvalued", Tesla at "1321%
# overvalued", purely because Graham Number treats book value as a proxy for
# a company's worth, which fails hard when most of a company's value is
# intangible (brand, ecosystem, IP) rather than balance-sheet assets.
#
# This model classifies each company into one of four valuation bases - the
# metric actually being valued differs by business type, since a single
# metric can't meaningfully value both a bank and a pre-profit growth
# company - and projects THAT metric across two growth stages plus an
# exit-multiple terminal value, discounted back, averaged across three
# scenarios (Normal/Best/Worst). All four bases are treated as per-share
# EQUITY cash flows (not enterprise value), specifically so no net-debt
# bridge is needed - a documented simplification, not a hidden one,
# consistent with this project's existing "Not applicable"/"Data
# unavailable" fail-soft pattern.
#
# History of rejected/replaced approaches, for the record:
#   1. Graham's own growth-adjusted revision (V = EPS x (8.5+2g) x 4.4/Y)
#      using yfinance's raw earningsGrowth field - made things WORSE
#      (Tesla -> 4374%), because that field is a noisy single-quarter YoY
#      number (confirmed live: NVDA showed 214%, XOM 112%), not the
#      smoothed long-term rate the formula assumes.
#   2. A self-invented per-basis growth-tier table (different g1/g2/exit
#      multiple per FCF/EPS/Dividend/Revenue basis) with a CAPM-derived,
#      per-company discount rate from beta - fixed most tickers but left
#      Tesla and Nvidia badly broken (Tesla "overvalued" by 1583-2289%
#      across variants tried), because high-beta names got an inflated
#      discount rate on top of already-conservative growth assumptions,
#      double-punishing exactly the names that needed the opposite.
#   3. A flat, universal SCENARIOS table (same g1/g2/exit_multiple for
#      EVERY company, same 60/20/20 probability weights) - resolved most of
#      variant 2's outliers, but was confirmed live against a real
#      investor-analyst's own per-company DCF assumptions (5 tickers:
#      NVDA/MSFT/PEP/NFLX/XOM) to be wrong in two structural ways, not just
#      mistuned constants:
#        a. Probability weights should be equal (1/3 each), not 60/20/20.
#        b. For "eps"/"fcf"/"revenue" bases, summing all 10 years of
#           projected cash flow AND adding a terminal value double-counts -
#           that projected cash flow isn't actually paid to the
#           shareholder each year (unlike a dividend), so only the
#           discounted terminal (eventual sale) price should count. This
#           was the single biggest source of error (NVDA/MSFT/NFLX were all
#           40-70%+ too high under the old full-sum formula; matched within
#           2-12% once switched to terminal-only - see
#           scenario_terminal_value below).
#      Growth/exit-multiple were also confirmed to genuinely vary by
#      company (not universal) - see CURATED_SCENARIOS and build_scenarios
#      below for how per-company inputs are now sourced.
#
# Deterministic, code-only math - never LLM-generated - matching
# financial-sentiment-model's training-data generators (see that
# repo's CONTRIBUTING.md 4-way sync rule for why the RENDERED BLOCK FORMAT
# needs to stay in step with them; the formula/constants below are not yet
# ported there - see this repo's PR history for the staged-rollout plan).

import logging

logger = logging.getLogger(__name__)

STAGE_1_YEARS = 5
STAGE_2_YEARS = 5

# Sector strings match yfinance's Ticker.info["sector"] values exactly.
ASSET_HEAVY_SECTORS = {"Energy", "Industrials", "Basic Materials", "Utilities"}
# Raised 0.40 -> 0.55 after live data across 26 real tickers: names sitting
# just above the old 0.40 (LOW 0.406, QCOM 0.410, AVGO 0.413, CSCO 0.498)
# all showed LOW dividend yields (0.66-2.29%) - the market clearly isn't
# pricing them on dividend income the way it obviously is for names with a
# higher payout AND higher yield (VZ 0.728/5.84%, MO 0.893/6.45%, PEP
# 0.753/4.20%). Concretely: QCOM's real consensus derived a plausible-
# looking-but-wrong "$22.90, overvalued ~150%" (masked by
# VALUATION_PCT_DISPLAY_CAP - the raw gap was 624%) BECAUSE it landed on
# this basis at all, not because of any bad growth input - a $3.68
# dividend against a $165.79 price structurally can't produce anything
# near that price under a dividend-discount model no matter how the
# growth rate is bounded (confirmed live: even a flat 0% g1/g2 caps out
# around $51). This also fixes a separate, real, currently-live bug: XOM
# (real payout_ratio 0.525) was classified "dividends" here while its
# CURATED_SCENARIOS entry is calibrated for "eps" (see that dict's own
# comment - reproduces a real analyst's target within 2-12%) - the
# mismatch made build_scenarios' basis-match check correctly refuse the
# curated data and silently fall through to a generic, uncurated
# valuation instead. 0.55 excludes XOM (0.525) from this basis too,
# realigning it with its curated "eps" tag without needing to guess new
# dividends-basis numbers for it. MMM (0.536, 1.71% yield) also moves to
# "eps" as a side effect - consistent with the same low-yield pattern,
# and MMM isn't curated so there's no calibration to preserve either way.
DIVIDEND_PAYOUT_THRESHOLD = 0.55  # payout_ratio >= this -> treated as a mature dividend payer

# A payout_ratio above this means the company is paying out MORE than its
# entire trailing earnings - confirmed live (DSM-Firmenich, mid its 2023
# merger, showed payout_ratio=1.79) that this isn't a genuine "mature
# cash-cow" payout POLICY like Kinder Morgan's stable ~0.76, it's a
# mechanical artifact of a transiently earnings-crushed company holding
# its dividend flat. Above this ceiling, the payout-ratio check below is
# skipped so the company falls through to the asset-heavy/fcf or eps
# checks instead, same fail-soft reasoning as the rest of this
# classifier. Chosen a little above 1.0 (not exactly 1.0) so a company
# paying out fractionally more than one bad quarter's earnings isn't
# needlessly excluded - the DSM case (1.79) is nowhere near this edge.
DIVIDEND_PAYOUT_CEILING = 1.20

# Width of the payout-ratio band straddling DIVIDEND_PAYOUT_THRESHOLD
# where the basis is BLENDED instead of hard-switched - confirmed live
# the hard cutoff produced a 28.5% intrinsic-value jump for a 0.001
# payout-ratio change (EPS $10, g1 5%: eps-basis $153.61 vs dividends-
# basis $109.87), because crossing the threshold flips the entire
# FORMULA, not just an input. A company at payout 0.548 is not
# meaningfully different from one at 0.552 and shouldn't be valued 28%
# apart - see valuation_block_for's blend logic. Only applies to
# non-curated, non-REIT tickers: curated tickers' basis is a human
# override (see CURATED_SCENARIOS_BASIS), not a live classification call,
# so there's nothing to blend; REITs are a sector override (GAAP
# depreciation makes eps/payout unreliable for that sector specifically -
# see REIT_SECTORS), not a payout-ratio judgment call this band applies to.
DIVIDEND_PAYOUT_BLEND_HALF_WIDTH = 0.05

# Fallback order tried when the chosen basis (curated override, REIT
# override, or classify_valuation_basis's own pick) has no usable cf0 for
# this company right now - see valuation_block_for's fallback loop. "fcf"
# ranked above "dividends" - confirmed live "dividends" ranked first was
# a real bug: cash_flow_basis_value("dividends", ...) succeeds for ANY
# company with a nonzero dividend_rate, with no payout-ratio gate at all
# (unlike classify_valuation_basis's own routing), so a low-yield growth
# company whose eps got screened out (e.g. GOOG via EARNINGS_SURPRISE_
# ONE_TIME_ITEM_THRESHOLD) fell back to valuing itself on a token $0.88
# dividend instead of its real free cash flow - the exact "forced into
# an inappropriate dividend model" failure DIVIDEND_PAYOUT_THRESHOLD was
# raised to fix elsewhere in this module, just reached via a different
# path. Not every basis needs to be reachable from every starting point;
# this is just an exhaustive, fixed order so the loop terminates
# deterministically.
FALLBACK_BASIS_ORDER = ["fcf", "eps", "dividends", "revenue"]

# REITs are legally required to distribute ~90% of TAXABLE income as
# dividends, but yfinance's payoutRatio is computed against GAAP earnings,
# which real-estate accounting depresses with large non-cash depreciation
# charges - a REIT can be distributing effectively all its real cash flow
# while showing a deceptively low GAAP payout ratio (confirmed live:
# Aedifica, a real REIT, showed payout_ratio=0.34 - below
# DIVIDEND_PAYOUT_THRESHOLD - which routed it to "eps" instead of
# "dividends"). Same reasoning means GAAP EPS itself is unreliable for this
# sector (depreciation can push it to near-zero or negative even for a
# healthy REIT), so this is checked as an unconditional sector override,
# ahead of the profitability check below - not just an addition to the
# payout-ratio check.
REIT_SECTORS = {"Real Estate"}

# Rough, illustrative per-sector median trailing P/E - not fetched live (no
# free, reliable "sector median P/E today" endpoint). Moved here from
# fundamentals.py (which now imports it back) so build_scenarios below can
# anchor the "eps"/"fcf" exit multiple to it - see NORMAL_EXIT_MULTIPLE's
# comment for why a flat 20x contradicted this same table for banks/
# energy/utilities. Real Estate deliberately omitted: REIT_SECTORS already
# routes those to a dividends/P/B-based valuation instead of P/E.
SECTOR_MEDIAN_PE = {
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

BASIS_LABELS = {
    "revenue": "Revenue-based",
    "eps": "EPS-based",
    "fcf": "FCF-based",
    "dividends": "Dividend-based",
}

# Backstop cap on the displayed over/undervalued percentage - see
# valuation_block's own comment for why this exists alongside G1_CAP
# rather than instead of it.
VALUATION_PCT_DISPLAY_CAP = 150.0

# Flat for every company (not risk-adjusted per company) - deliberately
# simpler than a CAPM/beta-derived rate, and specifically what resolved the
# high-beta-name double-punishment problem described in the module history
# above. Unlike g1/g2/exit_multiple below, this one constant was NOT
# contradicted by the live analyst comparison, so it's kept as-is.
DISCOUNT_RATE = 0.10

# Confirmed live (see module history, point 3a): the analyst's three
# scenarios were weighted equally, not 60/20/20.
SCENARIO_PROBABILITY = 1 / 3

# Exact per-company scenario assumptions from a real investor analyst's own
# DCF, keyed by ticker - used verbatim (bypassing build_scenarios' derived/
# generic logic below) whenever the incoming ticker matches. g1 = years 1-5
# growth, g2 = years 6-10 growth, exit_multiple applied to year-10's
# projected cash flow. Confirmed these reproduce the analyst's own target
# price within 2-12% once combined with equal weighting and the
# terminal-only formula (see scenario_terminal_value) for the non-dividend
# bases - NVDA/MSFT/NFLX/PEP were all 40-70%+ off under the old flat model.
CURATED_SCENARIOS = {
    "AAPL": {
        # Confirmed live: reproduces the analyst's own $128 target within
        # 2.6% ($124.65 at trailing EPS $8.26, the analyst's own cf0).
        "normal": {"g1": 0.07, "g2": 0.07, "exit_multiple": 20.0},
        "best": {"g1": 0.12, "g2": 0.07, "exit_multiple": 25.0},
        "worst": {"g1": 0.05, "g2": 0.05, "exit_multiple": 10.0},
    },
    "NVDA": {
        "normal": {"g1": 0.30, "g2": 0.10, "exit_multiple": 20.0},
        "best": {"g1": 0.30, "g2": 0.15, "exit_multiple": 25.0},
        "worst": {"g1": 0.05, "g2": 0.05, "exit_multiple": 10.0},
    },
    "MSFT": {
        "normal": {"g1": 0.15, "g2": 0.10, "exit_multiple": 20.0},
        "best": {"g1": 0.20, "g2": 0.10, "exit_multiple": 25.0},
        "worst": {"g1": 0.05, "g2": 0.05, "exit_multiple": 12.0},
    },
    "PEP": {
        "normal": {"g1": 0.03, "g2": 0.03, "exit_multiple": 20.0},
        "best": {"g1": 0.05, "g2": 0.05, "exit_multiple": 25.0},
        "worst": {"g1": 0.03, "g2": -0.05, "exit_multiple": 15.0},
    },
    "NFLX": {
        "normal": {"g1": 0.12, "g2": 0.10, "exit_multiple": 20.0},
        "best": {"g1": 0.15, "g2": 0.12, "exit_multiple": 25.0},
        "worst": {"g1": 0.08, "g2": 0.06, "exit_multiple": 15.0},
    },
    "XOM": {
        "normal": {"g1": 0.04, "g2": 0.04, "exit_multiple": 20.0},
        "best": {"g1": 0.06, "g2": 0.06, "exit_multiple": 30.0},
        "worst": {"g1": 0.03, "g2": 0.03, "exit_multiple": 12.0},
    },
}

# The basis each CURATED_SCENARIOS ticker's growth/exit-multiple
# assumptions were actually calibrated against. Applying growth assumptions
# calibrated for one basis's cash flow (e.g. AAPL's trailing EPS) to a
# DIFFERENT basis's cash flow (e.g. its much smaller dividend rate) doesn't
# produce a "less accurate" number, it produces one with no relationship to
# the analyst's actual target at all - the growth/exit-multiple assumptions
# and the cf0 they're meant to compound have to come from the same DCF.
#
# valuation_block_for overrides classify_valuation_basis's output with this
# tag outright for any curated ticker, rather than merely checking the two
# agree - confirmed live that classify_valuation_basis's result can
# legitimately drift for reasons that have nothing to do with whether the
# curated numbers are still valid (e.g. raising DIVIDEND_PAYOUT_THRESHOLD to
# fix QCOM's misrouting silently reclassified XOM from "dividends" to "fcf",
# not the "eps" this table was calibrated for, leaving a human-verified
# ticker's curated data unused as a side effect of an unrelated constant).
# build_scenarios' own basis-match check below still applies for callers
# that reach it directly without going through that override (e.g. tests).
CURATED_SCENARIOS_BASIS = {
    "AAPL": "eps",
    "NVDA": "eps",
    "MSFT": "eps",
    "PEP": "dividends",
    "NFLX": "eps",
    "XOM": "eps",
}

# Fallback for any ticker not in CURATED_SCENARIOS, segmented by basis and
# sector along the two axes that were actually confirmed live rather than
# invented - see build_scenarios for how these combine with a per-ticker
# derived g1.
#
# Exit multiples: normal (20.0x) and best (25.0x) cluster tightly across
# ALL 5 analyst examples regardless of company - genuine fixed defaults.
# Worst-case is the one exit multiple with a confirmed (if partial) sector
# signal: XOM (Energy, commodity-cycle exposed) got 12.0x vs PEP (Consumer
# Defensive, non-cyclical) at 15.0x - asset-heavy/cyclical sectors get the
# lower figure. This does NOT explain the full spread (NVDA 10.0x vs MSFT
# 12.0x are both Technology and still differ by 2 points with no available
# signal to split them further) - that residual is deliberately averaged
# over via WORST_EXIT_MULTIPLE_DEFAULT rather than guessed at.
WORST_EXIT_MULTIPLE_ASSET_HEAVY = 12.0
WORST_EXIT_MULTIPLE_DEFAULT = 13.0
NORMAL_EXIT_MULTIPLE = 20.0
BEST_EXIT_MULTIPLE = 25.0

# Separate, much lower exit multiples for the "revenue" basis specifically -
# reusing NORMAL_EXIT_MULTIPLE/BEST_EXIT_MULTIPLE/WORST_EXIT_MULTIPLE_* here
# was a bug: those are P/E- and P/FCF-grade multiples, confirmed only
# against NVDA/MSFT/PEP/NFLX/XOM (all eps/dividends-basis companies), and
# applying a 20-25x earnings-style multiple to per-share REVENUE instead
# massively overstates intrinsic value - confirmed live: FLUT (routed to
# "revenue" because it's unprofitable) came back ~94% undervalued at a
# $1747.78 intrinsic value against a ~$99 price, i.e. priced as if its
# SALES traded at Nvidia's earnings multiple. The "revenue" basis exists
# specifically for UNPROFITABLE companies (see classify_valuation_basis
# rule 2) - exactly the group that deserves the LEAST generous multiple,
# not the same one as a profitable mega-cap.
#
# These are a conservative, general P/S-multiple heuristic (typical
# real-world P/S ratios run roughly 1-6x outside of high-margin, hyper-
# growth SaaS names) - NOT yet confirmed against a real analyst's own
# revenue-basis DCF the way the constants above are. Revisit if a real
# example surfaces to calibrate against, same as this module's other
# constants.
REVENUE_WORST_EXIT_MULTIPLE = 1.0
REVENUE_NORMAL_EXIT_MULTIPLE = 3.0
REVENUE_BEST_EXIT_MULTIPLE = 6.0

# A theoretically-motivated separate, HIGHER set of dividends-basis exit
# multiples (implying a more realistic ~3% terminal yield vs a P/E-style
# 20x's ~5%) was tried and reverted here: confirmed live against QSR (a
# real, non-curated dividends-basis ticker) that raising the multiple
# made the gap WORSE, not better (23.5% -> 58.5% vs the analyst's own
# number) - the theoretical "20x implies too rich a terminal yield"
# argument doesn't survive contact with the one real calibration point
# available for this basis. PEP (CURATED_SCENARIOS, the only other real
# calibration point) uses a 15-25x range via its OWN hand-vetted table,
# bypassing this constant entirely - so there is no confirmed evidence
# this basis's fair exit multiple differs from eps/fcf's at all. Reverted
# to sharing NORMAL_EXIT_MULTIPLE/BEST_EXIT_MULTIPLE/WORST_EXIT_MULTIPLE_
# DEFAULT with eps/fcf below (not sector-anchored the way eps/fcf now
# are - no calibration evidence for that either, and dividends-basis
# sectors like Utilities/REITs/Financial Services don't obviously share
# eps/fcf's sector-median-P/E logic). Revisit if a real non-curated
# dividends example surfaces to actually calibrate against.

# g2 (years 6-10 growth): confirmed live that for "eps"/"fcf"/"revenue"
# bases, the NORMAL scenario fades to exactly 10% regardless of g1 in all 3
# non-dividend examples (NVDA 30%->10%, MSFT 15%->10%, NFLX 12%->10%) - a
# genuine, confirmed rule. Best/worst g2 are NOT as clean (NVDA/NFLX's best
# cases fade to 15%/12%, not 10%) so those two stay at a coarser
# approximation. For "dividends", g2 is set equal to g1 in build_scenarios
# below (not fixed here) - PEP/XOM's normal and best cases both show
# g2 == g1, i.e. a mature dividend payer is already near its steady-state
# rate, nothing further to fade toward.
GROWTH_BASIS_G2 = {"normal": 0.10, "best": 0.12, "worst": 0.04}

# Floor on g2 for the "dividends" basis specifically (build_scenarios sets
# g2 = g1 there, uncapped, before this applies) - see G1_FLOOR's own
# comment for the QCOM case this fixes: a g1 that's negative because of
# ONE bad consensus year is a weak enough basis for a 5-year projection
# already; copying it into g2 asserts the SAME decline rate holds for a
# SECOND five years, which is a much stronger, much less defensible claim
# a temporary dip doesn't support. Set to -0.05, not less negative -
# that's PEP's own real CURATED_SCENARIOS worst-case g2 (see
# CURATED_SCENARIOS above), the most negative g2 any analyst-vetted
# mature-payer number on file actually reaches. A generic derived
# estimate floors at the worst case real vetted data shows is plausible,
# not at zero (a genuinely struggling payer's long-term outlook can
# legitimately be somewhat negative - PEP's own worst case IS - just not
# QCOM's unfloored -13%).
G2_DIVIDENDS_FLOOR = -0.05

# g1 FALLBACK OF LAST RESORT - used only when NEITHER a reliable consensus
# growth estimate NOR the sustainable-growth-rate calculation below
# (_sustainable_growth_rate) has usable inputs. "Average company" growth
# assumptions, not inventing new numbers.
G1_FALLBACK = {"normal": 0.08, "best": 0.10, "worst": 0.04}

# Spread applied around the sustainable-growth-rate "normal" estimate to
# get best/worst - the same shape G1_FALLBACK already used (normal 0.08 ->
# best 0.10 is +0.02, -> worst 0.04 is -0.04), just now anchored to a
# company-specific rate instead of a flat one.
SUSTAINABLE_GROWTH_BEST_SPREAD = 0.02
SUSTAINABLE_GROWTH_WORST_SPREAD = -0.04

# Caps the ROE input to the sustainable-growth-rate formula (roe x
# retention ratio) before that ratio is used as g1 - confirmed live:
# buyback-heavy, asset-light companies (the exact class the Graham Number
# was replaced for - see this module's history comment) show
# eps_trailing/book_value_per_share ratios well above 100%, since
# aggressive buybacks shrink book value toward zero while earnings keep
# growing (this was the actual mechanism behind META/AMZN/TSLA/ADBE/BABA
# coming out +130-177% too high vs a real analyst's own numbers - all
# five hit G1_CAP=0.40 via this path). An uncapped ROE compounds ^5 in
# scenario_terminal_value, gets clamped to G1_CAP anyway, but ALL THREE
# scenarios (normal/best/worst) land on the exact same capped value once
# ROE alone exceeds ~0.38 - collapsing the scenario spread to nothing,
# the same failure mode G1_CAP's own comment describes. Capping ROE here
# instead keeps best > normal > worst distinguishable for genuinely
# exceptional but not runaway businesses. Set to stay clear of G1_CAP
# (0.30, see that constant's own comment on why it was tightened): at
# 0.35 the capped ROE plus SUSTAINABLE_GROWTH_BEST_SPREAD (0.37) and even
# the unspread ROE itself would ALL exceed G1_CAP, so normal/best/worst
# all pinned back to the exact same 0.30 - recreating the scenario-
# collapse failure this cap exists to prevent, just at a lower ceiling.
# 0.25 keeps best (0.25+0.02=0.27) comfortably under G1_CAP, preserving a
# genuine spread between scenarios for high-ROE names.
SUSTAINABLE_GROWTH_ROE_CAP = 0.25

# Floor on the RAW (pre-cap) eps_trailing/book_value_per_share ratio -
# below this, the ratio is treated as unusable (falls through to
# G1_FALLBACK) rather than trusted at face value. Confirmed live: BRK-B
# showed book_value_per_share=$498,663 (a known yfinance quirk for dual-
# class shares - Berkshire's B-shares trade around $500 but yfinance's
# bookValue field here reflects something far closer to the unsplit
# A-share scale), producing a raw "ROE" of ~0.008% against eps_trailing
# $39.77 - not a real signal about Berkshire's profitability, a data
# artifact. A genuinely profitable company (eps_trailing already gated
# positive by the caller in build_scenarios) essentially never has a
# REAL sustainable growth rate this close to zero; SUSTAINABLE_GROWTH_
# ROE_CAP only guards the HIGH side; this guards the low side (a
# different problem, not just "the same cap in reverse" - a near-zero
# result here reads as "usable data saying no growth", when it's
# actually "unusable data", so it needs to be rejected upstream of the
# fallback waterfall, not floored to some small positive number).
SUSTAINABLE_GROWTH_ROE_FLOOR = 0.02


def _sustainable_growth_rate(fundamentals: dict) -> float | None:
    """Sustainable growth rate = ROE x retention ratio (1 - payout_ratio) -
    a company's OWN profitability and reinvestment behavior, not the
    market's opinion of it. Deliberately NOT P/E-implied growth: P/E
    already prices in the market's growth expectations, so deriving a DCF
    growth input from P/E and then valuing the company with it is circular
    - it will conclude "fairly valued" almost by construction, defeating
    the point of an independent valuation. This formula only uses
    fundamentals already fetched for other purposes (eps_trailing,
    book_value_per_share, payout_ratio), no new data dependency.

    ROE is approximated as eps_trailing / book_value_per_share (both
    already per-share, so shares outstanding cancels out - standard
    approximation, not the textbook net-income/total-equity ratio, but
    equivalent for a per-share model like this one). Returns None (not a
    fetch failure) when eps_trailing or book_value_per_share isn't usable -
    caller falls back to G1_FALLBACK, same fail-soft convention as the
    rest of this module. A missing payout_ratio is NOT treated as
    unusable - defaults to 0 (full reinvestment), which is the correct
    assumption for a real company that pays no dividend (payout_ratio is
    only populated for dividend payers - see fundamentals.py), not a
    "don't know" case that should abandon the whole calculation.
    """
    eps_trailing = fundamentals.get("eps_trailing")
    book_value_per_share = fundamentals.get("book_value_per_share")
    if not eps_trailing or eps_trailing <= 0 or not book_value_per_share or book_value_per_share <= 0:
        return None
    raw_roe = eps_trailing / book_value_per_share
    if raw_roe < SUSTAINABLE_GROWTH_ROE_FLOOR:
        # See SUSTAINABLE_GROWTH_ROE_FLOOR's own comment (BRK-B's
        # dual-class book-value artifact) - an implausibly-near-zero raw
        # ratio is unusable DATA, not a usable "low growth" signal.
        return None
    roe = min(raw_roe, SUSTAINABLE_GROWTH_ROE_CAP)
    payout_ratio = fundamentals.get("payout_ratio") or 0.0
    return roe * (1 - payout_ratio)

# Ceiling on the DERIVED g1 (real per-ticker consensus growth estimates,
# not CURATED_SCENARIOS - see build_scenarios) - confirmed live: an
# uncapped g1 compounds over STAGE_1_YEARS (^5) then multiplies by up to
# BEST_EXIT_MULTIPLE (25x), barely dented by discounting back over 10
# years, so a real but aggressive consensus growth estimate (a genuinely
# common shape for real high-growth/momentum stocks, not just a
# theoretical edge case) can blow the resulting intrinsic value out to
# multiples of the current price - confirmed against synthetic data
# reusing this exact formula: 90th percentile gap 91%, 99th percentile
# 354%, max 760%. Originally set to 0.40 (ABOVE NVDA's own
# CURATED_SCENARIOS "best" g1 of 0.30) on the reasoning that an
# individually-vetted number deserves more trust than an automated
# average - but confirmed live against the 19-ticker analyst comparison
# that letting an UNVERIFIED derived g1 get MORE aggressive than the
# single most aggressive number a human has actually vetted was itself
# the problem: META/AMZN's own real consensus growth (35.5%/73.2%
# this-year estimates) blended into a "best" case landing right at 0.40,
# producing intrinsic values 47-157% above the analyst's own numbers.
# 0.30 - matching, not exceeding, NVDA's ceiling - is the actual
# constraint: a generic formula/consensus-average estimate should never
# be trusted to run further than the most aggressive number a human has
# individually verified. CURATED_SCENARIOS tickers bypass this entirely
# (see build_scenarios' early return) - this cannot change any
# CURATED_SCENARIOS ticker's already-calibrated output.
G1_CAP = 0.30

# Correction to this constant's own earlier comment ("only caps the upper
# bound... not a symmetric problem needing a floor too"): confirmed live
# it IS a symmetric problem. QCOM's real consensus (growth_0y -12.5%,
# growth_1y -3.05%, same-direction so "reliable" by the check above)
# derived a g1 of -7.79% - not extreme on its own, but for the "dividends"
# basis g2 is set equal to g1 (see GROWTH_BASIS_G2's comment), and
# projecting that SAME decline rate for a second 5-year stage compounds
# to an intrinsic value of $22.90 against a $165.79 price - a raw 624%
# overvaluation gap, masked down to the misleadingly modest-looking
# "~150%" by VALUATION_PCT_DISPLAY_CAP (which only bounds the DISPLAYED
# number, not this one). A single bad consensus year is not evidence a
# company decays at that rate for a decade. Same "set above the single
# most aggressive analyst-vetted number" reasoning as G1_CAP: PEP's and
# XOM's CURATED_SCENARIOS worst-case g1 (both real, analyst-vetted,
# +0.03) are comfortably above this, so a generic derived g1 still gets a
# real floor without disagreeing with actually-vetted data. (g2's own
# floor, for the "dividends" basis specifically, is separate - see
# G2_DIVIDENDS_FLOOR below; a temporary near-term dip and a decade-long
# fade aren't the same claim, so they don't share one floor.)
G1_FLOOR = -0.10

# Rejects a consensus growth pair as "reliable" (see build_scenarios'
# consensus_reliable check) when EITHER year's magnitude is this extreme,
# even when both years point the same direction - confirmed live: GOOG's
# growth_0y=90.4%/growth_1y=+low-single-digits pair is same-direction so
# passed the existing gate, but 90.4% consensus EPS growth for a company
# GOOG's size is not a real sustained rate - it's the same rebound-off-a-
# depressed-base artifact the same-direction gate was built to catch,
# just one where the "giveback" year happened to still be positive rather
# than flipping negative. 0.60 is comfortably above NVDA's own
# CURATED_SCENARIOS "best" g1 (0.30, the single most aggressive
# analyst-vetted number this model has) while still catching 90%+ swings.
CONSENSUS_GROWTH_MAGNITUDE_CAP = 0.60


def classify_valuation_basis(
    eps_trailing: float | None,
    payout_ratio: float | None,
    sector: str | None,
    free_cash_flow: float | None,
) -> str:
    """Picks which metric to value, since a single metric can't meaningfully
    value both a bank and a pre-profit growth company. Evaluated in order:

    1. Real Estate sector -> "dividends" unconditionally, BEFORE the
       profitability check below - see REIT_SECTORS' comment for why
       REITs need a sector override rather than relying on payout_ratio or
       eps_trailing, both of which GAAP real-estate depreciation makes
       unreliable for this sector specifically (confirmed live: Aedifica,
       a real REIT, would otherwise have been misrouted).
    2. Unprofitable or unknown profitability -> "revenue" (can't project
       earnings/FCF/dividends that don't exist yet - matches early-stage
       growth companies like Beyond Meat).
    3. High but PLAUSIBLE payout ratio (pays out a large share of earnings,
       without exceeding DIVIDEND_PAYOUT_CEILING) -> "dividends" (mature
       cash-cow-style payers - matches Kinder Morgan/Coca-Cola
       Europacific-style examples). A payout ratio ABOVE the ceiling is
       deliberately excluded here, not treated as an even-more-obvious
       dividends case - see DIVIDEND_PAYOUT_CEILING's comment for why
       (confirmed live: DSM-Firmenich's transiently earnings-crushed
       1.79 payout ratio would otherwise have been misrouted to
       "dividends" instead of falling through to "fcf" below, which is
       what an asset-heavy chemicals company should actually use).
    4. Asset-heavy sector AND a real positive FCF figure -> "fcf" (matches
       industrial/asset-heavy examples). The FCF check isn't redundant with
       the sector check - confirmed live that yfinance's freeCashflow is
       None for banks (they don't have a meaningful FCF in the standard
       sense), and it can also be negative/None for an asset-heavy company
       mid capex-spike - both fall through to EPS rather than crashing or
       producing a nonsense basis.
    5. Otherwise -> "eps" (profitable, low payout, not asset-heavy - most
       tech/platform/growth names, and the fallback for financials, whose
       sector is never in ASSET_HEAVY_SECTORS)."""
    if sector in REIT_SECTORS:
        return "dividends"
    if eps_trailing is None or eps_trailing <= 0:
        return "revenue"
    if payout_ratio is not None and DIVIDEND_PAYOUT_THRESHOLD <= payout_ratio <= DIVIDEND_PAYOUT_CEILING:
        return "dividends"
    if sector in ASSET_HEAVY_SECTORS and free_cash_flow is not None and free_cash_flow > 0:
        return "fcf"
    return "eps"


def _shares_outstanding_approx(market_cap: float | None, price: float | None) -> float | None:
    # market_cap = price x shares_outstanding by yfinance's own construction,
    # so this is an exact derivation, not an estimate - avoids fetching a
    # separate sharesOutstanding field.
    if not market_cap or not price:
        return None
    return market_cap / price


# A trailing P/E below this fraction of the forward P/E flags trailing
# EPS as likely inflated by a ONE-OFF item (asset sale, tax benefit,
# legal settlement) that reverts by next year - forward P/E returning to
# normal is what distinguishes this from a persistent distortion (see
# PERSISTENTLY_LOW_PE_THRESHOLD below). 0.5 (trailing P/E under HALF the
# forward P/E) is deliberately conservative - a normal trailing/forward
# difference from routine year-over-year earnings growth is common and
# should NOT trigger this; only a gap this extreme is implausible as
# organic growth.
ONE_TIME_ITEM_PE_RATIO_THRESHOLD = 0.5

# An ABSOLUTE trailing P/E floor, checked only when pe_forward is ALSO
# below it (or missing) - confirmed live CHTR actually failed this
# differently than first assumed: pe_trailing~4.0 AND pe_forward~3.5, so
# ONE_TIME_ITEM_PE_RATIO_THRESHOLD's "trailing recovers by next year"
# signal never fires (forward is equally low, not higher). That pattern
# is a genuinely different problem - CHTR is a heavily-leveraged serial
# share-repurchaser, so EPS is inflated by a shrinking share count on
# BOTH a trailing and forward basis, not a one-off item that reverts.
# Rather than guess at a "corrected" EPS number, this basis is treated as
# unusable when it fires - the fallback chain in valuation_block_for then
# retries with "fcf" (CHTR's real free_cash_flow gives a P/FCF ~9.5x,
# much more plausible than compounding a $38.43 EPS). 6.0 is well below
# any SECTOR_MEDIAN_PE entry (lowest is Energy at 12.0), so this doesn't
# catch genuinely cheap value stocks - only P/E levels implausible as
# organic earnings quality regardless of sector.
PERSISTENTLY_LOW_PE_THRESHOLD = 6.0

# Fraction above consensus EPS estimate, for the most recently reported
# quarter, that flags a likely one-time/non-operating item - confirmed
# live: GOOG's trailing EPS ($19.93) was inflated by two consecutive
# quarters beating consensus by +94% and +213% (almost certainly mark-
# to-market gains on Alphabet's equity investment stakes, a known
# recurring GAAP-distortion pattern for it specifically), not organic
# operating growth. A real analyst's own DCF used a normalized EPS
# ($8.62, ~43% of the GAAP figure) that reproduced their target within
# 6.7% once combined with their real growth assumptions; our GAAP-
# trailing-EPS version overshot by +147%. This is the mirror image of
# ONE_TIME_ITEM_PE_RATIO_THRESHOLD/PERSISTENTLY_LOW_PE_THRESHOLD above
# (which catch a distorted EPS via an anomalously LOW P/E) - GOOG's P/E
# looked completely normal (17.2x) precisely BECAUSE the inflated EPS
# denominator masked it, so neither existing screen fired. Comparing
# actual-vs-consensus EPS is a more direct signal than a P/E ratio for
# this failure mode. 0.75 sits comfortably above GOOG's own historical
# "large but plausibly organic" beats (its own trailing 5 years show a
# max of ~68% outside the two anomalous quarters) while catching its
# 94%/213% pair - a starting threshold, not yet cross-sectionally
# calibrated the way the P/E-based screens were; revisit if a real
# non-GOOG example surfaces to calibrate against.
EARNINGS_SURPRISE_ONE_TIME_ITEM_THRESHOLD = 0.75


def cash_flow_basis_value(basis: str, fundamentals: dict) -> float | None:
    """Extracts the per-share cash-flow figure for the classified basis.
    "eps" and "dividends" are already per-share in yfinance's data; "fcf"
    and "revenue" are company totals divided down via the shares
    approximation above. None (not a fetch failure - a "this basis's input
    isn't usable right now" signal) propagates to intrinsic_value below,
    which renders it as "Not applicable", the same fail-soft convention
    the old Graham Number implementation used for negative EPS/book value.
    """
    # eps_trailing/dividend_rate/total_revenue/free_cash_flow are all
    # reported in financial_currency, but every basis below ends up
    # compared against `price` (see valuation_block_for's gap_pct), which
    # is in the TRADING currency - confirmed live these can differ for a
    # company cross-listed on an exchange denominated in a different
    # currency than it reports in (Mondi plc: GBP-quoted on the LSE, EUR
    # financials). No FX-rate source is wired into this module, so rather
    # than show a number silently off by an unknown FX rate, EVERY basis
    # here is unusable when the two currencies differ - same fail-soft
    # "Not applicable" convention as every other unusable-input case in
    # this module. This is distinct from (and applied AFTER) the
    # yfinance-specific pence/pound subunit fix in yahoo_provider.py's
    # `_normalize_pence_quote`, which relabels "GBp"/"GBX" to "GBP" before
    # fundamentals ever reach this module - so a GBP-financial-currency UK
    # stock quoted in pence correctly passes this check, while a genuine
    # cross-currency case like Mondi's correctly does not.
    currency = fundamentals.get("currency")
    financial_currency = fundamentals.get("financial_currency")
    if currency and financial_currency and currency != financial_currency:
        return None

    if basis == "eps":
        eps_trailing = fundamentals.get("eps_trailing")
        pe_trailing = fundamentals.get("pe_trailing")
        pe_forward = fundamentals.get("pe_forward")
        price = fundamentals.get("price")
        if (
            eps_trailing and pe_trailing and pe_forward and price
            and pe_trailing > 0 and pe_forward > 0
            and pe_trailing < pe_forward * ONE_TIME_ITEM_PE_RATIO_THRESHOLD
        ):
            # Forward EPS (price / pe_forward) is the more honest run-rate
            # cash flow when trailing EPS looks one-time-item-inflated AND
            # forward reverts to a normal level - see
            # ONE_TIME_ITEM_PE_RATIO_THRESHOLD's own comment.
            return price / pe_forward
        if (
            eps_trailing and pe_trailing and pe_trailing > 0 and pe_trailing < PERSISTENTLY_LOW_PE_THRESHOLD
            and (pe_forward is None or (pe_forward > 0 and pe_forward < PERSISTENTLY_LOW_PE_THRESHOLD))
        ):
            # See PERSISTENTLY_LOW_PE_THRESHOLD's own comment - a
            # persistent (not one-off) distortion, so unlike the branch
            # above there's no "corrected" number to substitute; the
            # caller's fallback chain retries with another basis instead.
            return None
        # NOT checking recent_eps_surprise here (see
        # EARNINGS_SURPRISE_ONE_TIME_ITEM_THRESHOLD's own comment) -
        # deliberately, unlike the two checks above. That check lives in
        # valuation_block_for instead, as a short-circuit BEFORE this
        # function is ever called: confirmed live doing it here caused a
        # curated ticker (NVDA, hand-verified against real analyst work)
        # sharing a large-earnings-surprise profile to still get its cf0
        # nulled out here, triggering the generic fallback chain and
        # discarding its curated data - this function has no way to know
        # "is_curated", which is exactly the context needed to skip the
        # check correctly.
        return eps_trailing
    if basis == "dividends":
        return fundamentals.get("dividend_rate")

    shares = _shares_outstanding_approx(fundamentals.get("market_cap"), fundamentals.get("price"))
    if not shares:
        return None
    if basis == "revenue":
        revenue = fundamentals.get("total_revenue")
        return revenue / shares if revenue else None
    if basis == "fcf":
        fcf = fundamentals.get("free_cash_flow")
        return fcf / shares if fcf else None
    return None


def build_scenarios(ticker: str | None, fundamentals: dict, basis: str) -> dict[str, dict]:
    """Assembles this company's normal/best/worst g1/g2/exit_multiple/
    probability. Curated tickers (CURATED_SCENARIOS) use the analyst's
    exact numbers verbatim. Everyone else is assembled from three
    independently-sourced pieces, each confirmed live rather than a single
    invented "generic" bundle:

    - g1 (years 1-5 growth): a three-tier waterfall, each tier only used
      when the one before it isn't usable. (1) A same-direction 0y/+1y
      consensus growth estimate (see fundamentals.fetch_fundamentals'
      growth_0y/growth_1y/growth_0y_low/growth_0y_high) - real analyst
      data, the most trustworthy source when it exists. "Same-direction"
      is the reliability gate: confirmed live that when 0y and +1y point
      in OPPOSITE directions (e.g. XOM's +65.7% this year / -8.6% next
      year), that's not a real growth trend - it's a rebound-then-giveback
      around a distorted (commodity-cycle, one-off) base year, and no
      combination of those two numbers recovers the analyst's actual 4%
      long-run assumption. (2) The sustainable growth rate - ROE x
      retention ratio, see _sustainable_growth_rate - when eps_trailing/
      book_value_per_share are usable. Company-specific and non-circular
      (unlike a P/E-implied growth rate - see that function's own comment
      for why that's the wrong metric), but a formula, not observed
      analyst data, so it only applies when (1) isn't available. (3)
      G1_FALLBACK - a flat "average company" assumption, only when neither
      real data nor fundamentals are usable. Falling back at any tier is a
      deliberate "don't know", not a confidently wrong derived number.
    - g2 (years 6-10 growth): GROWTH_BASIS_G2 for "eps"/"fcf"/"revenue"
      (confirmed pattern - see its comment), or set equal to this
      scenario's own g1 for "dividends" (a mature payer doesn't fade
      further - also confirmed, see GROWTH_BASIS_G2's comment).
    - exit_multiple: "revenue" uses the flat REVENUE_*_EXIT_MULTIPLE
      constants (a P/S-style multiple - see their comment). "dividends"
      shares eps/fcf's flat defaults (no confirmed evidence it needs a
      different multiple - see the reverted DIVIDEND_*_EXIT_MULTIPLE
      comment). "eps"/"fcf" anchor the normal exit multiple to
      min(NORMAL_EXIT_MULTIPLE, max(this sector's SECTOR_MEDIAN_PE, the
      company's OWN current trailing P/E)) - see that block's own comment
      for why min() rather than the sector median outright, and why the
      own-P/E floor exists; worst case uses
      WORST_EXIT_MULTIPLE_ASSET_HEAVY for cyclical/commodity sectors
      (ASSET_HEAVY_SECTORS), else WORST_EXIT_MULTIPLE_DEFAULT.
    """
    if ticker and ticker in CURATED_SCENARIOS and CURATED_SCENARIOS_BASIS.get(ticker) == basis:
        return {
            name: {**scenario, "probability": SCENARIO_PROBABILITY}
            for name, scenario in CURATED_SCENARIOS[ticker].items()
        }

    g1_values = dict(G1_FALLBACK)

    sustainable_g1 = _sustainable_growth_rate(fundamentals)
    if sustainable_g1 is not None:
        g1_values = {
            "normal": sustainable_g1,
            "best": sustainable_g1 + SUSTAINABLE_GROWTH_BEST_SPREAD,
            "worst": sustainable_g1 + SUSTAINABLE_GROWTH_WORST_SPREAD,
        }

    growth_0y = fundamentals.get("growth_0y")
    growth_1y = fundamentals.get("growth_1y")
    consensus_reliable = (
        growth_0y is not None and growth_1y is not None
        and (growth_0y >= 0) == (growth_1y >= 0)
        # See CONSENSUS_GROWTH_MAGNITUDE_CAP's own comment (GOOG's 90.4%
        # same-direction-but-implausible case) - same-direction alone
        # doesn't bound how extreme either year can be.
        and abs(growth_0y) <= CONSENSUS_GROWTH_MAGNITUDE_CAP
        and abs(growth_1y) <= CONSENSUS_GROWTH_MAGNITUDE_CAP
    )
    if consensus_reliable:
        # best/worst are OFFSETS from the same 2-year blend "normal" uses
        # (by the analyst range's own spread around growth_0y), not a
        # wholesale substitution of a different, narrower-window number -
        # confirmed live this matters: best/worst used to be growth_0y_
        # high/low directly, which only reflects THIS year's range and
        # ignores next year's growth_1y entirely. For a company where next
        # year is expected to look meaningfully different from this year
        # (QCOM: -12.5% this year, -3.1% next), that let "best" end up
        # WORSE than "normal" - normal's blend partly saw the better next
        # year, best's raw high-end didn't see it at all. Offsetting from
        # the same blended base guarantees best >= normal >= worst always,
        # for every basis this consensus path feeds (not just dividends).
        blended_normal = (growth_0y + growth_1y) / 2
        growth_0y_high = fundamentals.get("growth_0y_high")
        growth_0y_low = fundamentals.get("growth_0y_low")
        raw_high_offset = (growth_0y_high - growth_0y) if growth_0y_high is not None else None
        raw_low_offset = (growth_0y - growth_0y_low) if growth_0y_low is not None else None
        # A missing bound mirrors the OTHER bound's own offset (or, if
        # NEITHER exists, the same generic spread SUSTAINABLE_GROWTH_*_
        # SPREAD already uses elsewhere) rather than leaving best/worst at
        # whatever the prior tier (SGR/fallback) had set - confirmed live
        # that was a real bug: normal is unconditionally overwritten by
        # the consensus blend below, so a stale prior-tier best/worst
        # value is not guaranteed to land on the correct side of the NEW
        # normal, breaking the best >= normal >= worst guarantee this
        # block exists to provide.
        high_offset = raw_high_offset if raw_high_offset is not None else (
            raw_low_offset if raw_low_offset is not None else SUSTAINABLE_GROWTH_BEST_SPREAD
        )
        low_offset = raw_low_offset if raw_low_offset is not None else (
            raw_high_offset if raw_high_offset is not None else -SUSTAINABLE_GROWTH_WORST_SPREAD
        )
        g1_values["normal"] = blended_normal
        g1_values["best"] = blended_normal + high_offset
        g1_values["worst"] = blended_normal - low_offset

    # See G1_CAP's/G1_FLOOR's own comments - applied after all three g1
    # sources above (fallback/derived-normal/derived-best) so nothing
    # downstream of this point ever sees an un-bounded g1, regardless of
    # which source set it.
    g1_values = {name: max(min(value, G1_CAP), G1_FLOOR) for name, value in g1_values.items()}

    if basis == "dividends":
        # dividends' g2 = g1 (see GROWTH_BASIS_G2's comment for why that's
        # usually right for a mature payer) gets its OWN, separate floor
        # here - see G2_DIVIDENDS_FLOOR's own comment for why a temporary
        # dip (g1's claim) and a decade-long fade (g2's claim, if left as
        # a raw copy of a deeply negative g1) aren't the same claim.
        g2_values = {name: max(value, G2_DIVIDENDS_FLOOR) for name, value in g1_values.items()}
    else:
        # g2 = min(the flat GROWTH_BASIS_G2 default, this SAME scenario's
        # own g1) - not the flat default unconditionally. Originally only
        # applied to the worst tier (a structurally declining company -
        # a genuine "value trap" - was otherwise mathematically
        # unrepresentable, since a fixed +4% worst-case g2 put a floor
        # under how bad the worst case could ever look). Generalized to
        # normal/best after re-examining what GROWTH_BASIS_G2's own
        # "confirmed live" calibration actually shows: across all 6
        # CURATED_SCENARIOS tickers and all 3 tiers (18 g1/g2 pairs
        # total), g2 <= g1 in EVERY SINGLE case - AAPL/XOM/PEP even show
        # g2 == g1 at their normal/best tiers. The flat 0.10/0.12 defaults
        # only ever matched cases where g1 already happened to be
        # positive and above them (NVDA 30%->10%, MSFT 15%->10%, NFLX
        # 12%->10%, all real fades DOWN); applying those SAME flat values
        # when g1 is small or negative was an unevidenced extrapolation
        # in the untested direction - confirmed live it produced QCOM's
        # "normal" scenario projecting years 1-5 at -7.8% (QCOM's own
        # real, negative analyst consensus) then flipping to +10% GROWTH
        # for years 6-10 with no basis for assuming that reversal. min()
        # makes "hold at the already-identified rate" the default
        # assumption instead of "assume an unexplained rebound to a
        # fixed target" - consistent with every real analyst-vetted
        # example on file, not just the worst tier's.
        g2_values = {name: min(GROWTH_BASIS_G2[name], g1_values[name]) for name in GROWTH_BASIS_G2}

    if basis == "revenue":
        exit_multiples = {
            "normal": REVENUE_NORMAL_EXIT_MULTIPLE,
            "best": REVENUE_BEST_EXIT_MULTIPLE,
            "worst": REVENUE_WORST_EXIT_MULTIPLE,
        }
    elif basis == "dividends":
        # Shares the flat eps/fcf normal/best defaults rather than a
        # separate, sector-anchored, or yield-theoretic set - see the
        # reverted DIVIDEND_*_EXIT_MULTIPLE comment above for why: no
        # confirmed calibration evidence supports this basis needing a
        # DIFFERENT multiple than eps/fcf's flat default. Worst case still
        # varies by ASSET_HEAVY_SECTORS (XOM-vs-PEP's own confirmed-live
        # pattern - see WORST_EXIT_MULTIPLE_ASSET_HEAVY's comment) since
        # that distinction predates and is independent of the reverted
        # dividends-specific multiples above.
        worst_exit_multiple = (
            WORST_EXIT_MULTIPLE_ASSET_HEAVY if fundamentals.get("sector") in ASSET_HEAVY_SECTORS
            else WORST_EXIT_MULTIPLE_DEFAULT
        )
        exit_multiples = {"normal": NORMAL_EXIT_MULTIPLE, "best": BEST_EXIT_MULTIPLE, "worst": worst_exit_multiple}
    else:
        # Sector-anchored ceiling on the normal exit multiple: the flat
        # 20x applied identically regardless of sector directly
        # contradicted SECTOR_MEDIAN_PE shown in the SAME prompt for
        # lower-multiple sectors - confirmed live a no-consensus bank's
        # fallback g1 implies a ~17.7x fair P/E against a stated sector
        # median of 13.0, so banks/energy/utilities/materials read as
        # undervalued by construction. min() rather than the sector
        # median outright: the flat 20.0x/25.0x pair IS the calibrated
        # value for Technology (CURATED_SCENARIOS' AAPL/NVDA/MSFT/NFLX -
        # all Technology - use exactly 20.0x/25.0x normal/best), and
        # Technology's sector median (28.0) is ABOVE that - anchoring to
        # the median outright would raise Tech's exit multiple past its
        # own curated calibration, the opposite of what this fix is for.
        # min() only pulls DOWN sectors whose median is genuinely lower
        # than the calibrated default, leaving Technology/Healthcare/
        # Consumer Defensive (medians >= 20.0) exactly as calibrated. Best
        # keeps the same +5 absolute spread the curated examples show
        # (25-20=5), not a ratio - a ratio would shrink alongside a
        # lowered normal multiple in a way no curated example supports.
        #
        # Floored at the company's OWN current trailing P/E, not just the
        # sector median - confirmed live a real bug for any NON-curated
        # ticker in a low-median sector: XOM (real trailing P/E 20.7x)
        # would get capped at Energy's flat 12.0x median the moment it
        # left CURATED_SCENARIOS, i.e. assuming the market prices it MORE
        # cheaply in 10 years than it already does today. This path was
        # never actually exercised for XOM specifically since it's stayed
        # curated (bypasses this block entirely - see the early return
        # above), but any other real Energy/Financial-Services/Utilities/
        # Basic-Materials ticker a user queries through this live API and
        # that ISN'T in CURATED_SCENARIOS hits this exact case whenever
        # its own multiple sits above its sector's. max(sector_median,
        # own_pe) keeps the sector floor for names genuinely trading at or
        # below it, without dragging a premium-multiple name down to the
        # sector's generic level.
        sector = fundamentals.get("sector")
        sector_median = SECTOR_MEDIAN_PE.get(sector)
        own_pe = fundamentals.get("pe_trailing")
        effective_median_candidates = [v for v in (sector_median, own_pe) if v is not None]
        effective_median = max(effective_median_candidates) if effective_median_candidates else None
        normal_exit_multiple = min(NORMAL_EXIT_MULTIPLE, effective_median) if effective_median is not None else NORMAL_EXIT_MULTIPLE
        best_exit_multiple = normal_exit_multiple + (BEST_EXIT_MULTIPLE - NORMAL_EXIT_MULTIPLE)
        worst_exit_multiple = (
            WORST_EXIT_MULTIPLE_ASSET_HEAVY if sector in ASSET_HEAVY_SECTORS
            else WORST_EXIT_MULTIPLE_DEFAULT
        )
        exit_multiples = {"normal": normal_exit_multiple, "best": best_exit_multiple, "worst": worst_exit_multiple}

    return {
        name: {
            "g1": g1_values[name],
            "g2": g2_values[name],
            "exit_multiple": exit_multiples[name],
            "probability": SCENARIO_PROBABILITY,
        }
        for name in ("normal", "best", "worst")
    }


def scenario_dcf_value(cf0: float, g1: float, g2: float, exit_multiple: float, discount_rate: float) -> float:
    """Present value of ONE scenario, summing every projected year's cash
    flow PLUS the discounted terminal value: cf0 compounds at g1 for
    STAGE_1_YEARS, then at g2 for STAGE_2_YEARS, each year's cash flow
    discounted back at discount_rate; the terminal value (final year's cash
    flow x exit_multiple) is discounted back from the same final year.
    Used only for the "dividends" basis (see scenario_present_values) -
    dividends are real cash actually paid to the shareholder every year, so
    summing the interim stream is correct there, unlike EPS/FCF/revenue
    (see scenario_terminal_value). cf0 must be positive - callers
    (intrinsic_value) are responsible for that check, matching this
    module's existing convention of validating inputs at the boundary
    rather than inside the pure math."""
    pv = 0.0
    cf = cf0
    for year in range(1, STAGE_1_YEARS + 1):
        cf *= 1 + g1
        pv += cf / (1 + discount_rate) ** year
    for year in range(STAGE_1_YEARS + 1, STAGE_1_YEARS + STAGE_2_YEARS + 1):
        cf *= 1 + g2
        pv += cf / (1 + discount_rate) ** year
    terminal_value = cf * exit_multiple
    pv += terminal_value / (1 + discount_rate) ** (STAGE_1_YEARS + STAGE_2_YEARS)
    return pv


def scenario_terminal_value(cf0: float, g1: float, g2: float, exit_multiple: float, discount_rate: float) -> float:
    """Present value of ONE scenario counting ONLY the discounted terminal
    value - no interim-year summation. Used for "eps"/"fcf"/"revenue"
    bases: projected EPS/FCF/revenue isn't cash actually paid to the
    shareholder each year (unlike a dividend), so a shareholder's real
    return comes from eventually selling at the projected year-10 price,
    not from "receiving" ten years of paper earnings on top of that sale.
    Summing both (scenario_dcf_value's approach) double-counts, which was
    confirmed live to be the single largest source of error in the
    previous flat model - see module history, point 3b."""
    future_cf = cf0 * (1 + g1) ** STAGE_1_YEARS * (1 + g2) ** STAGE_2_YEARS
    terminal_value = future_cf * exit_multiple
    return terminal_value / (1 + discount_rate) ** (STAGE_1_YEARS + STAGE_2_YEARS)


def _dividend_stream_pv(dividend_rate: float, g2: float, discount_rate: float) -> float:
    """PV of a full STAGE_1_YEARS+STAGE_2_YEARS dividend stream growing at
    g2 (the smoother stage-2 rate, not the potentially noisy consensus-
    derived g1 - applying EPS-consensus growth to a dividend stream is the
    same anti-pattern this fix exists to avoid, see g2's own reasoning in
    GROWTH_BASIS_G2). Added to scenario_terminal_value's result for "eps"/
    "fcf" companies that pay SOME dividend (payout_ratio > 0 but below
    DIVIDEND_PAYOUT_THRESHOLD, so classified eps/fcf rather than
    dividends) - confirmed live these dividends are real cash the
    terminal-only formula was otherwise discarding entirely for a full
    decade (e.g. MMM, payout ~0.536, valued as if it paid nothing - see
    scenario_terminal_value's own comment for why terminal-only is
    otherwise correct for RETAINED, non-dividend earnings specifically)."""
    pv = 0.0
    dividend = dividend_rate
    for year in range(1, STAGE_1_YEARS + STAGE_2_YEARS + 1):
        dividend *= 1 + g2
        pv += dividend / (1 + discount_rate) ** year
    return pv


def _scenario_pv(
    basis: str, cf0: float, g1: float, g2: float, exit_multiple: float, discount_rate: float,
    dividend_rate: float | None = None,
) -> float:
    if basis == "dividends":
        return scenario_dcf_value(cf0, g1, g2, exit_multiple, discount_rate)
    pv = scenario_terminal_value(cf0, g1, g2, exit_multiple, discount_rate)
    if dividend_rate:
        pv += _dividend_stream_pv(dividend_rate, g2, discount_rate)
    return pv


def scenario_present_values(
    cf0: float, basis: str, scenarios: dict[str, dict], dividend_rate: float | None = None,
) -> dict[str, float]:
    """PV per named scenario in `scenarios` (see build_scenarios), using the
    basis-appropriate formula (see _scenario_pv). Factored out of
    intrinsic_value so the DCF formula is applied exactly once per scenario
    in exactly one place - both intrinsic_value's probability-weighting and
    valuation_block_for's logging build on this same dict rather than
    recomputing or duplicating it. `dividend_rate` is ignored for the
    "dividends" basis itself (already IS cf0 there) - see _dividend_
    stream_pv's own comment for why it's added for eps/fcf."""
    return {
        name: _scenario_pv(
            basis, cf0, scenario["g1"], scenario["g2"], scenario["exit_multiple"], DISCOUNT_RATE, dividend_rate,
        )
        for name, scenario in scenarios.items()
    }


def intrinsic_value(
    cf0: float | None, basis: str, scenarios: dict[str, dict], dividend_rate: float | None = None,
) -> float | None:
    """Probability-weighted intrinsic value across `scenarios` (see
    build_scenarios), using the basis-appropriate DCF formula (see
    _scenario_pv). None (not a fetch failure) when cf0 is missing or
    non-positive - the classified basis's own metric isn't usable for this
    company right now (e.g. a company just barely flipped profitable
    enough to avoid the "revenue" fallback but has near-zero EPS), same
    "Not applicable" semantics as the old Graham Number's negative-EPS
    case."""
    if cf0 is None or cf0 <= 0:
        return None

    pvs = scenario_present_values(cf0, basis, scenarios, dividend_rate)
    return sum(scenario["probability"] * pvs[name] for name, scenario in scenarios.items())


def valuation_block(price: float | None, intrinsic: float | None, basis: str) -> str:
    """Renders the 'Valuation' prompt block from an already-computed
    intrinsic value (see intrinsic_value). intrinsic=None means the
    classified basis's inputs aren't usable for this company right now - a
    real, expected outcome, not a fetch failure, so it renders as 'Not
    applicable', not 'Data unavailable.'. price=None (fundamentals fetch
    failed entirely) is the actual unavailable case. The basis label (e.g.
    "FCF-based") is always shown so a reader - human or model - can learn
    that the metric being valued differs across companies, not just the
    number.
    """
    label = BASIS_LABELS[basis]
    if intrinsic is None:
        return f"Not applicable (insufficient data for the {label.lower()} valuation basis)."
    if price is None:
        return "Data unavailable."

    pct = (price - intrinsic) / intrinsic * 100
    raw_abs_pct = abs(pct)
    # Below 1% either "overvalued" or "undervalued" reads as a directional
    # claim the number doesn't actually support - confirmed live price==
    # intrinsic rendered "overvalued by ~0%", a bearish word on a neutral
    # fact.
    if raw_abs_pct < 1.0:
        return f"Intrinsic Value ({label}): ${intrinsic:.2f}\nvs Current Price: trading near fair value"

    verdict = "overvalued" if pct >= 0 else "undervalued"
    # Backstop, not the primary fix (see G1_CAP) - caps what gets SHOWN,
    # not the underlying math, so it still catches any other path to an
    # extreme gap (e.g. an unusual exit-multiple/cf0 combination) that
    # capping g1 alone doesn't reach. Shows ">" rather than "~" once
    # actually capped - confirmed live "overvalued by ~150%" with no
    # marker read as a specific, calm estimate while the block's OWN price
    # and intrinsic value are one division apart from the true, much
    # larger gap (e.g. 624% for the QCOM case this cap was built around) -
    # silently understating a genuinely extreme signal is worse than a
    # capped number would be if honestly flagged as a floor, not a value.
    # Note this can only ever fire on the OVERVALUED side: undervalued is
    # (price-intrinsic)/intrinsic with price>=0, which is bounded in
    # (-100%, 0] by construction - a stock cannot be ">100% undervalued"
    # under a percent-of-intrinsic-value definition, so the cap is a
    # structural no-op there, not a second bug.
    if raw_abs_pct > VALUATION_PCT_DISPLAY_CAP:
        return f"Intrinsic Value ({label}): ${intrinsic:.2f}\nvs Current Price: {verdict} by >{VALUATION_PCT_DISPLAY_CAP:.0f}%"
    return (
        f"Intrinsic Value ({label}): ${intrinsic:.2f}\n"
        f"vs Current Price: {verdict} by ~{raw_abs_pct:.0f}%"
    )


def _log_valuation_computation(ticker, fundamentals, basis, cf0, scenarios, intrinsic, block):
    # INFO (not DEBUG) so it shows up by default under this app's existing
    # logging.basicConfig(level=logging.INFO) (see main.py), matching
    # inference.py's prompt-logging convention - no config change needed to
    # see this in Render's log stream. Logs the full "recipe" (which
    # classification inputs drove the basis choice, the actual cf0 used,
    # whether this ticker's scenarios are curated or derived/generic, every
    # scenario's growth/exit-multiple/PV, and the final weighted result) so
    # the formula's behavior can be audited per-ticker after the fact, not
    # just the one-line rendered block. Fires even when cf0/intrinsic end
    # up None (the "Not applicable" case) - that's exactly when knowing WHY
    # (which classification inputs were missing/unusable) is most useful,
    # not less.
    pvs = scenario_present_values(cf0, basis, scenarios) if cf0 is not None and cf0 > 0 else {}
    source = "curated" if ticker and ticker in CURATED_SCENARIOS else "derived/generic"
    scenario_summary = "; ".join(
        f"{name}(p={s['probability']:.0%}, g1={s['g1']:.1%}, g2={s['g2']:.1%}, exit={s['exit_multiple']:.1f}x)"
        + (f" -> PV=${pvs[name]:,.2f}" if name in pvs else "")
        for name, s in scenarios.items()
    )
    logger.info(
        "Valuation[%s]: classification inputs eps_trailing=%s, payout_ratio=%s, "
        "sector=%r, free_cash_flow=%s -> basis=%s; cf0=%s; discount_rate=%.0f%%; "
        "scenarios(%s): %s; intrinsic_value=%s; price=%s; block=%r",
        ticker,
        fundamentals.get("eps_trailing"), fundamentals.get("payout_ratio"),
        fundamentals.get("sector"), fundamentals.get("free_cash_flow"), basis,
        cf0, DISCOUNT_RATE * 100, source, scenario_summary, intrinsic, fundamentals.get("price"),
        block,
    )


def _classify_alternate_basis(sector: str | None, free_cash_flow: float | None) -> str:
    """The basis a ticker would get if its payout_ratio fell just OUTSIDE
    the dividends band - i.e. classify_valuation_basis's own steps 4-5,
    replayed in isolation. This is the "other side" of the payout-
    threshold cliff DIVIDEND_PAYOUT_BLEND_HALF_WIDTH blends across (see
    that constant's comment) and the fallback tried by valuation_block_for
    when a REIT override has no usable dividend data."""
    if sector in ASSET_HEAVY_SECTORS and free_cash_flow is not None and free_cash_flow > 0:
        return "fcf"
    return "eps"


def _payout_blend_fraction(payout_ratio: float | None) -> float | None:
    """0..1 fraction of how far `payout_ratio` sits inside the blend band
    around DIVIDEND_PAYOUT_THRESHOLD (0 = fully the alternate basis, 1 =
    fully dividends), or None outside the band entirely - see
    DIVIDEND_PAYOUT_BLEND_HALF_WIDTH's own comment for why this band
    exists at all."""
    if payout_ratio is None:
        return None
    low = DIVIDEND_PAYOUT_THRESHOLD - DIVIDEND_PAYOUT_BLEND_HALF_WIDTH
    high = DIVIDEND_PAYOUT_THRESHOLD + DIVIDEND_PAYOUT_BLEND_HALF_WIDTH
    if not (low <= payout_ratio <= high):
        return None
    return (payout_ratio - low) / (high - low)


def valuation_block_for(fundamentals: dict | None, ticker: str | None = None) -> str:
    """Convenience wrapper for callers that only need the rendered text -
    see valuation_assessment_for (this function's superset) for the full
    docstring and the numeric gap this discards."""
    return valuation_assessment_for(fundamentals, ticker)[0]


def valuation_assessment_for(fundamentals: dict | None, ticker: str | None = None) -> tuple[str, float | None]:
    """Convenience wrapper for callers holding a fundamentals.fetch_
    fundamentals() result - classifies the valuation basis, builds this
    company's scenarios (see build_scenarios), computes the intrinsic
    value, and renders the block in one call. A missing/failed fundamentals
    fetch (or a missing price specifically) renders as 'Data unavailable.'
    before classification is even attempted, since none of its inputs
    would be trustworthy either. `ticker` is optional - without it,
    build_scenarios can never match CURATED_SCENARIOS and always falls
    back to derived/generic, and the audit log below just omits the label;
    callers without it (e.g. existing tests) still work.

    Returns (block_text, gap_pct). gap_pct is the RAW, uncapped/unrounded
    (price - intrinsic) / intrinsic * 100 (positive = overvalued) behind
    the rendered block, or None whenever there's no usable price/intrinsic
    pair (the "Data unavailable."/"Not applicable" early exits, or a
    fallback chain that never found a usable basis). This is what
    fusion.py's valuation_bucket() consumes directly - the 150%
    VALUATION_PCT_DISPLAY_CAP applied when RENDERING block_text is a
    display concern only, never part of the fusion decision. valuation_
    block_for (above) is the pre-existing single-value convenience
    wrapper, kept so every prior caller/test needs no change.
    """
    if not fundamentals or fundamentals.get("price") is None:
        return "Data unavailable.", None

    basis = classify_valuation_basis(
        fundamentals.get("eps_trailing"),
        fundamentals.get("payout_ratio"),
        fundamentals.get("sector"),
        fundamentals.get("free_cash_flow"),
    )
    # Curated tickers override the generic classifier's output rather than
    # merely being checked against it. Confirmed live: XOM's own real
    # consensus growth estimates are opposite-direction year-over-year (see
    # build_scenarios' docstring) - exactly why it was hand-curated in the
    # first place, since the generic pipeline can't be trusted for it. Tying
    # curated-data usage to "does today's classify_valuation_basis output
    # happen to agree" made curated tickers hostage to unrelated constants:
    # raising DIVIDEND_PAYOUT_THRESHOLD to fix QCOM's misrouting silently
    # knocked XOM from "dividends" to "fcf" (Energy is in
    # ASSET_HEAVY_SECTORS and XOM has positive free_cash_flow) - still not
    # "eps", still using_curated=False, for a ticker whose g1/g2/exit
    # numbers were calibrated specifically for the "eps" cf0. A human
    # already verified this ticker's basis against real analyst work; that
    # judgment should win outright, not just when it coincidentally matches
    # a sector/payout-ratio heuristic that has nothing to do with it.
    is_curated = bool(ticker and ticker in CURATED_SCENARIOS_BASIS)
    if is_curated:
        basis = CURATED_SCENARIOS_BASIS[ticker]

    # Skips the whole compute/blend/fallback pipeline below entirely when
    # the eps basis's trailing EPS looks one-time-item-distorted (see
    # EARNINGS_SURPRISE_ONE_TIME_ITEM_THRESHOLD) - deliberately renders
    # "Not applicable" rather than falling back to another basis, unlike
    # every OTHER "no usable cf0" case below. Confirmed live the
    # fallback chain's implicit assumption ("some other basis is
    # probably clean") doesn't hold here: AMZN shows the same class of
    # distorted trailing EPS as GOOG (a 214% earnings surprise), but its
    # fcf fallback was ALSO distorted this same quarter, for an unrelated
    # reason (a heavy AI-infrastructure capex cycle crushing free cash
    # flow) - the fallback didn't produce a correct number, just a
    # DIFFERENTLY wrong one (flipped from wildly overvalued-looking to
    # wildly undervalued-looking). Not applied to curated tickers - a
    # human already verified those against real analyst work, which
    # would have caught a similarly distorted trailing figure.
    if not is_curated and basis == "eps":
        recent_eps_surprise = fundamentals.get("recent_eps_surprise")
        if recent_eps_surprise is not None and recent_eps_surprise > EARNINGS_SURPRISE_ONE_TIME_ITEM_THRESHOLD:
            block = valuation_block(fundamentals["price"], None, "eps")
            _log_valuation_computation(ticker, fundamentals, "eps", None, {}, None, block)
            return block, None

    def compute(b: str, include_dividend_pv: bool):
        cf0_ = cash_flow_basis_value(b, fundamentals)
        scenarios_ = build_scenarios(ticker, fundamentals, b)
        # Two separate reasons to skip the dividend-stream add-on
        # (_dividend_stream_pv): (1) curated tickers - their g1/g2/
        # exit_multiple were fitted end-to-end against the analyst's own
        # target via the terminal-only formula ALONE, so adding a second,
        # uncalibrated dividend term would double-count relative to that
        # calibration; (2) the payout-threshold BLEND below - confirmed
        # live blending a dividend-PV-augmented eps/fcf value against a
        # full "dividends"-basis value (which already captures the
        # complete dividend stream itself, at its own higher multiple)
        # double-counts the dividend credit twice over (ACN: eps-with-
        # PV-addon $273.52 blended with dividends-basis $199.52 pushed
        # intrinsic to $267, +86% vs the analyst - blending the PURE
        # terminal-only eps value against dividends instead fixes this;
        # the blend fraction itself already IS the "how much dividend
        # credit" mechanism near the threshold, so the standalone add-on
        # is redundant there specifically, not wrong in general - it
        # still applies for eps/fcf tickers OUTSIDE the blend band that
        # pay a real but non-threshold-adjacent dividend).
        dividend_rate_ = fundamentals.get("dividend_rate") if include_dividend_pv else None
        intrinsic_ = intrinsic_value(cf0_, b, scenarios_, dividend_rate_)
        return cf0_, scenarios_, intrinsic_

    # Payout-threshold cliff smoothing (see DIVIDEND_PAYOUT_BLEND_HALF_
    # WIDTH) - only for non-curated, non-REIT, non-revenue tickers (a
    # revenue-basis company is unprofitable by definition; there's no
    # dividends-band payout_ratio for it to straddle).
    blend_t = None
    if not is_curated and basis not in ("revenue",) and fundamentals.get("sector") not in REIT_SECTORS:
        blend_t = _payout_blend_fraction(fundamentals.get("payout_ratio"))

    if blend_t is not None:
        alt_basis = _classify_alternate_basis(fundamentals.get("sector"), fundamentals.get("free_cash_flow"))
        div_cf0, div_scenarios, div_iv = compute("dividends", False)
        alt_cf0, alt_scenarios, alt_iv = compute(alt_basis, False)
        if div_iv is not None and alt_iv is not None:
            intrinsic = blend_t * div_iv + (1 - blend_t) * alt_iv
            basis, cf0, scenarios = (
                ("dividends", div_cf0, div_scenarios) if blend_t >= 0.5 else (alt_basis, alt_cf0, alt_scenarios)
            )
        elif div_iv is not None:
            basis, cf0, scenarios, intrinsic = "dividends", div_cf0, div_scenarios, div_iv
        else:
            basis, cf0, scenarios, intrinsic = alt_basis, alt_cf0, alt_scenarios, alt_iv
    else:
        cf0, scenarios, intrinsic = compute(basis, not is_curated)

    if intrinsic is None:
        # The chosen basis (classify_valuation_basis's own pick, a
        # curated override, or the REIT sector override) has no usable
        # cf0 for this company right now - confirmed live this is a real
        # failure mode both for curated tickers (yfinance drops a field
        # for one call) and REITs (the REIT sector override forces
        # "dividends" unconditionally with no data check of its own - see
        # REIT_SECTORS' comment). Rather than a permanent "Not
        # applicable" for a company that DOES have other usable data, try
        # every other basis in a fixed order until one works - same
        # fail-soft spirit as every other branch in this module, just
        # applied one level up (basis selection) instead of within a
        # single basis's math.
        is_curated = False
        for fallback_basis in FALLBACK_BASIS_ORDER:
            if fallback_basis == basis:
                continue
            fb_cf0, fb_scenarios, fb_intrinsic = compute(fallback_basis, True)
            if fb_intrinsic is not None:
                basis, cf0, scenarios, intrinsic = fallback_basis, fb_cf0, fb_scenarios, fb_intrinsic
                break

    block = valuation_block(fundamentals["price"], intrinsic, basis)
    _log_valuation_computation(ticker, fundamentals, basis, cf0, scenarios, intrinsic, block)
    gap_pct = None
    if intrinsic is not None and intrinsic != 0:
        gap_pct = (fundamentals["price"] - intrinsic) / intrinsic * 100
    return block, gap_pct
