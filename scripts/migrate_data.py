"""One-off migration of rows from the original Prisma-managed tables
(PascalCase, quoted identifiers) into this app's snake_case schema, run once
inside the same Neon project against the same database. Both schemas
coexist there with zero collisions since table names differ.

All IDs (the User cuid string and every integer PK) are preserved verbatim
so FK relationships stay intact. Every timestamp is explicitly reinterpreted
as UTC before being cast into the new `timestamptz` columns (`AT TIME ZONE
'UTC'`) — the old columns are naive `timestamp without time zone` but always
held UTC clock values; without the explicit reinterpretation, Postgres would
otherwise apply the session's local timezone when casting.

Safe to re-run: every insert uses `ON CONFLICT ... DO NOTHING`, so a partial
or repeated run never creates duplicates. Everything happens inside a single
transaction — either the whole migration lands, or none of it does.

Usage:
    DATABASE_URL=<same conn string used for both old and new tables> \\
        python scripts/migrate_data.py [--dry-run]
"""

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

OLD_TABLES = {
    "users": '"User"',
    "portfolio_transactions": '"PortfolioTransaction"',
    "ticker_metadata": '"TickerMetadata"',
    "passive_investments": '"PassiveInvestment"',
    "passive_transactions": '"PassiveTransaction"',
    "passive_recurring_deposits": '"PassiveRecurringDeposit"',
}

INSERT_STATEMENTS = {
    "users": """
        INSERT INTO users (id, email, name, created_at, updated_at)
        SELECT id, email, name,
               ("createdAt" AT TIME ZONE 'UTC'), ("updatedAt" AT TIME ZONE 'UTC')
        FROM "User"
        ON CONFLICT (id) DO NOTHING
    """,
    "portfolio_transactions": """
        INSERT INTO portfolio_transactions
            (id, user_id, ticker, date, type, native_currency, quantity,
             price_per_share, cash_amount, fx_rate_to_chf, notes, created_at)
        SELECT id, "userId", ticker, (date AT TIME ZONE 'UTC'), type, "nativeCurrency",
               quantity, "pricePerShare", "cashAmount", "fxRateToCHF", notes,
               ("createdAt" AT TIME ZONE 'UTC')
        FROM "PortfolioTransaction"
        ON CONFLICT (id) DO NOTHING
    """,
    "ticker_metadata": """
        INSERT INTO ticker_metadata
            (id, user_id, ticker, market, category, native_currency, created_at, updated_at)
        SELECT id, "userId", ticker, market, category, "nativeCurrency",
               ("createdAt" AT TIME ZONE 'UTC'), ("updatedAt" AT TIME ZONE 'UTC')
        FROM "TickerMetadata"
        ON CONFLICT (id) DO NOTHING
    """,
    "passive_investments": """
        INSERT INTO passive_investments
            (id, user_id, name, type, currency, notes, created_at, updated_at,
             gain_loss_pct, gain_loss_updated_at)
        SELECT id, "userId", name, type, currency, notes,
               ("createdAt" AT TIME ZONE 'UTC'), ("updatedAt" AT TIME ZONE 'UTC'),
               "gainLossPct",
               ("gainLossUpdatedAt" AT TIME ZONE 'UTC')
        FROM "PassiveInvestment"
        ON CONFLICT (id) DO NOTHING
    """,
    "passive_transactions": """
        INSERT INTO passive_transactions
            (id, passive_investment_id, type, date, amount_native, notes, created_at)
        SELECT id, "passiveInvestmentId", type, (date AT TIME ZONE 'UTC'),
               "amountNative", notes, ("createdAt" AT TIME ZONE 'UTC')
        FROM "PassiveTransaction"
        ON CONFLICT (id) DO NOTHING
    """,
    "passive_recurring_deposits": """
        INSERT INTO passive_recurring_deposits
            (id, passive_investment_id, amount_native, start_date, frequency,
             end_date, notes, last_generated_date, created_at, updated_at)
        SELECT id, "passiveInvestmentId", "amountNative", ("startDate" AT TIME ZONE 'UTC'),
               frequency,
               ("endDate" AT TIME ZONE 'UTC'),
               notes,
               ("lastGeneratedDate" AT TIME ZONE 'UTC'),
               ("createdAt" AT TIME ZONE 'UTC'), ("updatedAt" AT TIME ZONE 'UTC')
        FROM "PassiveRecurringDeposit"
        ON CONFLICT (id) DO NOTHING
    """,
}

# Only the 5 tables with an autoincrement integer PK need a sequence reset.
SEQUENCE_TABLES = [t for t in TABLES_IN_FK_ORDER if t != "users"]


def report_counts(conn, label: str) -> dict[str, int]:
    counts = {}
    print(f"\n--- {label} ---")
    for new_table in TABLES_IN_FK_ORDER:
        old_table = OLD_TABLES[new_table]
        old_count = conn.execute(text(f"SELECT COUNT(*) FROM {old_table}")).scalar_one()
        new_count = conn.execute(text(f"SELECT COUNT(*) FROM {new_table}")).scalar_one()
        counts[new_table] = new_count
        print(f"  {new_table:<28} old={old_count:<6} new={new_count}")
    return counts


def main() -> None:
    dry_run = "--dry-run" in sys.argv
    engine = create_engine(get_settings().database_url)

    with engine.begin() as conn:
        report_counts(conn, "before")

        if dry_run:
            print("\n[dry-run] no writes performed")
            return

        for table in TABLES_IN_FK_ORDER:
            result = conn.execute(text(INSERT_STATEMENTS[table]))
            print(f"inserted into {table}: {result.rowcount} row(s)")

        for table in SEQUENCE_TABLES:
            conn.execute(
                text(
                    f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), "
                    f"COALESCE((SELECT MAX(id) FROM {table}), 1), true)"
                )
            )
            print(f"reset sequence for {table}")

        report_counts(conn, "after")


if __name__ == "__main__":
    main()
