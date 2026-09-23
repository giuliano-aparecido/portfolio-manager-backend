"""Recent news headlines for one security, from Google News RSS.

Ported (not imported) from financial-sentiment-api's
`app/services/news.py` — the same convention this repo already follows for
the DCF math (`fundamentals/intrinsic_value.py`) and the Yahoo provider:
the two repos stay independent, so the quality/relevance filters below are
a deliberate copy. Keep the denylists roughly in sync with that file by
hand; there is no shared package and adding one isn't worth coupling two
deployables.

Four deliberate differences from the source, all driven by the different
consumer — an interactive portfolio agent rather than a fine-tuned model's
RAG block:

1. Returns up to `limit` headlines, not exactly one. The source selects a
   single headline only to match its training rows' shape.
2. No degrade-to-junk fallback. The source falls back
   relevant -> quality-only -> raw so it always returns *something*; here a
   headline that fails the filters is never returned at all.
3. Instead, the search WINDOW widens (`when:7d` -> 30d -> 90d -> 365d,
   see SEARCH_WINDOWS_DAYS) until a window yields at least one headline
   that passes both filters. The window actually used comes back in the
   result so the caller can say "nothing this week, this is from last
   month" rather than implying the news is fresh.
4. The query searches the COMPANY NAME when one is known, not the bare
   ticker. Measured live on 2026-09-18: "NESN stock earnings financial
   news when:7d" returned 3 entries where "Nestle S.A. stock earnings
   financial news when:7d" returned 11 — the gap matters most for exactly
   the non-US listings this portfolio is full of.
"""

import datetime
import logging
import re
import threading
import time
import unicodedata
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass, replace

import feedparser
import httpx

from app.schemas.agent import NewsStatus
from app.services.ticker_config import MARKET_YAHOO_SUFFIX

logger = logging.getLogger(__name__)

PUBLISHER_FALLBACK = "Google News"

# Widened in order until one of them yields >= 1 headline surviving both
# filters. 365 is the last resort: past a year a headline is history, not
# news, and the caller says so rather than widening further.
SEARCH_WINDOWS_DAYS = (7, 30, 90, 365)

# How many raw RSS entries per window to consider as candidates before
# filtering — NOT the number returned. Ported from the source, where a
# live survey of ORCL/TSLA/QCOM (2026-08-19) found a SINGLE publisher
# (MarketBeat) accounted for 36-40% of all raw results per ticker, almost
# entirely auto-generated 13F-filing spam ("46,643 Shares in Oracle
# Corporation $ORCL Purchased by Trust Co. of Vermont"), not news. The
# pool has to be much larger than `limit` for that reason.
CANDIDATE_POOL_SIZE = 30

_REQUEST_TIMEOUT_SECONDS = 10.0

# Google News results for one security barely move within a chat session,
# and the escalation loop can fire up to len(SEARCH_WINDOWS_DAYS) requests
# per miss — so a short in-process TTL keeps a multi-turn conversation
# about one holding from re-fetching on every turn. Mirrors the layer in
# fundamentals/cache.py (same TTL shape, same test-only clear hook); no DB
# table, because unlike fundamentals this is worth re-fetching intraday.
_MEM_TTL_SECONDS = 900.0
_MEM_MAX_ENTRIES = 256
_mem_lock = threading.Lock()
# (search ticker, search subject, sector) — deliberately NOT `limit`.
# `limit` is an LLM-chosen tool argument, and keying on it would make a
# follow-up turn asking for 3 headlines instead of 5 miss a cache entry
# that already holds them as a superset. The full filtered window is
# cached; `limit` is applied on read.
_CacheKey = tuple[str, str, str]
_mem: dict[_CacheKey, tuple[float, "NewsResult"]] = {}

# Only a real exchange suffix is stripped before a ticker is used for
# search or headline matching. The source repo uses a general
# `\.[A-Za-z]{1,3}$` regex, which CANNOT be reused here: this app's
# canonical tickers include share classes like "BRK.B", and that regex
# would turn it into "BRK". Matching the known suffix list instead leaves
# "BRK.B" alone while still handling a ticker a user typed into the UI
# with a suffix already attached ("ARYN.SW"), which the create-ticker
# route doesn't reject.
#
# Why strip at all: the source found live that Google News treats
# "ARYN.SW" as a literal token no article's text contains — that exact
# query returned 0 entries where the bare "ARYN" returned 23 — and the
# relevance filter had the same problem in reverse, requiring a literal
# ".SW" in headline text that real coverage writes as plain "ARYN".
_EXCHANGE_SUFFIXES = tuple(sorted({s for s in MARKET_YAHOO_SUFFIX.values() if s}))


