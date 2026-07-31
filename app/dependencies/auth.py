import uuid

import jwt
from fastapi import Depends, Header
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.session import get_db
from app.exceptions import UnauthorizedError
from app.models import User

DEV_USER_EMAIL = "dev@local.test"


def get_authenticated_user_id(
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> str:
    """Single auth gate used by every route:

    - development/test: auto-provision/look up a fixed dev@local.test user,
      no real auth check at all.
    - production: the frontend's NextAuth session is a standard HS256 JWT
      (see the frontend's authOptions.jwt.encode/decode override), signed
      with the same NEXTAUTH_SECRET configured here. A valid, non-expired
      signature is not sufficient by itself — the decoded email must match
      an *existing* User row. The User table IS the allowlist; nothing is
      auto-provisioned in production.
    """
    settings = get_settings()

    if settings.environment in ("development", "test"):
        user = db.query(User).filter(User.email == DEV_USER_EMAIL).first()
        if user is None:
            user = User(id=str(uuid.uuid4()), email=DEV_USER_EMAIL, name="Dev User")
            db.add(user)
            db.commit()
            db.refresh(user)
        return user.id

    if not authorization or not authorization.startswith("Bearer "):
        raise UnauthorizedError()

    token = authorization.removeprefix("Bearer ")
    try:
        payload = jwt.decode(token, settings.nextauth_secret, algorithms=["HS256"])
    except jwt.PyJWTError as exc:
        raise UnauthorizedError() from exc

    email = payload.get("email")
    user = db.query(User).filter(User.email == email).first() if email else None
    if user is None:
        raise UnauthorizedError()
    return user.id
