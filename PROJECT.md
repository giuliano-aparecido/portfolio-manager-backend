# Project Overview

## What this is

A REST API for tracking a multi-currency personal investment portfolio,
with CHF as the base currency, serving
[`portfolio-manager-frontend`](https://github.com/GiulianoAparecido/portfolio-manager-frontend).

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

Monetary fields throughout (`quantity`, `price_per_share`, `cash_amount`,
`fx_rate_to_chf`, `amount_native`, `gain_loss_pct`) are `Float`
(double-precision), not `Numeric`/`Decimal`. This is a known tradeoff for
financial data — deliberately accepted here rather than overlooked: the
FIFO and ledger code already treat exact equality as unsafe and compare
against a `TOLERANCE` epsilon everywhere it matters (`services/fifo.py`),
so float drift is handled defensively rather than assumed away. `Decimal`
would be the more conventional choice for a production financial system.

## Key business logic, in more detail

**FIFO cost basis** (`services/fifo.py`): BUY and DRIP transactions push a
lot onto a queue; SELL consumes the oldest lots first. Each unit sold uses
*that lot's own* historical `fx_rate_to_chf` for its CHF cost basis, never
today's rate — so realized gains reflect the FX rate that was actually in
effect when the shares were bought. A DRIP reinvestment is modeled as its
own BUY at the reinvestment price, so its cost is included in both
realized-gain cost basis (when the DRIP lot is later sold) and in the
current/unrealized cost basis (while it's still held) — the same
treatment as any other BUY lot, deliberately, so `average_cost_per_share`
reflects the true cost of every share actually held.

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

## Auth consistency

Every route goes through the same `get_authenticated_user_id` dependency
rather than duplicating an auth check per-route, which keeps behavior
uniform across the whole API:

- Every route requires authentication and scopes its query by the
  authenticated user — there's no route that reads or writes data without
  checking whose it is.
- Auth failure always returns 401, consistently, rather than some routes
  401-ing and others falling through to a generic 500.

## Authentication model

There is no self-service signup. In production, the `users` table *is* the
allowlist: a request's JWT is verified (HS256, shared secret with the
frontend's NextAuth instance), and the decoded email must match an
existing row, or the request is rejected with 401. Nothing gets
auto-created for an unrecognized email. In `development`/`test`
environments, a fixed `dev@local.test` user is auto-provisioned instead,
so local work never needs real Google OAuth credentials.

## Rate limiting

`app/main.py` wires a global `slowapi` `Limiter` (60 requests/minute per
client IP, in-memory storage, applied to every route via `default_limits`
rather than per-route decorators). On an app this small — an allowlist of
one or two real users — the actual risk isn't coordinated multi-user
abuse; it's a leaked token being used to hammer the yfinance-backed
endpoints (which risks Yahoo Finance rate-limiting or blocking the whole
outbound IP) or generic bot traffic probing public URLs and burning
Render's free-tier compute. `SlowAPIMiddleware` is registered *before*
`CORSMiddleware` so CORS ends up wrapping it as the outer layer — Starlette
builds its middleware stack so whichever is added last wraps outermost —
otherwise a 429 response would be missing CORS headers and a browser would
report it as an opaque network error rather than a readable 429.

Known accepted gap: `--forwarded-allow-ips=*` (Dockerfile) means the
rate limiter's per-IP key trusts a client-supplied `X-Forwarded-For`
value, letting a leaked-token holder dodge the 60/minute cap — a
documented, evaluated tradeoff, not an oversight. Full rationale is in
the comment above `limiter` in `app/rate_limiter.py`.

`POST /agent/ask` overrides this to a tighter 6/minute (see below) —
LLM calls are slow and cost money, so a leaked token should be capped
harder there than on the rest of the API. The mounted `/mcp` endpoint
(see below) needed a separate fix: `SlowAPIMiddleware` runs for those
requests too, but slowapi's own route-based limiting looks up the
matched route's endpoint to pick a limit, and a Starlette `Mount` has no
`.endpoint` — so it was silently exempt. `McpRateLimitMiddleware`
(`app/rate_limiter.py`) closes that gap with a direct 20/minute check
against the same shared limiter/storage, since `/mcp` exposes the same
yfinance-backed tools as everything else here.

## Portfolio assistant agent + MCP server

`POST /agent/ask` is a chat endpoint: an LLM (Claude, via
`app/services/llm/claude_provider.py`) reasons over the user's question and
decides which read-only tools to call — it never computes a financial
figure itself, only the app's existing deterministic services do
(FIFO, live pricing, rollups). The response streams back as
Server-Sent Events (`token`/`tool_call`/`tool_result`/`done`/`error`
frames); the frontend resends the whole conversation each turn, so nothing
is persisted server-side.

The tool implementations live once, in `app/services/agent_tools.py`, and
are registered onto a single `MCPServer` instance
(`app/mcp_server.py`) — a real [MCP](https://modelcontextprotocol.io)
server, mounted at `/mcp` (`app.mount(...)` in `main.py`). Starlette
middleware wraps the whole app including this mount, so CORS still
applies — what actually doesn't reach `/mcp` is SlowAPI's per-route
limiting (it looks up a matched route's endpoint for a decorated limit,
and a Mount has none, so it silently no-ops there); `McpRateLimitMiddleware`
closes that gap with its own direct check (see "Rate limiting" above).
Auth is genuinely separate: the mount has its own gate, not
`get_authenticated_user_id`, since MCP clients aren't browser-hosted.
Two things dispatch tool calls against that one registry:

- **External MCP clients** (Claude Desktop, etc.) connect directly over
  Streamable HTTP and authenticate via a bearer JWT, verified by
  `app/dependencies/mcp_auth.py::JwtTokenVerifier` — the same
  HS256/`NEXTAUTH_SECRET` + users-table-allowlist check as
  `get_authenticated_user_id`, adapted to the `mcp` SDK's `TokenVerifier`
  protocol. There's no self-service token issuance: a long-lived JWT is
  manually minted and pasted into the external client's config. Building
  real OAuth device-flow/token issuance is a deliberate future
  improvement, not an oversight, for an app with an allowlist of one or
  two real users.
- **`/agent/ask`'s own loop** dispatches in-process, via
  `mcp_server.list_tools()` / `mcp_server.call_tool(...)` (plain method
  calls, not an HTTP request to its own `/mcp` mount) — this avoids a
  same-process double round-trip and a self-auth problem the loopback
  would otherwise need (minting the process a bearer token for its own
  MCP server). The externally-connectable MCP server is what demonstrates
  the protocol; the internal loop doesn't need to also speak MCP to
  itself to get the same tool-registry benefit.

Since MCP tool functions don't go through FastAPI's dependency system,
each one opens its own short-lived DB session and resolves the caller's
identity via `app/services/agent_context.py::resolve_user_id()` — which
checks the MCP SDK's own `get_access_token()` (set for real MCP-client
calls) and falls back to a contextvar the `/agent/ask` loop sets from its
own `get_authenticated_user_id` result (real MCP auth never runs for that
path).

One tool is worth calling out because it breaks the pattern of every
other one here: `get_ticker_news` returns third-party *reporting* rather
than a figure this app computed. Its filters (`app/services/news.py`) are
a deliberate copy of `financial-sentiment-api`'s — the same
"ported, not imported" convention already used for the DCF math — because
raw Google News results for a single ticker are mostly auto-generated
13F-filing spam and "here's why the stock moved" filler. Nothing that
fails those filters is ever returned; instead the search window widens
(a week, then a month, a quarter, a year) until something meaningful
turns up, and the window actually used comes back in the response so the
agent can say how old the news is rather than implying it's fresh.

`LLMProvider` (`app/services/llm/base.py`) is a provider-agnostic seam:
conversation history is a list of neutral `Turn` objects (`UserTurn` /
`AssistantTurn` / `ToolResultsTurn`), plain JSON-Schema tool defs, and a
system-prompt string. `GeminiProvider` and `ClaudeProvider` each translate
that neutral history into their own wire shape at call time — Gemini's
`Content`/`Part` objects with `function_call`/`function_response` parts,
Claude's content blocks with `tool_use`/`tool_result` — since the two
APIs structure a tool-calling turn differently enough that a shared dict
shape would just be one provider's shape with the other translating out
of it. `AGENT_PROVIDER` (`gemini` by default, or `claude`) selects which
one `app/routers/agent.py::get_llm_provider()` returns; switching is
config, not code.
