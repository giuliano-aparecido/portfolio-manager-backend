"""The Google News feed client (app/services/news.py) — filters, window
escalation and ordering, with the network seam stubbed out. No DB, no HTTP.
"""

import datetime

import pytest

from app.services.news import (
    LOW_QUALITY_PUBLISHERS,
    _company_match_name,
    SEARCH_WINDOWS_DAYS,
    _EVENT_SIGNAL_RE,
    _is_low_content_headline,
    _is_low_quality_publisher,
    _is_relevant_headline,
    _parse_title_and_publisher,
    _strip_exchange_suffix,
    clear_news_cache,
    fetch_ticker_news,
)


class FakeFeed:
    def __init__(self, entries):
        self.entries = entries


def entry(title: str, *, published: datetime.date | None = None, link: str = "https://news.example/a"):
    """One feedparser-shaped entry. `published_parsed` is the struct_time
    feedparser exposes; None models an entry whose date it couldn't parse."""
    raw = {"title": title, "link": link}
    if published is not None:
        raw["published"] = published.strftime("%a, %d %b %Y 12:00:00 GMT")
        raw["published_parsed"] = published.timetuple()
    else:
        raw["published"] = ""
    return raw


def feeds(*per_window):
    """Builds a fetch stub returning a different feed per call, i.e. per
    search window, and records the queries it was asked for."""
    calls: list[str] = []
    responses = list(per_window)

    def fetch(query: str):
        calls.append(query)
        return FakeFeed(responses[len(calls) - 1] if len(calls) <= len(responses) else [])

    fetch.calls = calls  # type: ignore[attr-defined]
    return fetch


TODAY = datetime.date(2026, 9, 18)
GOOD = "Nestle raises full-year outlook after coffee price pass-through - Reuters"


# --- Filters ---------------------------------------------------------------


@pytest.mark.parametrize(
    "title",
    [
        "Intel Stock Trades Up, Here Is Why",
        "Why Tesla Stock Dropped on Tuesday",
        "Should You Buy Microsoft Stock?",
        "3 Reasons to Buy Nestle Stock",
        "46,643 Shares in Oracle Corporation Purchased by Trust Co. of Vermont",
        "Nestle Shares Acquired by Some Advisors LLC",
        "Better Buy: Nestle or Unilever",
    ],
)
def test_low_content_headline_shapes_are_rejected(title):
    assert _is_low_content_headline(title)


@pytest.mark.parametrize(
    "title",
    [
        # Every one of these was returned by a live run of the tool on
        # 2026-09-18 and prompted a filter branch beyond the ported set.
        "Nestle stock gains modestly as investors focus on 2026 margins",
        "Nestle stock edges lower after recent gains as investors weigh margin outlook",
        "What's behind British American Tobacco's latest 1.9% stock pullback?",
        "Here Is How to Play IBM Stock After Its Arm and IBM Z Breakthrough",
        "Jim Cramer Says IBM Is A Hated Stock That Deserves Better",
        "Amazon.com, Inc. (AMZN) is Attracting Investor Attention: Here is What You Should Know",
        "Berkshire Hathaway (BRK.A) Moved Today, What Is Drawing Fresh Attention?",
        "Berkshire Hathaway B (BRK.B) Stock Declines While Market Improves",
        "BRK.B Stock Quote Price and Forecast",
    ],
)
def test_headlines_observed_slipping_through_live_are_now_rejected(title):
    assert _is_low_content_headline(title)


@pytest.mark.parametrize(
    "title",
    [
        "Nestle raises full-year outlook after coffee price pass-through",
        "Nestle to sell its water brands in a $4bn deal",
        "Nestle names a new chief executive",
        # The added branches are adjacency- and noun-scoped on purpose:
        # real reporting that merely contains a move word must survive.
        "Nestle gains approval for a new plant in Vietnam",
        "IBM stock split proposal goes to shareholders",
        # Rejected by an earlier, flatter version of the filter; survives
        # now because a completed buyback is a concrete event.
        "British American Tobacco stock trades near GBX4,148 support as buyback completed",
    ],
)
def test_real_reporting_survives_the_shape_filter(title):
    assert not _is_low_content_headline(title)


