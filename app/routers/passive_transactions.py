from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.dependencies.auth import get_authenticated_user_id
from app.exceptions import AppError, NotFoundError
from app.models import PassiveInvestment, PassiveTransaction
from app.schemas.passive import PassiveTransactionCreateRequest, PassiveTransactionOut, PassiveTransactionUpdateRequest
from app.services.ledger import PassiveLedgerTxn, validate_cash_ledger_integrity
from app.services.locking import lock_passive_investment
from app.services.validation import PASSIVE_TXN_TYPES, is_valid_passive_txn_type
from app.utils import parse_date, parse_int_id, to_number

router = APIRouter(prefix="/passive-investments/{investment_id}/transactions", tags=["passive-transactions"])


def _to_ledger_txn(t: PassiveTransaction) -> PassiveLedgerTxn:
    return PassiveLedgerTxn(date=t.date, type=t.type, amount_native=t.amount_native, id=t.id)


@router.post("", response_model=PassiveTransactionOut, status_code=201)
def create_passive_transaction(
    investment_id: str,
    body: PassiveTransactionCreateRequest,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_authenticated_user_id),
) -> PassiveTransaction:
    inv_id = parse_int_id(investment_id)
    investment = db.query(PassiveInvestment).filter(PassiveInvestment.id == inv_id, PassiveInvestment.user_id == user_id).first()
    if investment is None:
        raise NotFoundError("Passive investment not found")

    txn_type = (body.type or "").strip().upper()
    if not is_valid_passive_txn_type(txn_type):
        raise AppError(400, f"type must be one of: {', '.join(PASSIVE_TXN_TYPES)}")
    date = parse_date(body.date)
    if date is None:
        raise AppError(400, "date is invalid")
    amount_native = to_number(body.amount_native)
    if amount_native is None or amount_native <= 0:
        raise AppError(400, "amountNative must be a positive number")
    notes = (body.notes or "").strip() or None

    # See app/services/locking.py — acquired before the "existing rows"
    # read so a second concurrent request blocks until this one commits,
    # then validates against its committed write instead of a stale snapshot.
    lock_passive_investment(db, inv_id)

    existing_txns = (
        db.query(PassiveTransaction)
        .filter(PassiveTransaction.passive_investment_id == inv_id)
        .order_by(PassiveTransaction.date.asc(), PassiveTransaction.id.asc())
        .all()
    )
    candidate = PassiveLedgerTxn(date=date, type=txn_type, amount_native=amount_native)
    validation = validate_cash_ledger_integrity([_to_ledger_txn(t) for t in existing_txns] + [candidate])
    if not validation["valid"]:
        raise AppError(400, validation["error"])

    row = PassiveTransaction(passive_investment_id=inv_id, type=txn_type, date=date, amount_native=amount_native, notes=notes)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _get_owned_transaction(db: Session, investment_id: int, txn_id_str: str, user_id: str) -> PassiveTransaction:
    txn_id = parse_int_id(txn_id_str)
    existing = (
        db.query(PassiveTransaction)
        .join(PassiveInvestment)
        .filter(
            PassiveTransaction.id == txn_id,
            PassiveTransaction.passive_investment_id == investment_id,
            PassiveInvestment.user_id == user_id,
        )
        .first()
    )
    if existing is None:
        raise NotFoundError("Transaction not found")
    return existing


@router.put("/{txn_id}", response_model=PassiveTransactionOut)
def update_passive_transaction(
    investment_id: str,
    txn_id: str,
    body: PassiveTransactionUpdateRequest,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_authenticated_user_id),
) -> PassiveTransaction:
    inv_id = parse_int_id(investment_id)
    existing_txn = _get_owned_transaction(db, inv_id, txn_id, user_id)

    txn_type = (body.type or "").strip().upper()
    if not is_valid_passive_txn_type(txn_type):
        raise AppError(400, f"type must be one of: {', '.join(PASSIVE_TXN_TYPES)}")
    date = parse_date(body.date)
    if date is None:
        raise AppError(400, "date is invalid")
    amount_native = to_number(body.amount_native)
    if amount_native is None or amount_native <= 0:
        raise AppError(400, "amountNative must be a positive number")
    notes = (body.notes or "").strip() or None

    lock_passive_investment(db, inv_id)

    others = (
        db.query(PassiveTransaction)
        .filter(PassiveTransaction.passive_investment_id == inv_id, PassiveTransaction.id != existing_txn.id)
        .order_by(PassiveTransaction.date.asc(), PassiveTransaction.id.asc())
        .all()
    )
    candidate = PassiveLedgerTxn(date=date, type=txn_type, amount_native=amount_native)
    validation = validate_cash_ledger_integrity([_to_ledger_txn(t) for t in others] + [candidate])
    if not validation["valid"]:
        raise AppError(400, validation["error"])

    existing_txn.date = date
    existing_txn.type = txn_type
    existing_txn.amount_native = amount_native
    existing_txn.notes = notes
    db.commit()
    db.refresh(existing_txn)
    return existing_txn


@router.delete("/{txn_id}")
def delete_passive_transaction(
    investment_id: str,
    txn_id: str,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_authenticated_user_id),
) -> dict:
    inv_id = parse_int_id(investment_id)
    existing_txn = _get_owned_transaction(db, inv_id, txn_id, user_id)

    lock_passive_investment(db, inv_id)

    remaining = (
        db.query(PassiveTransaction)
        .filter(PassiveTransaction.passive_investment_id == inv_id, PassiveTransaction.id != existing_txn.id)
        .order_by(PassiveTransaction.date.asc(), PassiveTransaction.id.asc())
        .all()
    )
    validation = validate_cash_ledger_integrity([_to_ledger_txn(t) for t in remaining])
    if not validation["valid"]:
        raise AppError(400, f"Cannot delete: {validation['error']}")

    db.delete(existing_txn)
    db.commit()
    return {"success": True}
