# Project Overview

## What this is

A REST API for tracking a multi-currency personal investment portfolio,
with CHF as the base currency. It's a from-scratch Python/FastAPI port of
the backend half of [`MyPortfolio`](https://github.com/GiulianoAparecido/MyPortfolio)
(originally Next.js/TypeScript/Prisma), serving
[`portfolio-manager-frontend`](https://github.com/GiulianoAparecido/portfolio-manager-frontend).
The original Node.js app remains live and unchanged — this is a parallel
rewrite, not a replacement in place.

Two portfolio types are tracked, with different data models because they
behave differently:

- **Securities** (stocks, ETFs, crypto) — quantity-based positions with a
  real, live market price. Cost basis is computed via FIFO lot matching,
  not a running average, because share purchases at different times/prices
  need to be consumed oldest-first for correct realized-gain accounting on
  partial sells.
- **Passive investments** (cash accounts, pension funds, investment funds)
  — no ticker, no live price. Cost basis is just the net balance of a
  deposit/withdrawal ledger. Market value beyond that is driven by a
  manually-entered gain/loss percentage, since there's no way to look up a
  live valuation automatically.

## Architecture

```
app/
  main.py            FastAPI app factory, CORS, router mounting, exception handlers
  config.py          Settings (env vars): ENVIRONMENT, DATABASE_URL, NEXTAUTH_SECRET, FRONTEND_ORIGIN
  exceptions.py       AppError hierarchy -> {"error": "..."} JSON responses
  db/                 SQLAlchemy engine/session
  models/             One file per table (SQLAlchemy 2.0 Mapped/mapped_column, snake_case)
  schemas/            Pydantic v2 request/response models
  dependencies/auth.py  Resolves the authenticated user from a Bearer JWT
  routers/            Thin HTTP layer: parse request -> call a service -> map result to response
  services/           All business logic, framework-agnostic and unit-testable without HTTP:
    fifo.py              FIFO cost-basis engine for securities
    ledger.py            Passive-investment deposit/withdrawal ledger math
    recurring.py         Recurring-deposit occurrence scheduling
    ticker_config.py     Hardcoded per-ticker facts (Yahoo symbol, currency, market, category)
    price_service.py     Live price/FX fetching via yfinance
    ticker_detail.py, portfolio_rollup_service.py   Securities detail/rollup computation
    passive_detail.py, passive_rollup_service.py    Passive detail/rollup computation
alembic/              Database migrations
tests/                pytest, run against a real Postgres database (not mocks)
scripts/migrate_data.py   One-off ETL from the original app's Prisma schema
```

The routers are deliberately thin. Every business rule — FIFO consumption
order, what counts as a realized gain, how a recurring deposit's next
occurrence is computed — lives in `services/` and is directly unit-tested
there, independent of any HTTP request/response shape.

## Data model

Six tables:

- **User** — the allowlist. In production there is no self-registration:
  a row must already exist for a Google account to be allowed in (see
  Authentication below).
- **PortfolioTransaction** — one row per BUY/SELL/DIVIDEND/DRIP event.
  Distinct fields per type (`quantity`+`price_per_share` for
  BUY/SELL/DRIP, `cash_amount` for DIVIDEND) rather than one generic
  "amount" field, so dividend cash can never be mistaken for a share
  quantity. `fx_rate_to_chf` is captured at transaction time and never
  recomputed — past valuations stay reproducible even after FX rates move.
- **TickerMetadata** — per-user, per-ticker facts (market, category, native
  currency) confirmed manually, not inferred from any transaction data.
- **PassiveInvestment** — a named account (e.g. "Emergency fund", a
  pension fund) with a type, currency, optional notes, and an optional
  user-reported gain/loss percentage.
- **PassiveTransaction** — the deposit/withdrawal ledger for a
  PassiveInvestment.
- **PassiveRecurringDeposit** — an optional, strictly 1:1 recurring
  schedule attached to a PassiveInvestment (weekly/monthly/yearly, with an
  optional end date).

## Key business logic, in more detail

**FIFO cost basis** (`services/fifo.py`): BUY and DRIP transactions push a
lot onto a queue; SELL consumes the oldest lots first. Each unit sold uses
*that lot's own* historical `fx_rate_to_chf` for its CHF cost basis, never
today's rate — so realized gains reflect the FX rate that was actually in
effect when the shares were bought. DRIP-originated lots are excluded from
realized-gain cost basis calculations by design (see Deviations below for
why this matters less than it sounds).

**Passive ledger** (`services/ledger.py`): cost basis is simply the net of
all deposits minus withdrawals. A separate integrity check rejects any
transaction ordering where the running balance would go negative — this
runs at mutation time, not just at read time, so a bad edit can't corrupt
the ledger silently.

**Recurring deposits** (`services/recurring.py`): the trickiest edge case
is month-end dates — a monthly deposit starting Jan 31 should land on Feb
28, then snap back to Mar 31, not drift to Mar 3 the way naive "add one
month" arithmetic would. Every occurrence date is computed fresh from the
original start date (never by chaining off the previous occurrence), which
is what makes this snap-back-not-drift behavior correct.

**Live pricing** (`services/price_service.py`): yfinance is the data
source (no API key, unofficial Yahoo Finance access). A price or FX fetch
failure for one ticker never fails the whole rollup or falls back to a
stale value — it's recorded as a per-ticker error, and that ticker is
excluded from portfolio totals until the next successful fetch.

## Deviations from the original Next.js app

Two bugs were found and fixed during the port rather than carried over
silently:

- `GET /portfolio/tickers/{ticker}` previously had no auth check and
  didn't scope by user — a cross-tenant data leak. Now requires auth and
  scopes by the authenticated user.
- `PUT`/`DELETE /portfolio/tickers/{ticker}` previously fell through to a
  generic 500 on auth failure instead of 401 like every other route. Now
  standardized — every route goes through the same auth dependency.

Both fixes were essentially free: they fell out of routing every handler
through one shared `get_authenticated_user_id` dependency instead of
duplicating auth checks per-route.

## Authentication model

There is no self-service signup. In production, the `users` table *is* the
allowlist: a request's JWT is verified (HS256, shared secret with the
frontend's NextAuth instance), and the decoded email must match an
existing row, or the request is rejected with 401. Nothing gets
auto-created for an unrecognized email. In `development`/`test`
environments, a fixed `dev@local.test` user is auto-provisioned instead,
so local work never needs real Google OAuth credentials.