@pytest.mark.parametrize(
    "title",
    [
        # THE regression set. A tightening pass on 2026-09-18 rejected 9 of
        # these 10 — every one caught by a move-verb pattern, because a
        # move verb is how real reporting leads an event headline. That
        # over-rejection is worse than it looks: nothing meaningful
        # surviving makes the caller widen its window and report a busy
        # week as a quiet one. Any future filter change must keep these.
        "Nestle shares surge after activist investor Third Point builds stake",
        "IBM shares gain as quantum unit wins $2bn government contract",
        "British American Tobacco stock jumps on US menthol ruling",
        "Berkshire Hathaway holds near-record cash as Buffett stays on sidelines",
        "Mondi reports 15% drop in packaging demand",
        "Vale shares slide after dam ruling",
        "Nestle to cut 16,000 jobs in biggest overhaul in decades",
        "Roche posts 8% gain in Q3 revenue as pharma division beats",
        "What's behind Nestle's decision to sell its water business",
        "IBM holds above 5% stake in Kyndryl after spinoff",
        "Amazon and Nvidia announce a multi-year AI chip partnership",
    ],
)
def test_event_headlines_are_never_dropped_as_move_reports(title):
    assert not _is_low_content_headline(title)


@pytest.mark.parametrize(
    "title",
    [
        # Past-tense verb forms - see _EVENT_SIGNAL_RE's comment.
        "Nestle stock falls after it reported weaker sales",
        "Nestle stock falls after it posted weaker sales",
        "Nestle stock falls after it missed sales targets",
        "Nestle stock falls as it declared a special dividend",
        "Nestle stock falls as it divested a subsidiary",
        "Nestle stock falls as it appointed a new finance chief",
        "Nestle stock falls after regulators approved a rival drug",
        "Nestle stock falls after it was sued by a former supplier",
        "Nestle stock falls as regulators are fining the company",
    ],
)
def test_event_headlines_in_past_tense_are_never_dropped_as_move_reports(title):
    assert not _is_low_content_headline(title)


@pytest.mark.parametrize(
    "title",
    [
        # "sold"/"selling" without a determiner must not trigger the
        # override - see _EVENT_SIGNAL_RE's comment.
        "Nestle shares are sold off after weak guidance",
        "Nestle stock is selling off sharply today",
        "Nestle shares selling off after weak guidance",
        # An unrelated segment mention nearby must not rescue these either.
        "Shares sold off as retail division slips",
        "Tech shares sold broadly as chip division slumps",
        "Bank shares are selling off as trading unit struggles",
        "Retailer shares sold off as e-commerce arm underperforms",
    ],
)
def test_bare_sold_or_selling_does_not_trigger_the_event_override(title):
    # These aren't rejected by the move-report tier today either (no
    # existing pattern treats "sold off" as a price move), so this only
    # guards the override itself: it must not fire on price action alone.
    assert not _EVENT_SIGNAL_RE.search(title)


@pytest.mark.parametrize(
    "title",
    [
        # A real divestiture (any object noun) must still override tier 2.
        "Nestle stock falls despite selling its water business",
        "Nestle stock rises after it sold its stake in a JV",
        "Nestle stock falls as it sold its operations in Brazil",
        "Nestle stock falls as it sold its subsidiary in China",
        "Nestle stock falls as it is selling its manufacturing plant",
        # Present-tense "sells" was already bare/unscoped before this
        # diff and stays that way - it isn't the risky tense.
        "Nestle stock gains as it sells its shares in a joint venture",
        "Nestle stock falls as it sells non-core assets",
    ],
)
def test_a_scoped_divestiture_still_triggers_the_event_override(title):
    assert not _is_low_content_headline(title)


@pytest.mark.parametrize(
    "title",
    [
        # The same defect as above, second instance: a first pass at
        # covering 13F spam rejected these too. They sit in the HARD
        # reject tier, where _EVENT_SIGNAL_RE structurally cannot rescue
        # them — so a company changing a stake, which for Berkshire is its
        # most-reported event type, vanished silently. What separates spam
        # from news here is an institutional filer as the subject, not the
        # presence of a holdings noun.
        "Berkshire Hathaway Boosts Stake in Occidental Petroleum",
        "Nestle Sells Stake in Its Water Business to Private Equity",
        "Nestle Raises Stake in Health Science Venture to 60%",
        "IBM Acquires Position in Quantum Startup",
        "Vale Cuts Stake in Joint Venture as Part of Overhaul",
    ],
)
def test_a_company_changing_a_stake_is_not_mistaken_for_filing_spam(title):
    assert not _is_low_content_headline(title)


