from fastapi import Request
from fastapi.responses import JSONResponse


class UnauthorizedError(Exception):
    """Raised by the auth dependency when there's no valid session/token.

    Mapped to 401 {"error": "Unauthorized"} — matching the original Next.js
    app's exact response shape, not FastAPI's default {"detail": ...}.
    """


class NotFoundError(Exception):
    def __init__(self, message: str = "Not found") -> None:
        self.message = message
        super().__init__(message)


async def unauthorized_handler(_request: Request, _exc: UnauthorizedError) -> JSONResponse:
    return JSONResponse(status_code=401, content={"error": "Unauthorized"})


async def not_found_handler(_request: Request, exc: NotFoundError) -> JSONResponse:
    return JSONResponse(status_code=404, content={"error": exc.message})
