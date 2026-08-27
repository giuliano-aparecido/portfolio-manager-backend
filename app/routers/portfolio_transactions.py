from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.dependencies.auth import get_authenticated_user_id
from app.exceptions import AppError, NotFoundError
from app.models import PortfolioTransaction, TickerMetadata
from app.schemas.portfolio import TransactionCreateRequest, TransactionOut, TransactionUpdateRequest
from app.services.fifo import ProcessedTransaction
from app.services.locking import lock_portfolio_ticker
from app.services.mappers import portfolio_transaction_to_processed
from app.services.mutation_validation import validate_fifo_integrity
from app.services.price_service import fetch_historical_fx_rate
from app.services.validation import TRANSACTION_TYPES, is_valid_transaction_type
from app.utils import parse_date, to_number

router = APIRouter(prefix="/portfolio/transactions", tags=["portfolio-transactions"])


def _parse_and_validate_transaction(body) -> dict:
    txn_type = (body.type or "").strip().upper()
    if not is_valid_transaction_type(txn_type):
        raise AppError(400, f"type must be one of: {', '.join(TRANSACTION_TYPES)}")
    date = parse_date((body.date or "").strip())
    if date is None:
        raise AppError(400, "date is invalid")
    notes = (body.notes or "").strip() or None

    quantity: float | None = None
    price_per_share: float | None = None
    cash_amount: float | None = None
    if txn_type in ("BUY", "SELL", "DRIP"):
        quantity = to_number(body.quantity)
        if quantity is None or quantity <= 0:
            raise AppError(400, "quantity must be a positive number")
        price_per_share = to_number(body.price_per_share)
        if price_per_share is None or price_per_share <= 0:
            raise AppError(400, "pricePerShare must be a positive number")
    else:  # DIVIDEND
        cash_amount = to_number(body.cash_amount)
        if cash_amount is None or cash_amount <= 0:
            raise AppError(400, "cashAmount must be a positive number")

    return {
        "txn_type": txn_type,
        "date": date,
        "notes": notes,
        "quantity": quantity,
        "price_per_share": price_per_share,
        "cash_amount": cash_amount,
    }


