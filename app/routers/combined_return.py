import logging

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.dependencies.auth import get_authenticated_user_id
from app.exceptions import AppError
from app.schemas.portfolio import CombinedReturn
from app.services.return_service import compute_combined_return

logger = logging.getLogger(__name__)

router = APIRouter(tags=["combined-return"])


@router.get("/combined-return", response_model=CombinedReturn)
def get_combined_return(
    refresh: bool = False,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_authenticated_user_id),
) -> CombinedReturn:
    try:
        return compute_combined_return(db, user_id, force_refresh=refresh)
    except RuntimeError as exc:
        # Missing TickerMetadata for an open position - our own message.
        raise AppError(500, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        # Not surfaced: unexpected errors can embed connection details.
        logger.exception("Unexpected error computing combined return")
        raise AppError(500, "Internal server error") from exc
