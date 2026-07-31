# Portfolio Manager — Backend

A Python/FastAPI backend for a multi-currency investment portfolio tracker
(FIFO cost basis, live Yahoo Finance pricing, a manual-gain/loss
passive-investment ledger with recurring deposits), built with SQLAlchemy,
Alembic, and pytest. It serves
[`portfolio-manager-frontend`](https://github.com/GiulianoAparecido/portfolio-manager-frontend).

## Documentation

- [`PROJECT.md`](PROJECT.md) — architecture, data model, business logic
- [`DEVELOPMENT.md`](DEVELOPMENT.md) — full local setup, environment variables, migrations, testing
- [`CONTRIBUTING.md`](CONTRIBUTING.md) — branching, PR expectations, code style

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

Render (backend, prod only) + Vercel (frontend, prod only) + Neon
Postgres. This database is also used by another application, with its own
tables — this app's tables are all snake_case (`users`,
`portfolio_transactions`, ...) specifically so they never collide with
that application's differently-cased table names.

## Auth consistency

Every route goes through the same `get_authenticated_user_id` dependency,
so auth behavior is uniform across the whole API: every route requires
authentication, scopes its query by the authenticated user, and returns a
401 (not a 500 or an unscoped result) on auth failure.
