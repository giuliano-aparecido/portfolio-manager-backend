from app.exceptions import AppError


def parse_int_id(value: str, message: str = "invalid id") -> int:
    """Shared `int(path_param)` parsing for router path params - every route
    taking a numeric id in the path needs the same "not a valid int -> 400"
    handling, previously duplicated per-router with slightly different
    messages (preserved here via `message` rather than unified, since at
    least one test asserts on the exact existing string)."""
    try:
        return int(value)
    except ValueError:
        raise AppError(400, message) from None


def to_number(value: object) -> float | None:
    """Mirrors JS's Number(x) for validation purposes: returns None (the
    NaN equivalent) instead of raising, so callers can uniformly write
    `n = to_number(x); if n is None or n <= 0: ...`.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def parse_date(value: object) -> "datetime | None":
    from datetime import datetime, timezone

    if not value:
        return None
    try:
        text = str(value).strip()
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed
