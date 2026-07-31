from fastapi import APIRouter, Depends
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.dependencies.auth import get_authenticated_user_id
from app.exceptions import AppError, NotFoundError
from app.models import PassiveInvestment, PassiveRecurringDeposit
from app.schemas.passive import RecurringDepositCreateRequest, RecurringDepositOut, RecurringDepositUpdateRequest
from app.services.validation import RECURRING_FREQUENCIES, is_valid_recurring_frequency
from app.utils import parse_date, to_number

router = APIRouter(prefix="/passive-investments/{investment_id}/recurring-deposit", tags=["passive-recurring-deposit"])


def _parse_investment_id(investment_id: str) -> int:
    try:
        return int(investment_id)
    except ValueError:
        raise AppError(400, "invalid id") from None


def _parse_and_validate(body) -> dict:
    amount_native = to_number(body.amount_native)
    if amount_native is None or amount_native <= 0:
        raise AppError(400, "amountNative must be a positive number")

    frequency = (body.frequency or "").strip().upper()
    if not is_valid_recurring_frequency(frequency):
        raise AppError(400, f"frequency must be one of: {', '.join(RECURRING_FREQUENCIES)}")

    start_date = parse_date(body.start_date)
    if start_date is None:
        raise AppError(400, "startDate is invalid")

    end_date = None
    if body.end_date:
        end_date = parse_date(body.end_date)
        if end_date is None:
            raise AppError(400, "endDate is invalid")
        if end_date < start_date:
            raise AppError(400, "endDate must be on or after startDate")

    notes = (body.notes or "").strip() or None

    return {"amount_native": amount_native, "frequency": frequency, "start_date": start_date, "end_date": end_date, "notes": notes}


def _get_owned_investment(db: Session, investment_id: int, user_id: str) -> PassiveInvestment:
    investment = db.query(PassiveInvestment).filter(PassiveInvestment.id == investment_id, PassiveInvestment.user_id == user_id).first()
    if investment is None:
        raise NotFoundError("Passive investment not found")
    return investment


@router.post("", response_model=RecurringDepositOut, status_code=201)
def create_recurring_deposit(
    investment_id: str,
    body: RecurringDepositCreateRequest,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_authenticated_user_id),
) -> PassiveRecurringDeposit:
    inv_id = _parse_investment_id(investment_id)
    _get_owned_investment(db, inv_id, user_id)

    existing_rule = db.query(PassiveRecurringDeposit).filter(PassiveRecurringDeposit.passive_investment_id == inv_id).first()
    if existing_rule is not None:
        raise AppError(409, "A recurring deposit rule already exists for this investment")

    parsed = _parse_and_validate(body)
    row = PassiveRecurringDeposit(passive_investment_id=inv_id, last_generated_date=None, **parsed)
    db.add(row)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        if "uq_passive_recurring_deposits_passive_investment_id" in str(exc.orig):
            raise AppError(409, "A recurring deposit rule already exists for this investment") from exc
        raise AppError(500, "Failed to create recurring deposit") from exc
    db.refresh(row)
    return row


@router.put("", response_model=RecurringDepositOut)
def update_recurring_deposit(
    investment_id: str,
    body: RecurringDepositUpdateRequest,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_authenticated_user_id),
) -> PassiveRecurringDeposit:
    inv_id = _parse_investment_id(investment_id)
    _get_owned_investment(db, inv_id, user_id)

    existing_rule = db.query(PassiveRecurringDeposit).filter(PassiveRecurringDeposit.passive_investment_id == inv_id).first()
    if existing_rule is None:
        raise NotFoundError("No recurring deposit rule exists for this investment")

    parsed = _parse_and_validate(body)
    # lastGeneratedDate is deliberately never written here — the catch-up
    # scan always filters by comparing actual occurrence timestamps to it,
    # so it stays correct even after startDate/frequency change underneath it.
    existing_rule.amount_native = parsed["amount_native"]
    existing_rule.frequency = parsed["frequency"]
    existing_rule.start_date = parsed["start_date"]
    existing_rule.end_date = parsed["end_date"]
    existing_rule.notes = parsed["notes"]
    db.commit()
    db.refresh(existing_rule)
    return existing_rule


@router.delete("")
def delete_recurring_deposit(
    investment_id: str,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_authenticated_user_id),
) -> dict:
    inv_id = _parse_investment_id(investment_id)
    _get_owned_investment(db, inv_id, user_id)

    existing_rule = db.query(PassiveRecurringDeposit).filter(PassiveRecurringDeposit.passive_investment_id == inv_id).first()
    if existing_rule is None:
        raise NotFoundError("No recurring deposit rule exists for this investment")

    # No FK from PassiveTransaction to this model — deleting the rule
    # cannot touch already-generated deposits.
    db.delete(existing_rule)
    db.commit()
    return {"success": True}
