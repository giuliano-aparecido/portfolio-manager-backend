"""Called by every portfolio-transaction mutation route (create/update/
delete) with the full hypothetical post-mutation transaction set for that
ticker, to reject invariant-breaking edits up front.
"""

from app.services.fifo import ProcessedTransaction, process_ticker


def validate_fifo_integrity(transactions: list[ProcessedTransaction]) -> dict:
    # Same-date ties broken by ascending `id` (a not-yet-persisted
    # candidate, id is None, sorts last) — must match the `ORDER BY date,
    # id` used by every read path that later displays this ticker's FIFO
    # result, per process_ticker's docstring.
    non_dividend = sorted(
        (t for t in transactions if t.type != "DIVIDEND"),
        key=lambda t: (t.date, t.id if t.id is not None else float("inf")),
    )
    try:
        process_ticker(non_dividend)
    except ValueError as exc:
        return {"valid": False, "error": str(exc)}
    return {"valid": True}
