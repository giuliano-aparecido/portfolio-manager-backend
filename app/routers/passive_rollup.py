from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.dependencies.auth import get_authenticated_user_id
from app.exceptions import AppError
from app.schemas.passive import PassiveRollup
from app.services.passive_rollup_service import compute_passive_rollup

router = APIRouter(tags=["passive-rollup"])


@router.get("/passive-rollup", response_model=PassiveRollup)
def get_passive_rollup(
    refresh: bool = False,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_authenticated_user_id),
) -> PassiveRollup:
    try:
        return compute_passive_rollup(db, user_id, force_refresh=refresh)
    except Exception as exc:  # noqa: BLE001 — surfaces the raw message, matching the original route
        raise AppError(500, str(exc)) from exc
