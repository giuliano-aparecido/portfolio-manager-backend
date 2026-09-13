from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.dependencies.auth import get_authenticated_user_id
from app.exceptions import AppError, NotFoundError
from app.models import PassiveInvestment
from app.schemas.passive import PassiveInvestmentCreateRequest, PassiveInvestmentDetail, PassiveInvestmentOut, PassiveInvestmentUpdateRequest
from app.services.passive_detail import compute_passive_investment_detail
from app.services.validation import CURRENCIES, PASSIVE_TYPES, MIN_GAIN_LOSS_PCT, is_valid_currency, is_valid_gain_loss_pct, is_valid_passive_type
from app.utils import parse_int_id, to_number

router = APIRouter(prefix="/passive-investments", tags=["passive-investments"])

GAIN_LOSS_PCT_TOLERANCE = 1e-9


def _gain_loss_pct_changed(new: float | None, old: float | None) -> bool:
    if new is None or old is None:
        return new != old
    return abs(new - old) > GAIN_LOSS_PCT_TOLERANCE


def _parse_and_validate_investment(body) -> dict:
    name = (body.name or "").strip()
    type_ = (body.type or "").strip().upper()
    currency = (body.currency or "").strip().upper()
    notes = (body.notes or "").strip() or None

    if not name:
        raise AppError(400, "name is required")
    if not is_valid_passive_type(type_):
        raise AppError(400, f"type must be one of: {', '.join(PASSIVE_TYPES)}")
    if not is_valid_currency(currency):
        raise AppError(400, f"currency must be one of: {', '.join(CURRENCIES)}")

    return {"name": name, "type": type_, "currency": currency, "notes": notes}


@router.get("", response_model=list[PassiveInvestmentOut])
def list_passive_investments(
    db: Session = Depends(get_db),
    user_id: str = Depends(get_authenticated_user_id),
) -> list[PassiveInvestment]:
    return db.query(PassiveInvestment).filter(PassiveInvestment.user_id == user_id).all()


@router.post("", response_model=PassiveInvestmentOut, status_code=201)
def create_passive_investment(
    body: PassiveInvestmentCreateRequest,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_authenticated_user_id),
) -> PassiveInvestment:
    parsed = _parse_and_validate_investment(body)

    # gainLossPct is deliberately never accepted here — a brand-new
    # investment always has zero cost basis, so entering a % at creation
    # time would be inert. It's only editable via PUT, once deposits exist.
    row = PassiveInvestment(user_id=user_id, **parsed)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@router.get("/{investment_id}", response_model=PassiveInvestmentDetail)
def get_passive_investment_detail(
    investment_id: str,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_authenticated_user_id),
) -> PassiveInvestmentDetail:
    parsed_id = parse_int_id(investment_id)

    detail = compute_passive_investment_detail(db, parsed_id, user_id)
    if detail is None:
        raise NotFoundError("Passive investment not found")
    return detail


@router.put("/{investment_id}", response_model=PassiveInvestmentOut)
def update_passive_investment(
    investment_id: str,
    body: PassiveInvestmentUpdateRequest,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_authenticated_user_id),
) -> PassiveInvestment:
    parsed_id = parse_int_id(investment_id)

    existing = db.query(PassiveInvestment).filter(PassiveInvestment.id == parsed_id, PassiveInvestment.user_id == user_id).first()
    if existing is None:
        raise NotFoundError("Passive investment not found")

    parsed = _parse_and_validate_investment(body)

    raw_gain_loss_pct = body.gain_loss_pct
    if raw_gain_loss_pct is None or raw_gain_loss_pct == "":
        gain_loss_pct = None
    else:
        parsed_gain_loss_pct = to_number(raw_gain_loss_pct)
        if not is_valid_gain_loss_pct(parsed_gain_loss_pct):
            raise AppError(400, f"gainLossPct must be a number >= {MIN_GAIN_LOSS_PCT}")
        gain_loss_pct = parsed_gain_loss_pct

    gain_loss_changed = _gain_loss_pct_changed(gain_loss_pct, existing.gain_loss_pct)

    existing.name = parsed["name"]
    existing.type = parsed["type"]
    existing.currency = parsed["currency"]
    existing.notes = parsed["notes"]
    existing.gain_loss_pct = gain_loss_pct
    if gain_loss_changed:
        existing.gain_loss_updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(existing)
    return existing


@router.delete("/{investment_id}")
def delete_passive_investment(
    investment_id: str,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_authenticated_user_id),
) -> dict:
    parsed_id = parse_int_id(investment_id)

    existing = db.query(PassiveInvestment).filter(PassiveInvestment.id == parsed_id, PassiveInvestment.user_id == user_id).first()
    if existing is None:
        raise NotFoundError("Passive investment not found")

    # DB-level ON DELETE CASCADE removes all its PassiveTransaction and
    # PassiveRecurringDeposit rows automatically.
    db.delete(existing)
    db.commit()
    return {"success": True}