@router.post("", response_model=TransactionOut, status_code=201)
def create_transaction(
    body: TransactionCreateRequest,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_authenticated_user_id),
) -> PortfolioTransaction:
    ticker = (body.ticker or "").strip().upper()
    if not ticker:
        raise AppError(400, "ticker is required")

    parsed = _parse_and_validate_transaction(body)
    txn_type = parsed["txn_type"]
    date = parsed["date"]
    notes = parsed["notes"]
    quantity = parsed["quantity"]
    price_per_share = parsed["price_per_share"]
    cash_amount = parsed["cash_amount"]
    date_str = (body.date or "").strip()

    metadata = db.query(TickerMetadata).filter(TickerMetadata.ticker == ticker, TickerMetadata.user_id == user_id).first()
    if metadata is None:
        raise NotFoundError(f"Ticker {ticker} is not registered — add it as an investment first")

    try:
        fx_rate_to_chf = fetch_historical_fx_rate(metadata.native_currency, date)
    except Exception as exc:  # noqa: BLE001
        raise AppError(
            502, f"Could not fetch historical FX rate for {metadata.native_currency} on {date_str}: {exc}"
        ) from exc

    # Acquired before the "existing rows" read below (not just before the
    # write) so a second concurrent request actually blocks here until the
    # first commits, then sees the first's committed transaction as part
    # of its own validation — otherwise two concurrent SELLs could each
    # validate fine against the same stale snapshot and jointly oversell.
    lock_portfolio_ticker(db, user_id, ticker)

    existing = db.query(PortfolioTransaction).filter(
        PortfolioTransaction.ticker == ticker, PortfolioTransaction.user_id == user_id
    ).all()
    candidate = ProcessedTransaction(
        ticker=ticker,
        date=date,
        type=txn_type,
        native_currency=metadata.native_currency,
        fx_rate_to_chf=fx_rate_to_chf,
        quantity=quantity,
        price_per_share=price_per_share,
        cash_amount=cash_amount,
    )
    validation = validate_fifo_integrity([portfolio_transaction_to_processed(t) for t in existing] + [candidate])
    if not validation["valid"]:
        raise AppError(400, validation["error"])

    row = PortfolioTransaction(
        user_id=user_id,
        ticker=ticker,
        date=date,
        type=txn_type,
        native_currency=metadata.native_currency,
        quantity=quantity,
        price_per_share=price_per_share,
        cash_amount=cash_amount,
        fx_rate_to_chf=fx_rate_to_chf,
        notes=notes,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _get_owned_transaction(db: Session, txn_id_str: str, user_id: str) -> PortfolioTransaction:
    try:
        txn_id = int(txn_id_str)
    except ValueError:
        raise AppError(400, "Invalid transaction id") from None
    existing = db.query(PortfolioTransaction).filter(PortfolioTransaction.id == txn_id).first()
    if existing is None or existing.user_id != user_id:
        raise NotFoundError("Transaction not found")
    return existing


@router.put("/{transaction_id}", response_model=TransactionOut)
def update_transaction(
    transaction_id: str,
    body: TransactionUpdateRequest,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_authenticated_user_id),
) -> PortfolioTransaction:
    existing_txn = _get_owned_transaction(db, transaction_id, user_id)
    ticker = existing_txn.ticker  # immutable — editing never reassigns which ticker a transaction belongs to

    metadata = db.query(TickerMetadata).filter(TickerMetadata.ticker == ticker, TickerMetadata.user_id == user_id).first()
    if metadata is None:
        raise NotFoundError(f"Ticker {ticker} is no longer registered")

    parsed = _parse_and_validate_transaction(body)
    txn_type = parsed["txn_type"]
    date = parsed["date"]
    notes = parsed["notes"]
    quantity = parsed["quantity"]
    price_per_share = parsed["price_per_share"]
    cash_amount = parsed["cash_amount"]
    date_str = (body.date or "").strip()

    # Always re-fetched regardless of whether the date actually changed —
    # simpler than diffing.
    try:
        fx_rate_to_chf = fetch_historical_fx_rate(metadata.native_currency, date)
    except Exception as exc:  # noqa: BLE001
        raise AppError(
            502, f"Could not fetch historical FX rate for {metadata.native_currency} on {date_str}: {exc}"
        ) from exc

    lock_portfolio_ticker(db, user_id, ticker)

    others = db.query(PortfolioTransaction).filter(
        PortfolioTransaction.ticker == ticker, PortfolioTransaction.user_id == user_id, PortfolioTransaction.id != existing_txn.id
    ).all()
    candidate = ProcessedTransaction(
        ticker=ticker,
        date=date,
        type=txn_type,
        native_currency=metadata.native_currency,
        fx_rate_to_chf=fx_rate_to_chf,
        quantity=quantity,
        price_per_share=price_per_share,
        cash_amount=cash_amount,
    )
    validation = validate_fifo_integrity([portfolio_transaction_to_processed(t) for t in others] + [candidate])
    if not validation["valid"]:
        raise AppError(400, validation["error"])

    existing_txn.date = date
    existing_txn.type = txn_type
    existing_txn.quantity = quantity
    existing_txn.price_per_share = price_per_share
    existing_txn.cash_amount = cash_amount
    existing_txn.fx_rate_to_chf = fx_rate_to_chf
    existing_txn.notes = notes
    db.commit()
    db.refresh(existing_txn)
    return existing_txn


@router.delete("/{transaction_id}")
def delete_transaction(
    transaction_id: str,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_authenticated_user_id),
) -> dict:
    existing_txn = _get_owned_transaction(db, transaction_id, user_id)

    lock_portfolio_ticker(db, user_id, existing_txn.ticker)

    remaining = db.query(PortfolioTransaction).filter(
        PortfolioTransaction.ticker == existing_txn.ticker,
        PortfolioTransaction.user_id == user_id,
        PortfolioTransaction.id != existing_txn.id,
    ).all()
    validation = validate_fifo_integrity([portfolio_transaction_to_processed(t) for t in remaining])
    if not validation["valid"]:
        raise AppError(400, f"Cannot delete: {validation['error']}")

    db.delete(existing_txn)
    db.commit()
    return {"success": True}