@pytest.mark.parametrize(
    "title",
    [
        # "here.s why" (the ported spelling) matches "here's why" but not
        # "Here Is Why". That gap was masked while the move pattern caught
        # these anyway; once tier 2 became overridable it stopped being.
        "IBM Stock Trades Up After Revenue Report, Here Is Why",
        "Here Is Why Nestle Stock Rose Today",
        # A scheduled event that hasn't happened is an anti-event: naming
        # it must not rescue a pure price report.
        "Nestle stock is trading lower ahead of results",
        "IBM stock rises as investors await earnings",
        "Nestle stock falls on profit taking",
        # Observed live. Reports stasis after a scheduled event without
        # saying what the event contained.
        "Nestle stock holds steady after half-year 2026 results and dividend outlook",
    ],
)
def test_the_event_override_does_not_re_admit_pure_price_reports(title):
    assert _is_low_content_headline(title)


@pytest.mark.parametrize(
    "publisher",
    ["MarketBeat", "marketbeat.com", "Simply Wall St.", "simplywall.st", "Zacks Investment Research"],
)
def test_a_denylisted_publisher_is_matched_however_google_news_spells_it(publisher):
    """Observed live 2026-09-18: exact-string matching let every
    "marketbeat.com" result through while "MarketBeat" was denied."""
    assert _is_low_quality_publisher(publisher)


@pytest.mark.parametrize("publisher", ["Reuters", "Bloomberg", "Financial Times", "marketscreener.com"])
def test_a_real_publisher_is_not_denylisted(publisher):
    assert not _is_low_quality_publisher(publisher)


@pytest.mark.parametrize(
    "title",
    [
        # All four IBM results in one live 7-day window, 2026-09-18 —
        # 13F filing spam the ported branches didn't cover.
        "Sequoia Financial Advisors LLC Acquires 14,178 Shares of International Business Machines",
        "Van Hulzen Asset Management LLC Has $31.69 Million Stock Holdings in IBM",
        "Baird Financial Group Inc. Reduces Position in International Business Machines Corporation",
        "Capital Analysts LLC Buys 7,892 Shares of International Business Machines Corporation",
    ],
)
def test_fund_filing_spam_is_rejected(title):
    assert _is_low_content_headline(title)


def test_relevance_matches_a_name_whose_legal_suffix_contains_a_slash():
    # Regression test for the Danish "A/S" legal-suffix slash - see
    # _company_match_name's docstring.
    assert _is_relevant_headline(
        "NVO", "Novo Nordisk A/S", "Healthcare",
        "Novo Nordisk Stock Slides After Unveiling Long Term Pipeline Growth Targets",
    )


def test_slash_in_a_headline_does_not_merge_two_names_into_one_token():
    # Regression test for _normalize_for_match's slash-preservation
    # contract - see its docstring.
    assert _is_relevant_headline("BIDU", "Baidu", "Technology", "Baidu/Alibaba race for AI dominance")


def test_low_content_headline_does_not_reject_a_gerund_event_lede():
    # Gerund verb form - see _EVENT_SIGNAL_RE's comment.
    assert not _is_low_content_headline(
        "Novo Nordisk Stock Slides After Unveiling Long Term Pipeline Growth Targets"
    )


def test_relevance_matches_company_name_case_insensitively():
    assert _is_relevant_headline("NESN", "Nestle S.A.", None, "nestle s.a. lifts guidance")


def test_relevance_matches_ticker_as_a_whole_token_case_sensitively():
    assert _is_relevant_headline("NESN", None, None, "Analysts upgrade NESN after results")
    # A lowercase English word must not match a short ticker — the reason
    # the ticker tier is case-sensitive.
    assert not _is_relevant_headline("V", None, None, "The v shaped recovery continues")


@pytest.mark.parametrize(
    ("registered_name", "core"),
    [
        ("Nestlé S.A.", "nestle"),
        ("Alphabet Inc.", "alphabet"),
        ("Mondi plc", "mondi"),
        ("Berkshire Hathaway Inc.", "berkshire hathaway"),
        ("BKW AG", "bkw"),
        # Stripped from BOTH ends — tail-only leaves "the coca-cola",
        # which never appears in a headline that writes "Coca-Cola".
        ("The Coca-Cola Company", "coca-cola"),
        ("The Home Depot, Inc.", "home depot"),
        # Nothing distinctive survives — the caller falls back to the full
        # name rather than matching on an empty string.
        ("The Group Holdings Inc", ""),
    ],
)
def test_company_match_name_strips_accents_and_legal_suffixes(registered_name, core):
    assert _company_match_name(registered_name) == core


