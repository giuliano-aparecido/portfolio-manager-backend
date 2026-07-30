from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.exceptions import AppError, app_error_handler, validation_error_handler
from app.routers import (
    passive_investments,
    passive_recurring_deposit,
    passive_rollup,
    passive_transactions,
    portfolio_rollup,
    portfolio_tickers,
    portfolio_transactions,
)


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="Portfolio Manager API")

    app.add_middleware(
        CORSMiddleware,
        allow_origins=[settings.frontend_origin],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["Authorization", "Content-Type"],
    )

    app.add_exception_handler(AppError, app_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)

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
