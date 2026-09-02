"""Company-fundamentals for the value-investing agent tools.

Layered so the data source is swappable and the daily Postgres cache is
the only thing that ever talks to it:

- base.py     — FundamentalsData / the FundamentalsProvider protocol / the
                typed errors, plus get_fundamentals_provider() (the factory
                that reads settings.fundamentals_provider, mirroring
                app/routers/agent.py's get_llm_provider()).
- yahoo_provider.py — the one provider today (yfinance .info, no API key).
                Adapted from financial-sentiment-api's app/services/
                fundamentals.py; that repo is where an MCP/agent fronting
                the fine-tuned model will eventually live, at which point
                this copy is replaced rather than kept in lockstep.
- screen.py   — the value-investing overlay (sector-relative P/E, REIT
                flag, per-metric confidence bands) from
                financial-sentiment-api's value-investing-checklist.md.
- cache.py    — get_fundamentals(): the once-per-UTC-day Postgres cache,
                429-only retries, portfolio-composition GC. The agent
                tools call ONLY this.
"""

from app.services.fundamentals.base import (
    FundamentalsData,
    FundamentalsProvider,
    FundamentalsRateLimited,
    FundamentalsUnavailable,
    get_fundamentals_provider,
)

__all__ = [
    "FundamentalsData",
    "FundamentalsProvider",
    "FundamentalsRateLimited",
    "FundamentalsUnavailable",
    "get_fundamentals_provider",
]
