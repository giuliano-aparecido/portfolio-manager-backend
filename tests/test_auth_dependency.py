from datetime import datetime, timedelta, timezone

import jwt
import pytest
from sqlalchemy.orm import Session

from app.config import Settings
from app.dependencies.auth import DEV_USER_EMAIL, get_authenticated_user_id
from app.exceptions import UnauthorizedError
from app.models import User

SECRET = "test-secret-at-least-32-bytes-long-for-hs256"


def make_settings(environment: str) -> Settings:
    return Settings(environment=environment, nextauth_secret=SECRET)


def make_token(email: str, secret: str = SECRET, expires_delta: timedelta = timedelta(hours=1)) -> str:
    payload = {
        "email": email,
        "iat": datetime.now(timezone.utc),
        "exp": datetime.now(timezone.utc) + expires_delta,
    }
    return jwt.encode(payload, secret, algorithm="HS256")


class TestDevAndTestMode:
    def test_auto_provisions_fixed_dev_user_when_missing(self, db_session: Session, monkeypatch) -> None:
        monkeypatch.setattr("app.dependencies.auth.get_settings", lambda: make_settings("development"))

        user_id = get_authenticated_user_id(authorization=None, db=db_session)

        user = db_session.query(User).filter(User.email == DEV_USER_EMAIL).one()
        assert user.id == user_id
        assert user.name == "Dev User"

    def test_reuses_existing_dev_user_on_second_call(self, db_session: Session, monkeypatch) -> None:
        monkeypatch.setattr("app.dependencies.auth.get_settings", lambda: make_settings("test"))

        first_id = get_authenticated_user_id(authorization=None, db=db_session)
        second_id = get_authenticated_user_id(authorization=None, db=db_session)

        assert first_id == second_id
        assert db_session.query(User).filter(User.email == DEV_USER_EMAIL).count() == 1

    def test_ignores_any_authorization_header(self, db_session: Session, monkeypatch) -> None:
        monkeypatch.setattr("app.dependencies.auth.get_settings", lambda: make_settings("development"))

        # Even a garbage/malformed header must not matter in dev/test mode.
        user_id = get_authenticated_user_id(authorization="not a real header", db=db_session)
        assert user_id


class TestProductionMode:
    def test_rejects_missing_authorization_header(self, db_session: Session, monkeypatch) -> None:
        monkeypatch.setattr("app.dependencies.auth.get_settings", lambda: make_settings("production"))
        with pytest.raises(UnauthorizedError):
            get_authenticated_user_id(authorization=None, db=db_session)

    def test_rejects_non_bearer_header(self, db_session: Session, monkeypatch) -> None:
        monkeypatch.setattr("app.dependencies.auth.get_settings", lambda: make_settings("production"))
        with pytest.raises(UnauthorizedError):
            get_authenticated_user_id(authorization="Basic abc123", db=db_session)

    def test_rejects_token_signed_with_wrong_secret(self, db_session: Session, monkeypatch) -> None:
        monkeypatch.setattr("app.dependencies.auth.get_settings", lambda: make_settings("production"))
        token = make_token("someone@example.com", secret="a-completely-different-32-byte-secret")
        with pytest.raises(UnauthorizedError):
            get_authenticated_user_id(authorization=f"Bearer {token}", db=db_session)

    def test_rejects_expired_token(self, db_session: Session, monkeypatch) -> None:
        monkeypatch.setattr("app.dependencies.auth.get_settings", lambda: make_settings("production"))
        token = make_token("someone@example.com", expires_delta=timedelta(hours=-1))
        with pytest.raises(UnauthorizedError):
            get_authenticated_user_id(authorization=f"Bearer {token}", db=db_session)

    def test_rejects_valid_token_with_no_matching_user(self, db_session: Session, monkeypatch) -> None:
        # Valid signature, not expired — but no User row for this email.
        # The User table is the allowlist; nothing gets auto-provisioned.
        monkeypatch.setattr("app.dependencies.auth.get_settings", lambda: make_settings("production"))
        token = make_token("nobody@example.com")
        with pytest.raises(UnauthorizedError):
            get_authenticated_user_id(authorization=f"Bearer {token}", db=db_session)

    def test_accepts_valid_token_for_existing_user(self, db_session: Session, monkeypatch) -> None:
        monkeypatch.setattr("app.dependencies.auth.get_settings", lambda: make_settings("production"))
        existing = User(email="giuliano.aparecido@gmail.com", name="Giuliano")
        db_session.add(existing)
        db_session.flush()

        token = make_token("giuliano.aparecido@gmail.com")
        user_id = get_authenticated_user_id(authorization=f"Bearer {token}", db=db_session)

        assert user_id == existing.id
