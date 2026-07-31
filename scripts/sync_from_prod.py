"""Copies all rows from a production database down to a local one, for
testing against real data instead of an empty local database. Source and
destination share the exact same schema (unlike the historical Prisma ->
this-app migration), so this is a plain table-by-table copy - no column
renaming, no timezone reinterpretation needed.

Destructive to the DESTINATION only: every table is truncated first (via
CASCADE, so FK ordering doesn't matter for the truncate step) so the
result is an exact mirror of the source, not a merge with whatever was
there before. The source is only ever read from.

Usage:
    SOURCE_DATABASE_URL=<prod connection string> \\
    DATABASE_URL=<local Docker Postgres, e.g. postgresql://portfolio:portfolio-dev-password@localhost:5433/portfolio> \\
        python -m scripts.sync_from_prod [--dry-run]
"""

import os
import sys

from sqlalchemy import create_engine, text

from app.config import get_settings

TABLES_IN_FK_ORDER = [
    "users",
    "portfolio_transactions",
    "ticker_metadata",
    "passive_investments",
    "passive_transactions",
    "passive_recurring_deposits",
]

# Only the 5 tables with an autoincrement integer PK need a sequence reset.
SEQUENCE_TABLES = [t for t in TABLES_IN_FK_ORDER if t != "users"]


def report_counts(source_conn, dest_conn, label: str) -> None:
    print(f"\n--- {label} ---", flush=True)
    for table in TABLES_IN_FK_ORDER:
        source_count = source_conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one()
        dest_count = dest_conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one()
        print(f"  {table:<28} prod={source_count:<6} local={dest_count}", flush=True)


def main() -> None:
    dry_run = "--dry-run" in sys.argv

    source_url = os.environ.get("SOURCE_DATABASE_URL")
    if not source_url:
        raise SystemExit("SOURCE_DATABASE_URL is required (the prod connection string to copy from).")

    dest_url = get_settings().database_url
    if "localhost" not in dest_url and "127.0.0.1" not in dest_url:
        raise SystemExit(
            f"Refusing to run: DATABASE_URL ({dest_url}) doesn't look like a local database. "
            "This script truncates the destination - only ever point it at localhost."
        )

    source_engine = create_engine(source_url)
    dest_engine = create_engine(dest_url)

    with source_engine.connect() as source_conn, dest_engine.begin() as dest_conn:
        report_counts(source_conn, dest_conn, "before")

        if dry_run:
            print("\n[dry-run] no writes performed", flush=True)
            return

        print("\nTruncating destination tables...", flush=True)
        dest_conn.execute(text(f"TRUNCATE {', '.join(TABLES_IN_FK_ORDER)} CASCADE"))

        for table in TABLES_IN_FK_ORDER:
            rows = source_conn.execute(text(f"SELECT * FROM {table}")).mappings().all()
            if not rows:
                print(f"{table}: nothing to copy", flush=True)
                continue
            columns = list(rows[0].keys())
            placeholders = ", ".join(f":{c}" for c in columns)
            insert_sql = text(f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})")
            dest_conn.execute(insert_sql, [dict(row) for row in rows])
            print(f"{table}: copied {len(rows)} row(s)", flush=True)

        for table in SEQUENCE_TABLES:
            dest_conn.execute(
                text(
                    f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), "
                    f"COALESCE((SELECT MAX(id) FROM {table}), 1), true)"
                )
            )

        # Reuses dest_conn (still inside its own open transaction) rather
        # than opening a fresh connection - a new connection would try to
        # read the very tables this transaction has TRUNCATEd and not yet
        # committed, and block waiting for a lock this same process holds,
        # deadlocking against itself.
        report_counts(source_conn, dest_conn, "after")


if __name__ == "__main__":
    main()
