from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health_returns_ok() -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_health_accepts_head() -> None:
    # Uptime monitors (e.g. UptimeRobot's default HTTP(s) check) send HEAD,
    # not GET - a GET-only route 405s on that and reads as "down".
    response = client.head("/health")
    assert response.status_code == 200