def _strip_exchange_suffix(ticker: str) -> str:
    for suffix in _EXCHANGE_SUFFIXES:
        if ticker.upper().endswith(suffix):
            return ticker[: -len(suffix)]
    return ticker


# --- Quality filters (ported) ----------------------------------------------

# Homogeneous, near-100% low-content sources, excluded from candidacy
# entirely. Distinct from heterogeneous opinion/commentary outlets (Motley
# Fool, Benzinga, 24/7 Wall St.) that mix real reporting with opinion and
# are deliberately left in — the headline-shape filter below already
# catches their worst individual offenders.
LOW_QUALITY_PUBLISHERS = {
    "MarketBeat", "Stocktwits", "GuruFocus", "Trefis", "Simply Wall St.",
    "simplywall.st", "Zacks Investment Research", "StockStory",
    "TIKR.com", "Moomoo", "TradingKey", "Quiver Quantitative",
}


def _publisher_key(publisher: str) -> str:
    """Lower-cased, alphanumerics only. Google News labels the same outlet
    inconsistently — "MarketBeat" one day, "marketbeat.com" the next (the
    source's list carries both "Simply Wall St." and "simplywall.st" for
    the same reason). Folding to a key and prefix-matching below means one
    entry covers every spelling.

    Observed live 2026-09-18: with exact-string matching, all four IBM
    results in the 7-day window were "marketbeat.com" 13F spam that the
    denylist was already meant to exclude.
    """
    return "".join(c for c in publisher.lower() if c.isalnum())


_LOW_QUALITY_PUBLISHER_KEYS = frozenset(_publisher_key(p) for p in LOW_QUALITY_PUBLISHERS)


def _is_low_quality_publisher(publisher: str) -> bool:
    key = _publisher_key(publisher)
    # Prefix, so "marketbeatcom" matches "marketbeat". These are specific
    # brand names, so a legitimate outlet shadowing one is not a real risk.
    return any(key.startswith(denied) for denied in _LOW_QUALITY_PUBLISHER_KEYS)

# The source repo has ONE flat denylist. It's split in two here, because
# a flat list applied to this tool is actively dangerous rather than
# merely lossy: this tool escalates its search window when nothing
# survives filtering, and then tells the model a wide window means the
# company "has genuinely been quiet". A filter that over-rejects doesn't
# lose a headline, it reports a week of real news as a quiet week.
#
# Measured, 2026-09-18: a first attempt at tightening these patterns
# rejected 9 of 10 realistic event headlines for this portfolio's own
# holdings — "Nestle shares surge after activist investor Third Point
# builds stake", "IBM shares gain as quantum unit wins $2bn government
# contract", "Mondi reports 15% drop in packaging demand". Every one was
# caught by a move-verb pattern, because a move verb is exactly how real
# reporting LEADS an event headline.
#
# So: _HARD_REJECT_RE is structural junk that no context can redeem (13F
# filing spam, listicles, quote pages, opinion bait). _MOVE_REPORT_RE is
# the "reports THAT a stock moved" shape — and it only counts as
# low-content when the headline doesn't also say WHY, which is what
# _EVENT_SIGNAL_RE tests for. That's the source's own stated intent
# ("reports THAT a stock moved without saying why"); expressing it as two
# patterns plus an override is what makes it hold under tightening.
#
# Still a regex denylist, not a classifier — it will keep missing new
# phrasings, the accepted tradeoff noted in the source.
_MOVE_VERB_RE_FRAGMENT = (
    r"(?:ris(?:e|es|ing)|fell|fall(?:s|ing)?|dropp?(?:ed|s|ing)?|"
    r"rall(?:y|ies|ying|ied)|slid(?:e|es|ing)?|climb(?:s|ed|ing)?|"
    r"surg(?:e|es|ed|ing)?|plung(?:e|es|ed|ing)?|jump(?:s|ed|ing)?|"
    r"sank|sink(?:s|ing)?|tumbl(?:e|es|ed|ing)|gain(?:s|ed|ing)?|"
    r"los(?:es|ing)|lost|nosediv(?:e|es|ed|ing)|soar(?:s|ed|ing)?|"
    r"sag(?:s|ged|ging)?|slump(?:s|ed|ing)?|wilt(?:s|ed|ing)?|"
    r"slip(?:s|ped|ping)?|retreat(?:s|ed|ing)?|advanc(?:e|es|ed|ing)?|"
    r"wobbl(?:e|es|ed|ing)|sink|dip(?:s|ped|ping)?|swoon(?:s|ed|ing)?|"
    r"spik(?:e|es|ed|ing)|skid(?:s|ded|ding)?|edg(?:e|es|ed|ing)|"
    r"tick(?:s|ed|ing)?|declin(?:e|es|ed|ing))"
)

