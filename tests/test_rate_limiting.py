from fastapi.testclient import TestClient

from app.main import limiter


class TestRateLimiting:
    def test_returns_429_after_exceeding_the_default_limit(self, client: TestClient) -> None:
        # /health needs no auth, so this isolates the rate limiter itself
        # rather than anything auth-related. default_limits is "60/minute".
        for _ in range(60):
            response = client.get("/health")
            assert response.status_code == 200

        response = client.get("/health")
        assert response.status_code == 429
        assert response.json()["error"].startswith("Rate limit exceeded")

    def test_limit_is_tracked_separately_per_route(self, client: TestClient) -> None:
        for _ in range(60):
            assert client.get("/health").status_code == 200

        # /health is now exhausted, but a different route has its own bucket.
        response = client.get("/auth/whoami")
        assert response.status_code != 429

    def test_reset_clears_the_counter(self, client: TestClient) -> None:
        for _ in range(60):
            assert client.get("/health").status_code == 200
        assert client.get("/health").status_code == 429

        limiter.reset()

        assert client.get("/health").status_code == 200