def test_relevance_matches_a_registered_name_against_a_plain_brand_headline():
    """yfinance reports "Nestlé S.A."; headlines write "Nestle". Without
    folding both, the name tier never fires for most of this portfolio."""
    assert _is_relevant_headline("NESN", "Nestlé S.A.", None, "Nestle raises full-year outlook")


def test_relevance_still_rejects_an_unrelated_headline_after_normalization():
    assert not _is_relevant_headline("NESN", "Nestlé S.A.", None, "Boeing wins a defense contract")


def test_relevance_matches_a_brand_that_survives_only_after_a_leading_the():
    assert _is_relevant_headline("KO", "The Coca-Cola Company", None, "Coca-Cola lifts full-year guidance")


def test_relevance_matches_the_name_on_a_word_boundary_not_a_substring():
    """"Sea Limited" -> "sea" must not match the "sea" inside "research",
    or an unrelated company's story is presented as this holding's news."""
    assert not _is_relevant_headline("SE", "Sea Limited", None, "New research from Boeing on wing design")
    assert _is_relevant_headline("SE", "Sea Limited", None, "Sea posts record quarterly revenue")


def test_relevance_falls_back_to_the_full_name_when_no_core_survives():
    """A name that is all legal-entity words has no core, so the whole
    normalized name is matched instead of an empty string (which would
    otherwise match every headline)."""
    assert _is_relevant_headline("XYZ", "The Group Holdings Inc", None, "The Group Holdings Inc names a new CFO")
    assert not _is_relevant_headline("XYZ", "The Group Holdings Inc", None, "Boeing wins a defense contract")


def test_relevance_falls_through_to_sector_keywords():
    assert _is_relevant_headline("NVDA", "Nvidia", "Technology", "Semiconductor demand keeps climbing")
    assert not _is_relevant_headline("NVDA", "Nvidia", "Technology", "Grocery prices fall again")


def test_relevance_rejects_an_unrelated_headline():
    assert not _is_relevant_headline("NESN", "Nestle S.A.", "Consumer Defensive", "Boeing wins a defense contract")


def test_parse_title_and_publisher_falls_back_when_the_convention_breaks():
    assert _parse_title_and_publisher("Headline - Reuters") == ("Headline", "Reuters")
    assert _parse_title_and_publisher("Headline with no publisher")[1] == "Google News"


def test_exchange_suffix_stripped_but_a_share_class_is_left_alone():
    assert _strip_exchange_suffix("ARYN.SW") == "ARYN"
    assert _strip_exchange_suffix("BATS.L") == "BATS"
    # The regression the source repo's general `\.[A-Za-z]{1,3}$` would
    # cause here: BRK.B is a share class, not an exchange suffix.
    assert _strip_exchange_suffix("BRK.B") == "BRK.B"
    assert _strip_exchange_suffix("VITL-UN") == "VITL-UN"


# --- Selection -------------------------------------------------------------


def test_returns_newest_first_up_to_limit():
    fetch = feeds([
        entry("Nestle wins an award - Reuters", published=datetime.date(2026, 9, 12)),
        entry("Nestle opens a plant - Bloomberg", published=datetime.date(2026, 9, 17)),
        entry("Nestle signs a supplier deal - FT", published=datetime.date(2026, 9, 15)),
    ])
    result = fetch_ticker_news("NESN", name="Nestle", sector="Consumer Defensive", limit=2, fetch=fetch)

    assert result.status == "ok"
    assert [i.published_date for i in result.items] == [datetime.date(2026, 9, 17), datetime.date(2026, 9, 15)]
    assert result.items[0].publisher == "Bloomberg"
    assert result.window_days == SEARCH_WINDOWS_DAYS[0]


def test_low_quality_publisher_and_junk_shapes_never_reach_the_caller():
    junk_publisher = sorted(LOW_QUALITY_PUBLISHERS)[0]
    fetch = feeds([
        entry(f"Nestle S.A. Shares Sold by Someone - {junk_publisher}", published=TODAY),
        entry("Why Nestle Stock Dropped on Tuesday - Motley Fool", published=TODAY),
        entry(GOOD, published=TODAY),
    ])
    result = fetch_ticker_news("NESN", name="Nestle", sector=None, limit=5, fetch=fetch)

    assert [i.publisher for i in result.items] == ["Reuters"]