# Tier 1 — structural junk. No amount of event context redeems a 13F
# filing summary or a stock-quote landing page, so these reject outright.
_HARD_REJECT_RE = re.compile(
    r"^Is .+ a Good Stock"
    r"|^Is .{1,60}\bStill\b.{1,25}\bAfter\b.{0,40}(?:\d+%|rally|surge|drop|plunge|rout|gain|dip|slump|rebound)"
    r"|Stock a (?:Good )?Buy\b|^Should You Buy|Buy,? Hold,? (?:or|and) Sell"
    r"|^\d+ (?:Reasons?|Stocks?)|Better Buy|Zacks (?:Investment|Rank)|Trending Stock"
    r"|shares (?:added to|removed from|acquired by|sold by|purchased by)"
    r"|^[\d,]+\+? Shares (?:in|of)|(?:Buys|Purchases?|Sells) Shares (?:in|of)"
    r"|(?:Takes|Makes New) .{0,25}(?:Position|Investment) in|Invests? \$[\d,.]+|13F"
    r"|portfolio.{0,20}(?:quiverquant|according to a)"
    # More 13F shapes, observed live 2026-09-18: "Sequoia Financial
    # Advisors LLC Acquires 14,178 Shares of International Business
    # Machines", "Baird Financial Group Inc. Reduces Position in ...",
    # "Van Hulzen Asset Management LLC Has $31.69 Million Stock Holdings
    # in ...". The ported branches miss these because a share count sits
    # between the verb and "Shares", or the verb isn't in their list.
    #
    # A share count or dollar amount is itself filer-shaped, so those two
    # branches stand alone. The bare "Reduces Position in" shape is NOT —
    # gating it on an institutional subject is load-bearing, not
    # decoration. Without that gate it also rejects "Berkshire Hathaway
    # Boosts Stake in Occidental Petroleum" and "Nestle Sells Stake in Its
    # Water Business", which are the largest corporate events there are,
    # and being in tier 1 they can't be rescued by _EVENT_SIGNAL_RE.
    r"|\b(?:Acquires|Buys|Purchases|Sells|Snaps Up)\s+(?:its\s+)?[\d,]+\+?\s+Shares\b"
    r"|\bHas \$[\d,.]+ (?:Million|Billion) (?:Stock )?(?:Holdings|Position|Stake)\b"
    r"|\bShares? (?:of|in) .{0,60}\b(?:Purchased|Acquired|Sold|Bought) by\b"
    r"|\b(?:LLC|L\.L\.C\.|LLP|LP|Advisors?|Advisers?|Asset Management|Management|Capital|Partners|"
    # `.` not `[^.]` for the gap: a filer's name routinely contains a
    # period ("Baird Financial Group Inc. Reduces Position in ...").
    r"Wealth|Trust|Financial|Investments?|Securities|Bancorp|Bank|Fund|Holdings Inc)\b.{0,40}?"
    r"\b(?:Acquires|Buys|Purchases|Sells|Reduces|Boosts|Lowers|Raises|Trims|Grows|Cuts|Increases|Decreases)\s+"
    r"(?:its\s+)?(?:Position|Stake|Holdings|Holding|Shares)\b"
    # `here.s` (the ported spelling) covers "here's" and "heres" but NOT
    # "Here Is Why", which is how several outlets actually write it. That
    # gap used to be masked — those headlines were caught by the move
    # pattern instead — but tier 2 is now overridable, so a "Here Is Why"
    # piece naming any event word would get through, and this is the exact
    # category the tool promises the model it removes.
    r"|here(?:.|\s+i)s why|here(?:.|\s+i)s what (?:investors|we|you) (?:need to know|see|should know)"
    r"|what you need to know|laps the stock market|what.s going on with"
    # Observed live 2026-09-18 on IBM / AMZN / BRK.B: advice bait, a
    # TV-personality opinion, and a quote landing page syndicated into the
    # feed — none of them articles about the company.
    r"|here is how to play|how to play .{0,30}\bstock\b"
    r"|attracting investor attention|\bCramer\b"
    r"|\bstock quote\b|\bquote price\b",
    re.IGNORECASE,
)

