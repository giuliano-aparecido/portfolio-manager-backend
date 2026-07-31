"""Called by every portfolio-transaction mutation route (create/update/
delete) with the full hypothetical post-mutation transaction set for that
ticker, to reject invariant-breaking edits up front.
"""

from app.services.fifo import ProcessedTransaction, process_ticker


def validate_fifo_integrity(transactions: list[ProcessedTransaction]) -> dict:
    non_dividend = sorted((t for t in transactions if t.type != "DIVIDEND"), key=lambda t: t.date)
    try:
        process_ticker(non_dividend)
    except ValueError as exc:
        return {"valid": False, "error": str(exc)}
    return {"valid": True}