def test_duplicate_syndicated_headlines_are_collapsed():
    fetch = feeds([
        entry("Nestle raises full-year outlook - Reuters", published=TODAY),
        entry("nestle raises full-year outlook - Yahoo Finance", published=TODAY),
    ])
    result = fetch_ticker_news("NESN", name="Nestle", sector=None, limit=5, fetch=fetch)

    assert len(result.items) == 1


def test_undated_entries_sort_last_without_being_dropped():
    fetch = feeds([
        entry("Nestle opens a plant - Reuters", published=None),
        entry("Nestle signs a supplier deal - FT", published=TODAY),
    ])
    result = fetch_ticker_news("NESN", name="Nestle", sector=None, limit=5, fetch=fetch)

    assert [i.published_date for i in result.items] == [TODAY, None]


# --- Window escalation -----------------------------------------------------


def test_window_widens_until_something_meaningful_is_found():
    fetch = feeds(
        [entry("Should You Buy Nestle Stock? - Motley Fool", published=TODAY)],  # 7d: all junk
        [],  # 30d: empty
        [entry(GOOD, published=datetime.date(2026, 7, 2))],  # 90d: a real one
    )
    result = fetch_ticker_news("NESN", name="Nestle", sector=None, limit=5, fetch=fetch)

    assert result.status == "ok"
    assert result.window_days == 90
    assert len(fetch.calls) == 3
    assert [f"when:{d}d" in q for d, q in zip(SEARCH_WINDOWS_DAYS, fetch.calls)] == [True, True, True]


def test_a_productive_first_window_does_not_widen_to_pad_the_list():
    """Two genuinely recent headlines beat two recent plus three stale ones
    — escalation exists to find *any* news, not to fill `limit`."""
    fetch = feeds([entry(GOOD, published=TODAY)])
    result = fetch_ticker_news("NESN", name="Nestle", sector=None, limit=5, fetch=fetch)

    assert len(result.items) == 1
    assert len(fetch.calls) == 1


def test_no_news_when_every_window_is_exhausted():
    fetch = feeds()  # every call returns an empty feed
    result = fetch_ticker_news("NESN", name="Nestle", sector=None, limit=5, fetch=fetch)

    assert result.status == "no_news"
    assert result.items == ()
    assert result.window_days is None
    assert len(fetch.calls) == len(SEARCH_WINDOWS_DAYS)


def test_the_query_searches_the_company_name_when_one_is_known():
    fetch = feeds([entry(GOOD, published=TODAY)])
    fetch_ticker_news("NESN", name="Nestle S.A.", sector=None, fetch=fetch)
    assert fetch.calls[0].startswith("Nestle S.A. stock earnings financial news")

    clear_news_cache()
    bare = feeds([entry("NESN wins an award - Reuters", published=TODAY)])
    fetch_ticker_news("NESN.SW", name=None, sector=None, fetch=bare)
    assert bare.calls[0].startswith("NESN stock earnings financial news")


# --- Failure and caching ---------------------------------------------------


def test_a_fetch_failure_is_reported_not_raised():
    def boom(query: str):
        raise RuntimeError("connection reset")

    result = fetch_ticker_news("NESN", name="Nestle", sector=None, fetch=boom)

    assert result.status == "error"
    assert result.items == ()


def test_a_fetch_failure_is_not_cached():
    """A transient network blip must not suppress news for the whole TTL."""
    calls = {"n": 0}

    def flaky(query: str):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("connection reset")
        return FakeFeed([entry(GOOD, published=TODAY)])

    assert fetch_ticker_news("NESN", name="Nestle", sector=None, fetch=flaky).status == "error"
    assert fetch_ticker_news("NESN", name="Nestle", sector=None, fetch=flaky).status == "ok"


def test_a_successful_result_is_served_from_cache():
    fetch = feeds([entry(GOOD, published=TODAY)])
    first = fetch_ticker_news("NESN", name="Nestle", sector=None, fetch=fetch)
    second = fetch_ticker_news("NESN", name="Nestle", sector=None, fetch=fetch)

    assert second == first
    assert len(fetch.calls) == 1


