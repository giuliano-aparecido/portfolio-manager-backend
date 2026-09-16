from datetime import datetime, timezone

from app.services.fifo import ProcessedTransaction
from app.services.mutation_validation import validate_fifo_integrity

SAME_DATE = datetime.fromisoformat("2024-01-01").replace(tzinfo=timezone.utc)


def txn(
    type_: str,
    quantity: float | None = None,
    price_per_share: float | None = None,
    fx_rate_to_chf: float = 1.0,
    ticker: str = "AAPL",
    native_currency: str = "USD",
    id: int | None = None,
    date: datetime = SAME_DATE,
) -> ProcessedTransaction:
    return ProcessedTransaction(
        ticker=ticker,
        date=date,
        type=type_,
        native_currency=native_currency,
        fx_rate_to_chf=fx_rate_to_chf,
        quantity=quantity,
        price_per_share=price_per_share,
        id=id,
    )


def test_same_date_ties_broken_by_ascending_id_regardless_of_input_order() -> None:
    # A BUY (id=10) and a same-day SELL of everything it bought (id=11) —
    # chronologically the BUY must be processed first. Passed in the
    # REVERSE of id order here, simulating an unordered DB fetch that
    # happened to return the SELL row before the BUY row. Before this fix,
    # mutation_validation only sorted by date, so a same-date tie kept
    # whatever order the caller happened to hand it — this would have
    # processed the SELL first and raised an oversell error.
    sell = txn("SELL", quantity=10, price_per_share=120, id=11)
    buy = txn("BUY", quantity=10, price_per_share=100, id=10)

    result = validate_fifo_integrity([sell, buy])

    assert result["valid"] is True


def test_multiple_same_date_sells_still_resolve_by_id_when_input_is_scrambled() -> None:
    # Three same-date rows fed in an order that matches none of (date-only,
    # id, or chronological-intent) ordering: a SELL of the first lot's
    # exact quantity (id=12, must run after the id=10 BUY it depends on),
    # a second BUY (id=11) that's irrelevant to it, handed in a scrambled
    # order. Only sorting by (date, id) makes every SELL see its
    # prerequisite BUY.
    sell_from_first_lot = txn("SELL", quantity=10, price_per_share=120, id=12)
    second_buy = txn("BUY", quantity=5, price_per_share=110, id=11)
    first_buy = txn("BUY", quantity=10, price_per_share=100, id=10)

    result = validate_fifo_integrity([sell_from_first_lot, second_buy, first_buy])

    assert result["valid"] is True


def test_not_yet_persisted_candidate_sorts_after_existing_rows_on_same_date() -> None:
    # The mutation-validation routes append the not-yet-persisted candidate
    # (id=None) to the *end* of the existing list before validating. On a
    # same-date collision, the candidate must be treated as happening after
    # every already-persisted row for that date, not before.
    existing_buy = txn("BUY", quantity=5, price_per_share=100, id=1)
    candidate_sell = txn("SELL", quantity=5, price_per_share=150)  # id=None

    # If the candidate were (wrongly) treated as coming first, this would
    # be an oversell (no shares yet) — it must instead succeed.
    result = validate_fifo_integrity([existing_buy, candidate_sell])
    assert result["valid"] is True
