import logging

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.dependencies.auth import get_authenticated_user_id
from app.exceptions import AppError
from app.schemas.passive import PassiveRollup
from app.services.passive_rollup_service import compute_passive_rollup

logger = logging.getLogger(__name__)

router = APIRouter(tags=["passive-rollup"])


@router.get("/passive-rollup", response_model=PassiveRollup)
def get_passive_rollup(
    refresh: bool = False,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_authenticated_user_id),
) -> PassiveRollup:
    try:
        return compute_passive_rollup(db, user_id, force_refresh=refresh)
    except Exception as exc:  # noqa: BLE001 — unexpected: this path has no
        # expected-error case of its own (FX failures are already caught
        # per-currency inside compute_passive_rollup). Not surfaced to the
        # client: an unexpected exception here (e.g. a DB driver error) can
        # embed connection details, which must never reach a response body.
        logger.exception("Unexpected error computing passive rollup")
        raise AppError(500, "Internal server error") from exc
