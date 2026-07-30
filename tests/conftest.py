from collections.abc import Generator

import pytest
from sqlalchemy.orm import Session

from app.db.session import engine


@pytest.fixture
def db_session() -> Generator[Session, None, None]:
    """Real Postgres session per test, joined to an outer transaction that's
    rolled back on teardown — no SQLite, no mocking of the DB layer, but
    tests still leave no trace in the database.
    """
    connection = engine.connect()
    transaction = connection.begin()
    # join_transaction_mode="create_savepoint": code under test may call
    # session.commit() itself (e.g. the auth dependency's dev-mode
    # auto-provision) — this makes commit() only release a SAVEPOINT
    # instead of ending our outer transaction, so the rollback below still
    # discards everything.
    session = Session(bind=connection, join_transaction_mode="create_savepoint")
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()
