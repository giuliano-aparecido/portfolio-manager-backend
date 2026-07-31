# Contributing

This is a personal project, but the workflow below applies to any change,
human or AI-assisted.

## Branching

- Never commit directly to `main`. Every change goes on a feature branch
  cut from an up-to-date `main`.
- Branch prefixes: `feature/`, `fix/`, `docs/`, `refactor/`, `test/`.
- Only the repository owner pushes to `main` — that happens by merging a
  reviewed pull request, not by pushing directly.

```bash
git checkout main
git pull origin main
git checkout -b fix/short-description
```

## Before opening a pull request

```bash
pytest              # full test suite must pass
alembic upgrade head # if you changed a model, confirm the migration applies cleanly
```

If the change touches a live price/FX code path, do a manual smoke test
against real `yfinance` calls too — the automated tests mock that layer,
so they can't catch a real API shape change on their own.

## What a good PR description covers

- What changed and why (the "why" matters more than the "what" — the diff
  already shows what changed).
- Any deliberate deviation from the original Next.js app's behavior (see
  `PROJECT.md`'s "Deviations" section for the existing examples) — call
  these out explicitly rather than letting them hide in a diff.
- How it was verified: which tests cover it, and whether anything needed
  manual checking (e.g. a live API call, a database migration applied
  against a real environment).

## Database safety

- `scripts/migrate_data.py` is a one-off ETL script, not a repeatable
  sync tool — it's meant to run once per environment it targets. It's
  written to be safe to re-run (`ON CONFLICT DO NOTHING`, wrapped in a
  single transaction), but it is not designed for ongoing use.
- Never point a migration or a destructive script at a production
  `DATABASE_URL` without a recent backup and a clear understanding of
  what it will change. Prefer a dry run (where the script supports one)
  before a real run.

## Code style

- Business logic lives in `app/services/`, framework-agnostic and
  directly unit-tested. Routers stay thin: parse, call a service, map the
  result to a response. See `DEVELOPMENT.md` for more on this split.
- Don't add speculative abstractions, config flags, or error handling for
  cases that can't occur given how a function is actually called
  internally. Validate at actual boundaries (an incoming HTTP request, an
  external API response) — trust your own internal call graph.