# Tier 2 — the "a stock moved" shape. Only low-content when the headline
# doesn't also say why (see _EVENT_SIGNAL_RE).
_MOVE_REPORT_RE = re.compile(
    r"stock (?:is )?trad(?:ing|es) (?:up|down|higher|lower)"
    r"|(?:shares|stock) (?:are|is) (?:up|down|higher|lower) today"
    rf"|\bwhy\b.{{0,60}}\b(?:stock|shares?)\b.{{0,30}}\b{_MOVE_VERB_RE_FRAGMENT}\b"
    rf"|\b(?:stock|shares?)\b.{{0,20}}\b(?:is|are)\b.{{0,10}}\b{_MOVE_VERB_RE_FRAGMENT}(?:ing)?\b"
    # "Nestle stock gains modestly as investors focus on 2026 margins",
    # "Nestle stock edges lower after recent gains" — the ported
    # subject-verb branch above requires an "is"/"are" copula these lack.
    rf"|\b(?:stock|shares)\s+{_MOVE_VERB_RE_FRAGMENT}\b"
    # A move phrased as stasis, which no move verb catches. Deliberately
    # NOT "holds near/above/below": those match "holds near-record cash"
    # and "holds above 5% stake in Kyndryl", which are events.
    r"|\bhold(?:s|ing)? (?:steady|flat)\b"
    r"|\btrad(?:e|es|ing) (?:near|around|above|below)\b"
    r"|\blittle changed\b|\blargely unchanged\b"
    r"|\bmoved? today\b"
    # A move phrased as a noun — anchored to an explicit price subject so
    # that "posts 8% gain in Q3 revenue" and "reports 15% drop in
    # packaging demand" (both results reporting) are not swept up.
    r"|\b(?:stock|share price|shares)\b[^.]{0,20}\b\d+(?:\.\d+)?%\s+"
    r"(?:pullback|drop|jump|gain|surge|slide|dip|rally|decline)"
    # Same shape with the percentage leading: "...latest 1.9% stock pullback".
    r"|\b\d+(?:\.\d+)?%\s+(?:stock|share price)\s+"
    r"(?:pullback|drop|jump|gain|surge|slide|dip|rally|decline)"
    r"|^what.s behind .{0,80}\b(?:pullback|slide|rally|surge|drop|plunge|jump)\b",
    re.IGNORECASE,
)

# What separates "the stock moved" from "the stock moved BECAUSE of this":
# a concrete corporate or market event. Its presence overrides tier 2.
# Deliberately excludes soft words a pure price story also uses
# ("outlook", "margins", "investors", "analysts"), or the override would
# swallow the whole filter.
#
# Every verb here needs its "-ing" form too, not just "-s"/"-ed": a
# gerund lede ("Novo Nordisk Stock Slides After Unveiling Long Term
# Pipeline Growth Targets") is as common a headline shape as a finite
# verb, and a missing "-ing" form means the tier-2 move-report pattern
# below is never overridden for it. Confirmed live 2026-09-23 — that
# exact Novo Nordisk headline was silently dropped because
# "unveil(?:s|ed)?" doesn't match "unveiling".
#
# The same gap exists for the PAST tense, and it's just as live: an
# earnings-reaction headline is at least as often written "Nestle stock
# falls after it reported weaker sales" as present tense, and
# "report(?:s|ing)?" (no "-ed") missed it just like "unveil(?:s|ed)?"
# missed the gerund. Every verb below now covers all three forms it can
# plausibly appear in. Confirmed live 2026-09-23 against "reported" /
# "posted" / "missed" / "declared" / "divested" / "appointed" /
# "approved" / "sued" — each one was silently misclassified as low-content
# before this fix.
#
# "sold"/"selling" (past/gerund only - present-tense "sells" was already
# bare and unscoped before this file's diff, and isn't the risky one)
# require a determiner right after the verb ("sold ITS/A/AN/THE ...")
# rather than a maintained whitelist of divestiture-object nouns: "sell
# off"/"sold off"/"selling off" is a phrasal verb describing PRICE ACTION
# itself, not a transaction, and "off" is never a determiner, so this
# structurally excludes it without needing to know every possible
# transaction-object noun in advance. Confirmed live 2026-09-23: an
# earlier version scoped to a fixed noun list (stake/business/division/
# unit/arm/brand) within an unchecked 20-char gap, which both missed real
# divestiture headlines using an off-list noun ("sold its operations in
# Brazil") AND let an unrelated segment mention rescue real price-move
# spam ("Shares sold off as retail division slips" - "division" within
# the gap, with no relation to the sale at all). "sale of" is left bare
# since "of" already anchors it to a transaction, not price action.
_EVENT_SIGNAL_RE = re.compile(
    r"\b(?:announc(?:e|es|ed|ing)|unveil(?:s|ed|ing)?|launch(?:es|ed|ing)?|"
    r"acquir(?:e|es|ed|ing)|acquisition|merger|takeover|bid for|"
    r"buyback|repurchase|spin-?off|split|divest(?:s|ing|ed)?|"
    r"sells?|(?:sold|selling)\s+(?:its|their|a|an|the)\b|sale of|"
    r"win(?:s|ning)?|won|awarded|contract|deal|partnership|stake|activist|"
    r"lawsuit|su(?:es?|ing|ed)?|settl(?:es|ed|ement|ing)|ruling|probe|"
    r"investigation|fin(?:e[sd]?|ing)|recall(?:s|ed|ing)?|approval|approv(?:es?|ing|ed)?|fda|clinical|"
    r"ceo|chief executive|chairman|resign(?:s|ing|ed)?|steps? down|stepping down|stepped down|"
    r"appoint(?:s|ing|ed)?|"
    r"nam(?:es?|ing|ed)|job cuts|layoffs?|cuts? [\d,]+|cutting [\d,]+|restructur\w*|overhaul|"
    # Result VERBS only, never the bare calendar nouns. "earnings" /
    # "results" / "dividend" / "profit" on their own rescue pure price
    # reports that merely name a scheduled event — "IBM stock rises as
    # investors await earnings", "Nestle stock falls on profit taking" —
    # and an awaited event is an anti-event: it hasn't happened yet.
    r"post(?:s|ing|ed)?|report(?:s|ing|ed)?|beat(?:s|ing)?|miss(?:es|ing|ed)?|declar(?:es?|ing|ed)?|"
    r"upgrad(?:e|es|ed|ing)|downgrad(?:e|es|ed|ing)|"
    r"strike|factory|tariffs?|sanctions?)\b",
    re.IGNORECASE,
)


