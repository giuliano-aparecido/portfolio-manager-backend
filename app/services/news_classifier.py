"""Gemini screen rating news headlines for relevance to a company and
importance to its stock."""

import logging
from dataclasses import dataclass

from google import genai
from google.genai import types
from pydantic import BaseModel

from app.config import get_settings

logger = logging.getLogger(__name__)

IMPORTANCE_THRESHOLD = 3
_TIMEOUT_MS = 15_000

_SYSTEM_INSTRUCTION = (
    "You are an equity research analyst screening news headlines for one company. "
    "For each numbered headline decide:\n"
    "- relevant: true if the headline is about this company, or reports an event that "
    "directly affects its business or share price (e.g. a competitor's result in its core "
    "market, a regulator ruling on its products or industry). The company may be referred to by a "
    "shortened name, brand, product, subsidiary, ticker or executive. False if it is about a "
    "different company that merely shares a word with this one, a separately listed affiliate "
    "reporting its own local results, or only mentions it in passing.\n"
    "- importance: 1-5, how likely the news is to influence the company's stock price or "
    "fundamental outlook. Rate the concrete event the headline reports, not the company's size. "
    "5 = material (earnings, guidance, M&A, major regulatory or legal outcome, CEO change, "
    "large contract, pipeline readout). 3 = meaningful but secondary. "
    "A headline about a share-price move is rated only by the cause it names; if it names no "
    "concrete cause, importance is at most 2. 1 = no news event: stock-quote or news-listing "
    "pages, valuation questions ('is X undervalued', 'should you buy', 'still attractive'), "
    "listicles, opinion, analyst-rating or fund-holdings (13F) filings, promotional content.\n"
    "Return one entry per headline, using its number as index."
)


class _Assessment(BaseModel):
    index: int
    relevant: bool
    importance: int


@dataclass(frozen=True)
class HeadlineAssessment:
    relevant: bool
    importance: int

    @property
    def keep(self) -> bool:
        return self.relevant and self.importance >= IMPORTANCE_THRESHOLD


def classify_headlines(
    company: str, ticker: str, sector: str | None, headlines: list[tuple[str, str]]
) -> list[HeadlineAssessment] | None:
    """One assessment per (title, publisher), in input order. None when no
    API key is configured, or on any request/parse failure or a response
    that doesn't cover every headline."""
    settings = get_settings()
    if not headlines or not settings.gemini_api_key:
        return None

    numbered = "\n".join(f"{i}. {title} - {publisher}" for i, (title, publisher) in enumerate(headlines))
    prompt = f"Company: {company} (ticker {ticker}), sector: {sector or 'unknown'}\n\nHeadlines:\n{numbered}"
    config = types.GenerateContentConfig(
        system_instruction=_SYSTEM_INSTRUCTION,
        temperature=0,
        response_mime_type="application/json",
        response_schema=list[_Assessment],
        thinking_config=types.ThinkingConfig(thinking_budget=0),
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
    )
    try:
        with genai.Client(
            api_key=settings.gemini_api_key, http_options=types.HttpOptions(timeout=_TIMEOUT_MS)
        ) as client:
            parsed = client.models.generate_content(model=settings.gemini_model, contents=prompt, config=config).parsed
    except Exception as exc:  # noqa: BLE001
        logger.warning("news classification failed for %s: %s", ticker, exc)
        return None

    by_index = {a.index: a for a in parsed or [] if 0 <= a.index < len(headlines)}
    if len(by_index) != len(headlines):
        logger.warning("news classification for %s covered %d of %d headlines", ticker, len(by_index), len(headlines))
        return None
    return [
        HeadlineAssessment(relevant=by_index[i].relevant, importance=max(1, min(5, by_index[i].importance)))
        for i in range(len(headlines))
    ]
