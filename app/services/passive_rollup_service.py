"""Ported from lib/passive/rollup.ts."""

from concurrent.futures import ThreadPoolExecutor

from sqlalchemy.orm import Session

from app.models import PassiveInvestment
from app.schemas.passive import PassiveFxError, PassiveInvestmentRollupRow, PassiveRollup
from app.services.ledger import PassiveLedgerTxn, compute_net_balance
from app.services.price_service import fetch_fx_rate_to_chf
from app.services.recurring import materialize_due_recurring_deposits


def compute_passive_rollup(db: Session, user_id: str | None = None) -> PassiveRollup:
    if user_id:
        materialize_due_recurring_deposits(db, user_id=user_id)

    query = db.query(PassiveInvestment).order_by(PassiveInvestment.name.asc())
    if user_id:
        query = query.filter(PassiveInvestment.user_id == user_id)
    investments = query.all()

    distinct_currencies = list({inv.currency for inv in investments})
    fx_rate_by_currency: dict[str, float] = {}
    fx_errors: list[PassiveFxError] = []

    def fetch_fx(ccy: str) -> tuple[str, float | None, str | None]:
        try:
            return ccy, fetch_fx_rate_to_chf(ccy), None
        except Exception as exc:  # noqa: BLE001
            return ccy, None, str(exc)

    if distinct_currencies:
        with ThreadPoolExecutor(max_workers=len(distinct_currencies)) as pool:
            for ccy, rate, error in pool.map(fetch_fx, distinct_currencies):
                if rate is not None:
                    fx_rate_by_currency[ccy] = rate
                else:
                    fx_errors.append(PassiveFxError(currency=ccy, error=error))

    rows: list[PassiveInvestmentRollupRow] = []
    for inv in investments:
        fx_rate = fx_rate_by_currency.get(inv.currency)
        if fx_rate is None:
            continue

        cost_basis_native = compute_net_balance(
            [PassiveLedgerTxn(date=t.date, type=t.type, amount_native=t.amount_native) for t in inv.transactions]
        )
        market_value_native = (
            cost_basis_native * (1 + inv.gain_loss_pct / 100) if inv.gain_loss_pct is not None else cost_basis_native
        )
        cost_basis_chf = cost_basis_native * fx_rate
        market_value_chf = market_value_native * fx_rate

        rows.append(
            PassiveInvestmentRollupRow(
                id=inv.id,
                name=inv.name,
                type=inv.type,
                currency=inv.currency,
                cost_basis_native=cost_basis_native,
                cost_basis_chf=cost_basis_chf,
                market_value_native=market_value_native,
                market_value_chf=market_value_chf,
                unrealized_gain_native=market_value_native - cost_basis_native,
                unrealized_gain_chf=market_value_chf - cost_basis_chf,
                gain_loss_pct=inv.gain_loss_pct,
                gain_loss_updated_at=inv.gain_loss_updated_at,
                notes=inv.notes,
            )
        )

    return PassiveRollup(
        rows=rows,
        fx_errors=fx_errors,
        total_cost_basis_chf=sum(r.cost_basis_chf for r in rows),
        total_market_value_chf=sum(r.market_value_chf for r in rows),
        total_unrealized_gain_chf=sum(r.unrealized_gain_chf for r in rows),
    )
