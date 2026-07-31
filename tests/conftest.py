from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.db.session import engine, get_db
from app.dependencies.auth import get_authenticated_user_id
from app.main import app
from app.models import User
from app.services.price_service import clear_quote_cache


@pytest.fixture(autouse=True)
def _clear_quote_cache() -> Generator[None, None, None]:
    # price_service caches quotes across calls (see its module docstring).
    # Different tests often reuse the same ticker/currency with different
    # mocked values, so the cache must not leak between tests.
    clear_quote_cache()
    yield
    clear_quote_cache()


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


@pytest.fixture
def client(db_session: Session) -> Generator[TestClient, None, None]:
    """A TestClient whose get_db dependency is overridden to use the same
    transaction-rollback-isolated session as db_session, so route tests can
    both call the API and inspect the DB directly in the same test.
    """

    def override_get_db() -> Generator[Session, None, None]:
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.fixture
def test_user(db_session: Session) -> User:
    user = User(email="route-test@example.com", name="Route Test")
    db_session.add(user)
    db_session.flush()
    return user


@pytest.fixture
def authed_client(client: TestClient, test_user: User) -> Generator[TestClient, None, None]:
    """A client fixture with auth pre-overridden to a fixed test user —
    for route tests that aren't specifically exercising the auth
    dependency itself (that's covered directly in test_auth_dependency.py).
    """
    app.dependency_overrides[get_authenticated_user_id] = lambda: test_user.id
    try:
        yield client
    finally:
        app.dependency_overrides.pop(get_authenticated_user_id, None)