def _is_low_content_headline(title: str) -> bool:
    if _HARD_REJECT_RE.search(title):
        return True
    return bool(_MOVE_REPORT_RE.search(title)) and not _EVENT_SIGNAL_RE.search(title)


# --- Relevance filter (ported) ---------------------------------------------

SECTOR_KEYWORDS = {
    "Technology": {
        "ai", "artificial intelligence", "chip", "chips", "semiconductor",
        "software", "cloud", "cybersecurity", "data center", "data centers",
    },
    "Communication Services": {
        "streaming", "advertising", "ad revenue", "social media", "telecom",
        "wireless", "broadband", "5g",
    },
    "Consumer Cyclical": {
        "retail sales", "consumer spending", "e-commerce", "auto sales",
        "vehicle sales", "electric vehicle", "tariff", "tariffs",
    },
    "Consumer Defensive": {
        "retail sales", "consumer spending", "grocery", "beverage",
    },
    "Financial Services": {
        "rate hike", "rate cut", "federal reserve", "fed", "banking",
        "interest rates", "credit", "payments", "fintech",
    },
    "Industrials": {
        "aerospace", "defense", "manufacturing", "supply chain", "factory",
        "airline", "aviation",
    },
    "Energy": {
        "oil", "gas", "crude", "opec", "drilling", "refinery", "pipeline",
    },
    "Healthcare": {
        "drug", "fda", "clinical trial", "biotech", "pharma", "vaccine",
    },
}
SECTOR_KEYWORD_PATTERNS = {
    sector: re.compile(
        r"\b(?:" + "|".join(re.escape(k) for k in sorted(keywords, key=len, reverse=True)) + r")\b",
        re.IGNORECASE,
    )
    for sector, keywords in SECTOR_KEYWORDS.items()
}


# Legal-entity suffixes dropped from a company name before it's matched
# against a headline. The source repo compares the name as-is, which works
# there because its names come from its own metadata; here they come from
# yfinance's `.info`, which reports the full registered name — "Nestlé
# S.A.", "Alphabet Inc.", "Mondi plc" — while headlines write the plain
# brand. Without this, the name tier of the filter never fires for most of
# this portfolio and every holding falls through to the (much weaker)
# ticker and sector tiers.
_LEGAL_SUFFIX_WORDS = frozenset(
    {
        "inc", "incorporated", "corp", "corporation", "co", "cos", "company",
        "ltd", "limited", "plc", "llc", "lp", "llp", "pte", "sarl", "gmbh",
        "sa", "sas", "nv", "bv", "ag", "se", "spa", "ab", "asa", "oyj", "as", "kgaa",
        "holding", "holdings", "group", "the",
    }
)


