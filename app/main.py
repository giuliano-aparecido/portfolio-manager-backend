from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.exceptions import NotFoundError, UnauthorizedError, not_found_handler, unauthorized_handler


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

    app.add_exception_handler(UnauthorizedError, unauthorized_handler)
    app.add_exception_handler(NotFoundError, not_found_handler)

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    return app


app = create_app()
