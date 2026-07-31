from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.dependencies.auth import get_authenticated_user_id
from app.exceptions import AppError
from app.schemas.portfolio import PortfolioRollup
from app.services.portfolio_rollup_service import compute_portfolio_rollup

router = APIRouter(tags=["portfolio-rollup"])


@router.get("/portfolio-rollup", response_model=PortfolioRollup)
def get_portfolio_rollup(
    refresh: bool = False,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_authenticated_user_id),
) -> PortfolioRollup:
    try:
        return compute_portfolio_rollup(db, user_id, force_refresh=refresh)
    except Exception as exc:  # noqa: BLE001 — surfaces the raw message rather than a generic one
        raise AppError(500, str(exc)) from exc
