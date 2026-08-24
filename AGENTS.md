# Project docs

Full documentation lives in dedicated files, not duplicated here:

- [`README.md`](README.md) — stack, local setup, tests, deployment
- [`PROJECT.md`](PROJECT.md) — architecture, data model, FIFO/ledger/
  recurring-deposit business logic, auth model, rate limiting
- [`DEVELOPMENT.md`](DEVELOPMENT.md) — environment variables, migrations,
  day-to-day commands
- [`CONTRIBUTING.md`](CONTRIBUTING.md) — **branch + PR is required here,
  never push directly to `main`** — see there for the exact workflow,
  the pre-PR test/migration gate, and production-database safety rules

Serves [`portfolio-manager-frontend`](https://github.com/GiulianoAparecido/portfolio-manager-frontend)
(also documented, same conventions).

Fleet-wide conventions shared with this repo's siblings live in
[`agent-config/AGENTS.md`](agent-config/AGENTS.md) (a git submodule),
loaded automatically below for Claude Code.

@agent-config/AGENTS.md
