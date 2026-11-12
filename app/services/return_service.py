from sqlalchemy.orm import Session

from app.schemas.portfolio import CombinedReturn
from app.services.passive_rollup_service import compute_passive_rollup_with_flows
from app.services.portfolio_rollup_service import compute_portfolio_rollup_with_flows
from app.services.xirr import span_days, xirr_pct


def compute_combined_return(db: Session, user_id: str | None = None, *, force_refresh: bool = False) -> CombinedReturn:
    """XIRR over securities and passive cash flows pooled together. Reuses
    the rollups' own flow construction, so what's included/excluded (e.g.
    tickers with a failed price fetch) always matches their totals.
    """
    _, securities_flows = compute_portfolio_rollup_with_flows(db, user_id, force_refresh=force_refresh)
    _, passive_flows = compute_passive_rollup_with_flows(db, user_id, force_refresh=force_refresh)
    flows = securities_flows + passive_flows
    return CombinedReturn(xirr_pct=xirr_pct(flows), xirr_span_days=span_days(flows))
