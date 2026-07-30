from sqlalchemy.orm import Session

from app.models import User


def test_user_round_trip(db_session: Session) -> None:
    user = User(email="smoke-test@example.com", name="Smoke Test")
    db_session.add(user)
    db_session.flush()

    assert user.id  # uuid4 string assigned
    assert user.created_at is not None
    assert user.updated_at is not None

    fetched = db_session.query(User).filter(User.email == "smoke-test@example.com").one()
    assert fetched.id == user.id
    assert fetched.name == "Smoke Test"
