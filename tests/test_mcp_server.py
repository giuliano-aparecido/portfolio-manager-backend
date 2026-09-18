from datetime import datetime, timedelta, timezone

import jwt
import pytest
from sqlalchemy.orm import Session

from app.config import Settings
from app.dependencies.mcp_auth import JwtTokenVerifier
from app.mcp_server import mcp_server
from app.models import User

SECRET = "test-secret-at-least-32-bytes-long-for-hs256"

EXPECTED_TOOL_NAMES = {
    "get_holdings",
    "get_allocation",
    "get_ticker_detail",
    "get_passive_investments",
    "get_portfolio_fundamentals",
    "get_ticker_fundamentals",
    "get_intrinsic_value",
    "get_ticker_news",
    "compute_whatif",
}


def make_settings(environment: str) -> Settings:
    return Settings(
        environment=environment,
        nextauth_secret=SECRET,
        mcp_issuer_url="http://localhost:8000",
        mcp_resource_server_url="http://localhost:8000/mcp",
    )


def make_token(email: str, secret: str = SECRET, expires_delta: timedelta = timedelta(hours=1)) -> str:
    payload = {"email": email, "iat": datetime.now(timezone.utc), "exp": datetime.now(timezone.utc) + expires_delta}
    return jwt.encode(payload, secret, algorithm="HS256")


@pytest.fixture
def same_connection_session_factory(db_session: Session):
    """A session bound to the same transactional connection as db_session
    (not a fresh connection from the pool), so it sees that fixture's
    uncommitted data — but is safe for the verifier to open/close on its
    own, unlike handing it db_session directly.
    """

    def factory() -> Session:
        return Session(bind=db_session.connection(), join_transaction_mode="create_savepoint")

    return factory


class TestMcpServerToolRegistration:
    async def test_registers_the_expected_tools(self) -> None:
        tools = await mcp_server.list_tools()
        assert {t.name for t in tools} == EXPECTED_TOOL_NAMES

    async def test_every_tool_has_a_description_and_object_schema(self) -> None:
        tools = await mcp_server.list_tools()
        for tool in tools:
            assert tool.description
            assert tool.input_schema["type"] == "object"

    async def test_compute_whatif_schema_excludes_db_and_user_id(self) -> None:
        tools = {t.name: t for t in await mcp_server.list_tools()}
        properties = tools["compute_whatif"].input_schema["properties"]
        assert set(properties) == {"ticker", "action", "quantity", "price_per_share"}


class TestJwtTokenVerifierDevMode:
    async def test_accepts_any_token_and_resolves_the_fixed_dev_user(
        self, same_connection_session_factory, monkeypatch
    ) -> None:
        monkeypatch.setattr("app.dependencies.mcp_auth.get_settings", lambda: make_settings("test"))
        verifier = JwtTokenVerifier(session_factory=same_connection_session_factory)

        access_token = await verifier.verify_token("literally-anything")

        assert access_token is not None
        assert access_token.subject
        assert access_token.scopes == ["read"]


class TestJwtTokenVerifierProductionMode:
    async def test_rejects_token_with_wrong_signature(self, same_connection_session_factory, monkeypatch) -> None:
        monkeypatch.setattr("app.dependencies.mcp_auth.get_settings", lambda: make_settings("production"))
        verifier = JwtTokenVerifier(session_factory=same_connection_session_factory)
        token = make_token("someone@example.com", secret="a-completely-different-32-byte-secret")

        assert await verifier.verify_token(token) is None

    async def test_rejects_valid_token_with_no_matching_user(
        self, same_connection_session_factory, monkeypatch
    ) -> None:
        monkeypatch.setattr("app.dependencies.mcp_auth.get_settings", lambda: make_settings("production"))
        verifier = JwtTokenVerifier(session_factory=same_connection_session_factory)
        token = make_token("nobody@example.com")

        assert await verifier.verify_token(token) is None

    async def test_accepts_valid_token_for_an_allowlisted_user(
        self, db_session: Session, same_connection_session_factory, monkeypatch
    ) -> None:
        monkeypatch.setattr("app.dependencies.mcp_auth.get_settings", lambda: make_settings("production"))
        existing = User(email="allowed-user@example.com", name="Allowed User")
        db_session.add(existing)
        db_session.flush()

        verifier = JwtTokenVerifier(session_factory=same_connection_session_factory)
        token = make_token("allowed-user@example.com")
        access_token = await verifier.verify_token(token)

        assert access_token is not None
        assert access_token.subject == existing.id
