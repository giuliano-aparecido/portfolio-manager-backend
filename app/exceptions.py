from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


class AppError(Exception):
    """Generic {"error": "..."} + status code — every route returns this
    exact JSON shape on failure, not FastAPI's default {"detail": ...}.
    """

    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        self.message = message
        super().__init__(message)


class UnauthorizedError(AppError):
    def __init__(self) -> None:
        super().__init__(401, "Unauthorized")


class NotFoundError(AppError):
    def __init__(self, message: str = "Not found") -> None:
        super().__init__(404, message)


async def app_error_handler(_request: Request, exc: AppError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={"error": exc.message})


async def validation_error_handler(_request: Request, exc: RequestValidationError) -> JSONResponse:
    """Safety net for malformed request bodies that fall outside the
    documented, hand-validated error cases (e.g. a field that's a JSON
    object where a number was expected) — keeps the {"error": ...} envelope
    consistent instead of leaking FastAPI's default {"detail": [...]}
    shape, even though the message text is generic rather than a specific
    hand-written error string in these edge cases.
    """
    first = exc.errors()[0]
    field = ".".join(str(p) for p in first["loc"] if p != "body")
    return JSONResponse(status_code=400, content={"error": f"Invalid value for {field or 'request body'}"})
