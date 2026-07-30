from pydantic import BaseModel, ConfigDict


def to_camel_chf_aware(snake_str: str) -> str:
    """Standard snake_case -> camelCase, except "chf" is upper-cased as a
    whole word (matching the original TS API's fxRateToCHF, costBasisCHF,
    etc. — a plain camelCase generator would produce "...Chf" instead).
    """
    first, *rest = snake_str.split("_")
    return first + "".join(word.upper() if word == "chf" else word.capitalize() for word in rest)


class CamelModel(BaseModel):
    """Base for every request/response schema — the Python side stays
    snake_case (idiomatic), while the wire format matches the original
    Next.js app's exact camelCase JSON contract byte-for-byte, so the
    frontend needs no endpoint-shape changes.
    """

    model_config = ConfigDict(alias_generator=to_camel_chf_aware, populate_by_name=True, from_attributes=True)