def _normalize_for_match(text: str) -> str:
    """Accents folded, lower-cased, periods and commas dropped, whitespace
    collapsed — applied to BOTH the name and the headline so "Nestlé S.A."
    and "Nestle SA" compare equal.

    Deliberately leaves "/" alone, unlike "." and ",": a slash in headline
    text is usually a real word separator ("Baidu/Alibaba race for AI
    dominance"), and dropping it would merge the two sides into one token
    and break the `\\b` word-boundary match on either name — only a
    legal-entity suffix like "A/S" needs the slash removed, and that's
    handled locally in _company_match_name instead, where it can't affect
    headline text.
    """
    folded = unicodedata.normalize("NFKD", text)
    folded = "".join(c for c in folded if not unicodedata.combining(c))
    folded = folded.lower().replace(".", "").replace(",", "")
    return " ".join(folded.split())


def _company_match_name(name: str) -> str:
    """The brand part of a registered company name — "Nestle S.A." ->
    "nestle". Returns "" when nothing distinctive survives (a name that is
    all suffix, or a remainder too short to match on without false-
    positiving on ordinary words), in which case the caller falls back to
    the full normalized name.

    Each end's word is also compared with internal "/" removed before the
    _LEGAL_SUFFIX_WORDS lookup — needed for "Novo Nordisk A/S" (Danish,
    this company's own legal-entity suffix, same role as "Inc."/"plc"):
    _normalize_for_match deliberately leaves "/" in place (see its own
    docstring), so the raw word here is "a/s", which never equals the
    "as" entry in _LEGAL_SUFFIX_WORDS on its own. Confirmed live
    2026-09-23: without this, the name needle for Novo Nordisk stayed the
    literal "novo nordisk a/s", which no real headline contains.

    Known limitation, deliberately not chased: a name that carries its
    brand AFTER the suffix ("Petroleo Brasileiro S.A. - Petrobras") keeps
    the whole string and won't match a "Petrobras ..." headline on this
    tier — it still has the ticker tier below.
    """
    words = _normalize_for_match(name).split()
    # Both ends: yfinance reports "The Coca-Cola Company" / "The Home
    # Depot, Inc.", and stripping only the tail leaves "the coca-cola",
    # which never appears in a headline that writes "Coca-Cola".
    while words and words[0].replace("/", "") in _LEGAL_SUFFIX_WORDS:
        words.pop(0)
    while words and words[-1].replace("/", "") in _LEGAL_SUFFIX_WORDS:
        words.pop()
    core = " ".join(words)
    return core if len(core) >= 3 else ""


# A general word-frequency check (the wordfreq library) was tried here,
# splitting a multi-word core into individual words and promoting any
# word that isn't "ordinary English" (by corpus frequency) to its own
# needle - meant to let a multi-word name also match press shorthand for
# it ("Novo Nordisk" -> "Novo") without a maintained per-company alias
# list. Reverted 2026-09-23: it doesn't separate the risk classes that
# actually matter. Corpus frequency conflates "common word" with "common
# NAME" (a person's surname, a place, a generic noun for something this
# company's name happens to also be a word for), and those are
# frequently adjacent on the frequency scale - confirmed live: "novo"
# (3.18, safe - the word this idea existed to catch) and "hathaway"
# (3.29, Anne Hathaway - a real collision: "Anne Hathaway stuns at movie
# premiere" would match Berkshire Hathaway's core split on "hathaway"
# alone) are essentially indistinguishable by this metric, and "depot"
# (3.95, Home Depot's own second word) sits just under any reasonable
# cutoff picked from a small validation set, letting "City opens new
# rail depot" match Home Depot. No threshold closes that gap. This
# file's own bias is to prefer a false negative over a false positive
# (see the comment on _company_match_name's docstring for the "Target
# Corporation" case), and the generic per-word split traded that away for
# real collisions across ordinary large caps, not just an edge case - so
# it's gone.


def _name_needle(name: str | None) -> str:
    """The string a headline is matched against for this company — the
    brand core, or the whole normalized name when no core survives. ""
    when there's no name at all, i.e. skip the name tier."""
    if not name:
        return ""
    return _company_match_name(name) or _normalize_for_match(name)


def _is_relevant_headline(ticker: str, name: str | None, sector: str | None, title: str) -> bool:
    """True if `title` plausibly concerns `ticker`'s company or its sector.
    Checked in order: (1) company name, accent- and suffix-insensitive
    (see _company_match_name), (2) ticker as a standalone, case-SENSITIVE
    token (tickers are conventionally all-caps in real headlines — a
    case-insensitive check on short tickers like V/F/MA/GS would
    false-positive on ordinary English words), (3) sector keyword match,
    if this ticker's sector has an entry. A headline matching none of
    these is dropped."""
    return _matches_holding(ticker, _name_needle(name), sector, title)


