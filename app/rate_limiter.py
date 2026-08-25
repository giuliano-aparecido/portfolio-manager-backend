"""Extracted from main.py so routers (e.g. agent.py) can apply per-route
overrides without importing back from main.py, which imports every router
and would create a circular import.
"""

import limits
from slowapi import Limiter
from slowapi.util import get_remote_address
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

# Applies to every route via default_limits, no per-route decorators needed
# except where a route overrides it (see agent.py's tighter 6/minute limit).
# Keyed by client IP - see the Dockerfile's --proxy-headers flag, without
# which every request behind Render's proxy would share one IP and thus one
# bucket. 60/minute comfortably covers real usage (a handful of page loads
# and refreshes per session) while still capping abusive/bot traffic - the
# real risk on a personal, allowlist-gated app is a leaked token spamming
# the yfinance-backed endpoints (Yahoo can rate-limit or block the whole
# outbound IP for that) or bots probing public URLs and burning Render's
# free-tier compute, not deliberate multi-user abuse.
limiter = Limiter(key_func=get_remote_address, default_limits=["60/minute"])

MCP_RATE_LIMIT = limits.parse("20/minute")


class McpRateLimitMiddleware:
    """slowapi's automatic per-route limiting never applies to /mcp: it
    looks up the matched route's endpoint to decide which limit(s) apply,
    and a Starlette Mount (main.py's app.mount("/mcp", ...)) has no
    .endpoint attribute, so slowapi's own route lookup treats it as
    unconditionally exempt — verified against slowapi's internals. This
    closes that gap with a direct check against the same shared
    limiter/storage the rest of the app uses (so app/rate_limiter.py's own
    `limiter.reset()` — already called by tests/conftest.py's autouse
    fixture — resets this too), rather than relying on slowapi's
    route-based machinery at all.

    A plain ASGI middleware class (not Starlette's BaseHTTPMiddleware),
    since this only needs to inspect the path and short-circuit with a 429
    — no need to buffer/replay the request body the way BaseHTTPMiddleware
    would.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        is_mcp_path = scope["path"] == "/mcp" or scope["path"].startswith("/mcp/")
        if scope["type"] != "http" or not is_mcp_path:
            await self.app(scope, receive, send)
            return

        key = get_remote_address(Request(scope))
        if not limiter.limiter.hit(MCP_RATE_LIMIT, key):
            # No Retry-After/X-RateLimit-* headers, unlike slowapi's own 429
            # path (_rate_limit_exceeded_handler) — a deliberate simplification
            # since MCP clients aren't expected to inspect them, not an
            # oversight.
            response = JSONResponse(status_code=429, content={"error": f"Rate limit exceeded: {MCP_RATE_LIMIT}"})
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)
