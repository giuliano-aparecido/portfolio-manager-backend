"""McpRateLimitMiddleware is tested against a minimal dummy Starlette app
rather than the real mounted MCP server, to avoid needing that server's
session_manager lifespan running (see app/main.py's lifespan) just to
prove a rate limit — the middleware itself only inspects scope["path"],
so a trivial route in its place is a faithful, much simpler test double.
"""

from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from app.rate_limiter import McpRateLimitMiddleware


async def _ok(request):
    return PlainTextResponse("ok")


def _build_app(path: str) -> Starlette:
    app = Starlette(routes=[Route(path, _ok, methods=["GET"])])
    app.add_middleware(McpRateLimitMiddleware)
    return app


class TestMcpRateLimitMiddleware:
    def test_allows_requests_under_the_limit(self) -> None:
        client = TestClient(_build_app("/mcp/anything"))
        for _ in range(20):
            assert client.get("/mcp/anything").status_code == 200

    def test_returns_429_after_exceeding_20_per_minute(self) -> None:
        client = TestClient(_build_app("/mcp/anything"))
        for _ in range(20):
            assert client.get("/mcp/anything").status_code == 200

        response = client.get("/mcp/anything")
        assert response.status_code == 429
        assert response.json()["error"].startswith("Rate limit exceeded")

    def test_does_not_apply_to_non_mcp_paths(self) -> None:
        client = TestClient(_build_app("/other"))
        for _ in range(25):
            assert client.get("/other").status_code == 200
