from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from app.config import get_settings
from app.exceptions import AppError, app_error_handler, validation_error_handler
from app.mcp_server import mcp_server
from app.rate_limiter import McpRateLimitMiddleware, limiter
from app.routers import (
    agent,
    auth,
    passive_investments,
    passive_recurring_deposit,
    passive_rollup,
    passive_transactions,
    portfolio_rollup,
    portfolio_tickers,
    portfolio_transactions,
)

__all__ = ["app", "create_app", "limiter"]


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Required for the mounted MCP app (see app.mount("/mcp", ...) below) —
    # without an active session_manager, mounted requests fail.
    async with mcp_server.session_manager.run():
        yield


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="Portfolio Manager API", lifespan=lifespan)

    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    # Added before CORSMiddleware so CORS ends up as the outer layer (see
    # Starlette's add_middleware/build_middleware_stack: whichever is added
    # last wraps outermost) - otherwise a 429 response would be missing
    # CORS headers, and the browser would surface it as an opaque network
    # error instead of a readable 429.
    app.add_middleware(SlowAPIMiddleware)
    # slowapi's own route-based limiting exempts /mcp (a Mount has no
    # .endpoint for it to look up) — see McpRateLimitMiddleware's own
    # docstring for the full explanation.
    app.add_middleware(McpRateLimitMiddleware)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=[settings.frontend_origin],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["Authorization", "Content-Type"],
    )

    app.add_exception_handler(AppError, app_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)

    app.include_router(auth.router)
    app.include_router(portfolio_tickers.router)
    app.include_router(portfolio_transactions.router)
    app.include_router(portfolio_rollup.router)
    app.include_router(passive_investments.router)
    app.include_router(passive_transactions.router)
    app.include_router(passive_recurring_deposit.router)
    app.include_router(passive_rollup.router)
    app.include_router(agent.router)

    # Starlette middleware wraps the whole app including mounted sub-apps —
    # CORSMiddleware genuinely runs for /mcp requests too (verified), it
    # isn't bypassed. What actually doesn't apply is SlowAPI's per-route
    # limiting specifically: it looks up the matched route's endpoint to
    # find a decorated limit, and a Mount has none, so its check silently
    # no-ops for /mcp (see McpRateLimitMiddleware, which closes that one
    # gap directly). Auth is genuinely separate here too — /mcp runs its
    # own JwtTokenVerifier instead of get_authenticated_user_id — see
    # app/mcp_server.py. Fine since MCP clients aren't browser-hosted
    # anyway, not an oversight.
    app.mount("/mcp", mcp_server.streamable_http_app(streamable_http_path="/"))

    # methods=["GET", "HEAD"] - @app.get() alone 405s on HEAD, which is
    # what uptime monitors (e.g. UptimeRobot's default HTTP(s) check) send
    # by default, causing false "down" alerts against a perfectly healthy
    # service.
    @app.api_route("/health", methods=["GET", "HEAD"])
    def health() -> dict:
        return {"status": "ok"}

    return app


app = create_app()
