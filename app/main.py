from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from slowapi.util import get_remote_address

from app.config import get_settings
from app.exceptions import AppError, app_error_handler, validation_error_handler
from app.routers import (
    auth,
    passive_investments,
    passive_recurring_deposit,
    passive_rollup,
    passive_transactions,
    portfolio_rollup,
    portfolio_tickers,
    portfolio_transactions,
)

# Applies to every route via default_limits, no per-route decorators needed.
# Keyed by client IP - see the Dockerfile's --proxy-headers flag, without
# which every request behind Render's proxy would share one IP and thus one
# bucket. 60/minute comfortably covers real usage (a handful of page loads
# and refreshes per session) while still capping abusive/bot traffic - the
# real risk on a personal, allowlist-gated app is a leaked token spamming
# the yfinance-backed endpoints (Yahoo can rate-limit or block the whole
# outbound IP for that) or bots probing public URLs and burning Render's
# free-tier compute, not deliberate multi-user abuse.
#
# Known residual gap, evaluated and accepted: the Dockerfile's
# --forwarded-allow-ips=* tells uvicorn to trust the left-most entry of an
# inbound X-Forwarded-For header as the client IP, which is exactly the
# entry a client fully controls (a well-behaved proxy appends its own hop
# to the right, it doesn't get to overwrite the left). A leaked-token
# holder can send a fresh X-Forwarded-For per request and get a fresh
# rate-limit bucket every time, bypassing the 60/minute cap entirely.
# Render publishes outbound IP ranges (for third parties allowlisting calls
# this app makes out) but not the inbound edge/proxy range that would be
# needed to scope --forwarded-allow-ips down from "*" - those are two
# different things, and only the outbound one is documented. Given the
# threat model above (a single/allowlisted-user app, not multi-tenant
# abuse resistance), this is accepted rather than guessed at with an IP
# range that could silently stop working or break real client IP
# resolution.
limiter = Limiter(key_func=get_remote_address, default_limits=["60/minute"])


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="Portfolio Manager API")

    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    # Added before CORSMiddleware so CORS ends up as the outer layer (see
    # Starlette's add_middleware/build_middleware_stack: whichever is added
    # last wraps outermost) - otherwise a 429 response would be missing
    # CORS headers, and the browser would surface it as an opaque network
    # error instead of a readable 429.
    app.add_middleware(SlowAPIMiddleware)

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

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    return app


app = create_app()
