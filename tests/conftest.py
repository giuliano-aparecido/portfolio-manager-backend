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
    session = Session(bind=connection)
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()
