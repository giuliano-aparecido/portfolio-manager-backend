"""MCP-transport counterpart to app/dependencies/auth.py::get_authenticated_user_id
— same HS256/NEXTAUTH_SECRET decode + users-table allowlist check, adapted to
the mcp SDK's TokenVerifier protocol (async verify_token(token) -> AccessToken
| None) instead of a FastAPI dependency.

No self-serve token issuance for MCP clients: a long-lived JWT is manually
minted (signed with NEXTAUTH_SECRET, an allowlisted email) and pasted into
the external client's config. See PROJECT.md for the exact steps — building
real OAuth device-flow/token issuance is an explicit out-of-scope
improvement for this personal, allowlist-gated app.
"""

from collections.abc import Callable

import jwt
from mcp.server.auth.provider import AccessToken, TokenVerifier
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.session import SessionLocal
from app.dependencies.auth import get_or_create_dev_user
from app.models import User


class JwtTokenVerifier(TokenVerifier):
    def __init__(self, session_factory: Callable[[], Session] = SessionLocal) -> None:
        # Injectable for tests, which need a session bound to the same
        # transactional connection as the test's db_session fixture rather
        # than a fresh connection from the engine pool (which wouldn't see
        # that fixture's uncommitted data).
        self._session_factory = session_factory

    async def verify_token(self, token: str) -> AccessToken | None:
        settings = get_settings()
        db = self._session_factory()
        try:
            if settings.environment in ("development", "test"):
                # Matches get_authenticated_user_id's dev bypass: any bearer
                # value is accepted, always resolving to the fixed dev user.
                user = get_or_create_dev_user(db)
                return AccessToken(token=token, client_id=user.id, scopes=["read"], subject=user.id)

            try:
                payload = jwt.decode(token, settings.nextauth_secret, algorithms=["HS256"])
            except jwt.PyJWTError:
                return None

            email = payload.get("email")
            user = db.query(User).filter(User.email == email).first() if email else None
            if user is None:
                return None
            return AccessToken(token=token, client_id=user.id, scopes=["read"], subject=user.id)
        finally:
            db.close()
