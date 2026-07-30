# Portfolio Manager — Backend

Python/FastAPI rewrite of the backend for a multi-currency investment
portfolio tracker (FIFO cost basis, live Yahoo Finance pricing, a
manual-gain/loss passive-investment ledger with recurring deposits).

Originally built in Next.js/TypeScript/Prisma — this is a from-scratch port
to Python (SQLAlchemy, Alembic, pytest) as a learning project and the
backend for [`portfolio-manager-frontend`](https://github.com/GiulianoAparecido/portfolio-manager-frontend).
The original Node.js app remains live and unchanged at
[`MyPortfolio`](https://github.com/GiulianoAparecido/MyPortfolio).

## Stack

- **FastAPI** — API framework
- **SQLAlchemy 2.0** + **Alembic** — ORM and migrations
- **Postgres** (Docker locally, Neon in dev/test/prod)
- **pytest** — tests run against a real Postgres database, not mocks
- **yfinance** — live price/FX data
- **PyJWT** — verifies a session token issued by the frontend's NextAuth

## Local development

```bash
docker compose up -d          # Postgres on localhost:5432
cp .env.example .env          # fill in NEXTAUTH_SECRET
pip install -r requirements-dev.txt
uvicorn app.main:app --reload
```

Health check: `GET http://localhost:8000/health`

## Tests

```bash
pytest
```

## Deployment

Render (backend, prod only) + Vercel (frontend, prod only) + the **same
Neon project the original Node app already uses** — no separate project was
provisioned for this rewrite. `alembic upgrade head` creates this app's
snake_case tables (`users`, `portfolio_transactions`, ...) fresh in each
branch; they coexist safely alongside the old app's PascalCase-quoted
Prisma tables (`"User"`, `"PortfolioTransaction"`, ...) in the same
database, since the table names never collide.

`scripts/migrate_data.py` is a one-off script that copies the existing rows
from the old Prisma tables into this app's schema (same database, explicit
column mapping, IDs preserved verbatim, UTC-correct timestamp handling,
one transaction, safe to re-run via `ON CONFLICT DO NOTHING`). Already run
once against prod — verified via row-count parity and a rollup-output
diff against the live Node app's numbers (cost basis, dividends, and
realized gains matched exactly; market value differed only by the live
price movement between the two independent measurements).

## Deviations from the original Next.js app

Found during the port and fixed rather than carried over:
- `GET /portfolio/tickers/{ticker}` previously had no auth check and didn't
  scope by user (cross-tenant data leak). Now requires auth and scopes by
  the authenticated user.
- `PUT`/`DELETE /portfolio/tickers/{ticker}` previously fell through to a
  generic 500 on auth failure instead of 401 like every other route. Now
  standardized.
