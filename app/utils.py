from app.exceptions import AppError


def parse_int_id(value: str, message: str = "invalid id") -> int:
    """Parses a numeric path param, raising AppError(400, message) on failure."""
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