def _matches_holding(ticker: str, needle: str, sector: str | None, title: str) -> bool:
    """_is_relevant_headline with the name pre-normalized, so the filter
    loop can hoist that work out of a 30-candidate pass."""
    if needle:
        # Word-boundary, not plain substring: "Sea Limited" -> "sea" would
        # otherwise match the "sea" inside "research" and present an
        # unrelated company's story as news about this holding.
        if re.search(rf"\b{re.escape(needle)}\b", _normalize_for_match(title)):
            return True
    if re.search(rf"\b{re.escape(ticker)}\b", title):
        return True
    keyword_pattern = SECTOR_KEYWORD_PATTERNS.get(sector)
    if keyword_pattern is not None and keyword_pattern.search(title):
        return True
    return False


# --- Feed parsing ----------------------------------------------------------


def _parse_title_and_publisher(raw_title: str) -> tuple[str, str]:
    """Google News RSS titles are conventionally "Headline - Publisher" —
    split it out so LOW_QUALITY_PUBLISHERS can filter on it. Falls back to
    PUBLISHER_FALLBACK when the convention doesn't hold for an entry, so a
    missing publisher can never accidentally match the denylist."""
    if " - " in raw_title:
        title, _, publisher = raw_title.rpartition(" - ")
        return title, publisher
    return raw_title, PUBLISHER_FALLBACK


def _parse_published_date(entry) -> datetime.date | None:
    """feedparser exposes a pre-parsed `published_parsed` (a UTC
    time.struct_time) whenever it recognizes the entry's date format —
    used instead of hand-parsing the raw display string, and needed to
    rank candidates by recency. None when feedparser couldn't parse it;
    callers treat that as "no date signal", not a fetch failure."""
    parsed = entry.get("published_parsed")
    if not parsed:
        return None
    try:
        return datetime.date(parsed.tm_year, parsed.tm_mon, parsed.tm_mday)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class NewsItem:
    title: str
    publisher: str
    published_date: datetime.date | None
    link: str


@dataclass(frozen=True)
class NewsResult:
    """`window_days` is the window the returned items actually came from,
    None when there are none.

    `items` is a tuple, not a list: the same instance is handed out on
    every cache hit for 15 minutes, so a caller mutating it would poison
    the cache for everyone.
    """

    items: tuple[NewsItem, ...]
    window_days: int | None
    # Same values as app.schemas.agent.TickerNews.status, which is where
    # this is validated — keep the two in step.
    status: NewsStatus
    message: str | None = None


def clear_news_cache() -> None:
    """Test-only: drop the in-process TTL layer so a stubbed fetch in one
    test doesn't leak into another (mirrors
    fundamentals.cache.clear_fundamentals_cache)."""
    with _mem_lock:
        _mem.clear()


def _mem_get(key: _CacheKey) -> "NewsResult | None":
    with _mem_lock:
        hit = _mem.get(key)
    if hit is not None and time.monotonic() - hit[0] < _MEM_TTL_SECONDS:
        return hit[1]
    return None


def _mem_put(key: _CacheKey, result: "NewsResult") -> None:
    with _mem_lock:
        if len(_mem) >= _MEM_MAX_ENTRIES:
            cutoff = time.monotonic() - _MEM_TTL_SECONDS
            for stale_key in [k for k, (ts, _) in _mem.items() if ts < cutoff]:
                del _mem[stale_key]
            # A sweep that found nothing stale leaves the dict at capacity
            # and would let it grow without bound — drop the oldest entry
            # so _MEM_MAX_ENTRIES is an actual limit.
            if len(_mem) >= _MEM_MAX_ENTRIES:
                del _mem[min(_mem, key=lambda k: _mem[k][0])]
        _mem[key] = (time.monotonic(), result)


def _fetch_feed(query: str):
    """One Google News RSS search. Split out as the single network seam so
    tests can substitute it without patching httpx or feedparser.

    feedparser.parse(url) fetches internally via urllib with NO timeout — a
    hung connection would pin this thread (and the asyncio.to_thread pool
    slot it came from) forever. Fetch with an explicit timeout here and
    hand feedparser the bytes instead.
    """
    encoded_query = urllib.parse.quote(query)
    rss_url = f"https://news.google.com/rss/search?q={encoded_query}&hl=en-US&gl=US&ceid=US:en"
    response = httpx.get(rss_url, timeout=_REQUEST_TIMEOUT_SECONDS)
    response.raise_for_status()
    return feedparser.parse(response.content)


def _build_candidates(feed) -> list[NewsItem]:
    candidates: list[NewsItem] = []
    for entry in feed.entries[:CANDIDATE_POOL_SIZE]:
        raw_title = entry.get("title", "")
        if not raw_title:
            continue
        title, publisher = _parse_title_and_publisher(raw_title)
        candidates.append(
            NewsItem(
                title=title,
                publisher=publisher,
                published_date=_parse_published_date(entry),
                link=_safe_link(entry.get("link", "")),
            )
        )
    return candidates


