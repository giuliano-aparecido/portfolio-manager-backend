import pytest
from fastapi.testclient import TestClient

from app.dependencies.auth import get_authenticated_user_id
from app.exceptions import UnauthorizedError
from app.main import app
from app.models import User


def _raise_unauthorized():
    raise UnauthorizedError()


@pytest.fixture
def unauthenticated_client(client: TestClient):
    """Simulates an unauthenticated/unauthorized production request —
    overrides the auth dependency to raise UnauthorizedError directly,
    rather than relying on dev/test mode's real auto-provision behavior.
    """
    app.dependency_overrides[get_authenticated_user_id] = _raise_unauthorized
    try:
        yield client
    finally:
        app.dependency_overrides.pop(get_authenticated_user_id, None)


class TestAuthWhoamiRoute:
    def test_returns_200_with_user_id_when_authorized(self, authed_client: TestClient, test_user: User) -> None:
        response = authed_client.get("/auth/whoami")
        assert response.status_code == 200
        assert response.json() == {"userId": test_user.id}

    def test_returns_401_when_not_authorized(self, unauthenticated_client: TestClient) -> None:
        response = unauthenticated_client.get("/auth/whoami")
        assert response.status_code == 401
        assert response.json() == {"error": "Unauthorized"}