def test_the_cache_does_not_serve_one_ticker_s_result_for_another():
    """_mem is a process-global shared across every user and holding — the
    one property of it with a correctness consequence."""
    nesn = feeds([entry("Nestle wins an award - Reuters", published=TODAY)])
    fetch_ticker_news("NESN", name="Nestle", sector=None, fetch=nesn)

    ibm = feeds([entry("IBM signs a cloud deal - Reuters", published=TODAY)])
    result = fetch_ticker_news("IBM", name="IBM", sector=None, fetch=ibm)

    assert len(ibm.calls) == 1
    assert "IBM" in result.items[0].title


def test_a_different_company_name_or_sector_is_a_different_cache_entry():
    first = feeds([entry("Nestle wins an award - Reuters", published=TODAY)])
    fetch_ticker_news("NESN", name="Nestle", sector=None, fetch=first)

    second = feeds([entry("Nestle wins an award - Reuters", published=TODAY)])
    fetch_ticker_news("NESN", name="Nestle S.A.", sector=None, fetch=second)
    assert len(second.calls) == 1

    third = feeds([entry("Nestle wins an award - Reuters", published=TODAY)])
    fetch_ticker_news("NESN", name="Nestle", sector="Consumer Defensive", fetch=third)
    assert len(third.calls) == 1


def test_a_cached_window_serves_a_later_call_asking_for_a_different_limit():
    """`limit` is an LLM-chosen argument, so it must not be part of the
    cache key — the cached window already holds the smaller answer."""
    many = feeds([entry(f"Nestle deal number {n} - Reuters", published=TODAY) for n in range(8)])
    narrow = fetch_ticker_news("NESN", name="Nestle", sector=None, limit=3, fetch=many)
    # Widening AFTER a narrow read is the order that catches a slice
    # applied to the cached entry in place.
    wide = fetch_ticker_news("NESN", name="Nestle", sector=None, limit=5, fetch=many)

    assert len(narrow.items) == 3
    assert len(wide.items) == 5
    assert len(many.calls) == 1


def test_no_news_is_cached_so_a_quiet_holding_is_not_re_fetched_every_turn():
    """The deliberate contrast with the error branch: "genuinely quiet" is
    a real answer, not a failure to retry."""
    fetch = feeds()
    assert fetch_ticker_news("NESN", name="Nestle", sector=None, fetch=fetch).status == "no_news"
    calls_after_first = len(fetch.calls)

    assert fetch_ticker_news("NESN", name="Nestle", sector=None, fetch=fetch).status == "no_news"
    assert len(fetch.calls) == calls_after_first


def test_the_widest_window_is_actually_tried_before_giving_up():
    fetch = feeds(
        [],
        [],
        [],
        [entry("Nestle signs a supplier deal - Reuters", published=datetime.date(2025, 11, 1))],
    )
    result = fetch_ticker_news("NESN", name="Nestle", sector=None, fetch=fetch)

    assert result.window_days == SEARCH_WINDOWS_DAYS[-1] == 365
    assert f"when:{SEARCH_WINDOWS_DAYS[-1]}d" in fetch.calls[-1]


def test_the_no_news_message_names_the_widest_window_searched():
    result = fetch_ticker_news("NESN", name="Nestle", sector=None, fetch=feeds())
    assert str(SEARCH_WINDOWS_DAYS[-1]) in result.message


def test_a_feed_link_that_is_not_https_is_blanked():
    """The feed is external input and the link is rendered as an anchor
    downstream — a `javascript:` scheme must not reach the frontend."""
    fetch = feeds([
        entry("Nestle wins an award - Reuters", published=TODAY, link="javascript:alert(1)"),
        entry("Nestle signs a supplier deal - FT", published=TODAY, link="https://news.example/ok"),
    ])
    result = fetch_ticker_news("NESN", name="Nestle", sector=None, fetch=fetch)

    links = {i.title: i.link for i in result.items}
    assert links["Nestle wins an award"] == ""
    assert links["Nestle signs a supplier deal"] == "https://news.example/ok"


def test_limit_is_clamped_to_a_sane_range():
    many = [entry(f"Nestle result number {n} - Reuters", published=TODAY) for n in range(20)]
    assert len(fetch_ticker_news("NESN", name="Nestle", sector=None, limit=99, fetch=feeds(many)).items) == 10

    clear_news_cache()
    assert len(fetch_ticker_news("NESN", name="Nestle", sector=None, limit=0, fetch=feeds(many)).items) == 1
