import logging

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.dependencies.auth import get_authenticated_user_id
from app.exceptions import AppError
from app.schemas.portfolio import PortfolioRollup
from app.services.portfolio_rollup_service import compute_portfolio_rollup

logger = logging.getLogger(__name__)

router = APIRouter(tags=["portfolio-rollup"])


@router.get("/portfolio-rollup", response_model=PortfolioRollup)
def get_portfolio_rollup(
    refresh: bool = False,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_authenticated_user_id),
) -> PortfolioRollup:
    try:
        return compute_portfolio_rollup(db, user_id, force_refresh=refresh)
    except RuntimeError as exc:
        # The one expected failure mode (missing TickerMetadata for an open
        # position) - message is entirely our own text, safe to surface.
        raise AppError(500, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 — anything else is unexpected
        # Not surfaced to the client: an unexpected exception here (e.g. a
        # DB driver error) can embed connection details, which must never
        # reach an HTTP response body.
        logger.exception("Unexpected error computing portfolio rollup")
        raise AppError(500, "Internal server error") from exc
