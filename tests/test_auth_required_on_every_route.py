"""Walks every registered route in the app and asserts it requires
authentication — rather than hand-writing a "returns 401" test per route,
which only ever catches a regression on routes someone remembered to test.
This is built by introspecting `app.routes` at collection time, so a new
router/endpoint added later is automatically covered with no extra test
code needed.

Two independent checks per route:
- structural: `get_authenticated_user_id` appears somewhere in the route's
  resolved dependency tree (catches "forgot to add Depends(...)" even if,
  say, an unrelated bug happened to also return 401).
- functional: an actual request with the auth dependency forced to fail
  (via unauthenticated_client, see test_routes_portfolio_tickers.py) gets a
  401, not a 404/422/500 from some other code path swallowing the failure.

/health is the one deliberate exception — a liveness probe with no user
data, correctly unauthenticated.
"""

import re

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from app.dependencies.auth import get_authenticated_user_id
from app.exceptions import UnauthorizedError
from app.main import app

EXCLUDED_PATHS = {"/health"}


@pytest.fixture
def unauthenticated_client(client: TestClient):
    def _raise_unauthorized():
        raise UnauthorizedError()

    app.dependency_overrides[get_authenticated_user_id] = _raise_unauthorized
    try:
        yield client
    finally:
        app.dependency_overrides.pop(get_authenticated_user_id, None)


def _concrete_path(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "placeholder-id", path)


def _uses_auth_dependency(route: APIRoute) -> bool:
    def search(dependant) -> bool:
        if dependant.call is get_authenticated_user_id:
            return True
        return any(search(sub) for sub in dependant.dependencies)

    return search(route.dependant)


def _flatten_routes(routes) -> list[APIRoute]:
    """Recursively unwraps whatever `app.routes`/`router.routes` contains
    down to actual APIRoute leaves. Needed because included routers don't
    show up as plain APIRoute objects directly on `app.routes` on this
    FastAPI version - they're wrapped in a private `_IncludedRouter` that
    exposes the real routes via `.original_router.routes` instead. Handled
    defensively (checking for either shape) so this keeps working whether
    that wrapping exists or not.
    """
    flattened: list[APIRoute] = []
    for route in routes:
        if isinstance(route, APIRoute):
            flattened.append(route)
        elif getattr(route, "original_router", None) is not None:
            flattened.extend(_flatten_routes(route.original_router.routes))
        elif hasattr(route, "routes"):
            flattened.extend(_flatten_routes(route.routes))
    return flattened


def _all_data_routes() -> list[tuple[str, str, APIRoute]]:
    routes: list[tuple[str, str, APIRoute]] = []
    for route in _flatten_routes(app.routes):
        if route.path in EXCLUDED_PATHS:
            continue
        for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
            routes.append((method, route.path, route))
    return routes


_ROUTES = _all_data_routes()
_ROUTE_IDS = [f"{method} {path}" for method, path, _ in _ROUTES]

# Sanity check on the test itself: if router registration ever breaks (e.g.
# an import error swallowed at app-build time) and _all_data_routes() comes
# back empty or suspiciously small, the parametrized tests below would just
# silently pass with zero cases instead of failing loudly.
assert len(_ROUTES) >= 20, f"expected at least 20 authenticated routes, found {len(_ROUTES)} — route discovery may be broken"


class TestEveryRouteHasAuthDependency:
    @pytest.mark.parametrize("method,path,route", _ROUTES, ids=_ROUTE_IDS)
    def test_route_depends_on_get_authenticated_user_id(self, method: str, path: str, route: APIRoute) -> None:
        assert _uses_auth_dependency(route), (
            f"{method} {path} does not depend on get_authenticated_user_id — "
            "every route except /health must require authentication"
        )


class TestEveryRouteReturns401WithoutAuth:
    @pytest.mark.parametrize("method,path,route", _ROUTES, ids=_ROUTE_IDS)
    def test_route_returns_401_without_authentication(
        self, method: str, path: str, route: APIRoute, unauthenticated_client: TestClient
    ) -> None:
        concrete_path = _concrete_path(path)
        kwargs = {"json": {}} if method in ("POST", "PUT") else {}
        response = unauthenticated_client.request(method, concrete_path, **kwargs)
        assert response.status_code == 401, (
            f"{method} {path} returned {response.status_code} instead of 401 when unauthenticated "
            f"(body: {response.text[:200]})"
        )