def _safe_link(link: str) -> str:
    """A feed is an external input like any other, and this URL is handed
    to the frontend to render as an anchor — a `javascript:` or `data:`
    scheme from a malformed or hostile entry must not get that far. An
    unusable link is blanked rather than dropping the headline, which is
    still worth reading."""
    return link if link.startswith("https://") else ""


def _meaningful(candidates: list[NewsItem], ticker: str, name: str | None, sector: str | None) -> list[NewsItem]:
    """Quality filter then relevance filter, deduped by normalized title.
    Google News returns the same syndicated story under several publishers
    within one window, and without this a "top 5" is regularly the same
    headline five times."""
    # Hoisted: the name normalization is identical for every candidate, and
    # this runs over up to CANDIDATE_POOL_SIZE entries per window.
    needle = _name_needle(name)
    seen: set[str] = set()
    kept: list[NewsItem] = []
    for candidate in candidates:
        if _is_low_quality_publisher(candidate.publisher) or _is_low_content_headline(candidate.title):
            continue
        if not _matches_holding(ticker, needle, sector, candidate.title):
            continue
        key = candidate.title.strip().lower()
        if key in seen:
            continue
        seen.add(key)
        kept.append(candidate)
    return kept


def _newest_first(items: list[NewsItem]) -> list[NewsItem]:
    """Most recent first. Entries with no parseable date sort last but keep
    their original RSS order among themselves — Google News already returns
    results roughly newest-first, so that's a reasonable degrade rather
    than an arbitrary shuffle."""
    dated = [i for i in items if i.published_date is not None]
    undated = [i for i in items if i.published_date is None]
    return sorted(dated, key=lambda i: i.published_date, reverse=True) + undated


def fetch_ticker_news(
    ticker: str,
    name: str | None = None,
    sector: str | None = None,
    limit: int = 5,
    fetch: Callable[[str], object] | None = None,
) -> NewsResult:
    """Up to `limit` recent, meaningful headlines about `ticker`'s company.

    Widens the search window (SEARCH_WINDOWS_DAYS) until one yields at
    least one headline passing both the quality and relevance filters, then
    returns that window's newest `limit`. It does NOT keep widening to pad
    the list — two genuinely recent headlines beat two recent ones plus
    three from six months ago.

    Blocking: does its own synchronous HTTP (one request per window tried,
    at most len(SEARCH_WINDOWS_DAYS)). Async callers wrap it in
    asyncio.to_thread.
    """
    # Resolved here rather than as a default argument so that patching the
    # module attribute (what a test does) actually takes effect.
    fetch = fetch or _fetch_feed
    limit = max(1, min(limit, 10))
    search_ticker = _strip_exchange_suffix(ticker)
    subject = name or search_ticker

    cache_key = (search_ticker, subject, sector or "")
    cached = _mem_get(cache_key)
    if cached is not None:
        return _limited(cached, limit)

    for window_days in SEARCH_WINDOWS_DAYS:
        query = f"{subject} stock earnings financial news when:{window_days}d"
        try:
            feed = fetch(query)
        except Exception as exc:  # noqa: BLE001 — any fetch failure is the same answer to the caller
            logger.warning("Google News RSS error for %s (when:%dd): %s", ticker, window_days, exc)
            # Not cached: a transient network failure shouldn't suppress
            # news for the next 15 minutes.
            return NewsResult(items=(), window_days=None, status="error", message="Could not reach the news feed.")

        meaningful = _meaningful(_build_candidates(feed), search_ticker, name, sector)
        if meaningful:
            # The whole window is cached; `limit` is applied on the way out
            # so a later call with a different limit still hits the entry.
            result = NewsResult(items=tuple(_newest_first(meaningful)), window_days=window_days, status="ok")
            _mem_put(cache_key, result)
            return _limited(result, limit)

        logger.info(
            "fetch_ticker_news[%s]: nothing meaningful within %d days, widening the window", ticker, window_days
        )

    result = NewsResult(
        items=(),
        window_days=None,
        status="no_news",
        message=f"No meaningful news found for {ticker} in the past {SEARCH_WINDOWS_DAYS[-1]} days.",
    )
    # Cached, unlike the error branch above: "genuinely quiet" is a real
    # answer, and re-running four RSS fetches per turn to re-derive it is
    # exactly what this cache exists to prevent.
    _mem_put(cache_key, result)
    return result


def _limited(result: NewsResult, limit: int) -> NewsResult:
    if len(result.items) <= limit:
        return result
    return replace(result, items=result.items[:limit])
