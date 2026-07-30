"""Ported from lib/passive/detail.ts."""

from sqlalchemy.orm import Session

from app.models import PassiveInvestment, PassiveRecurringDeposit, PassiveTransaction
from app.schemas.passive import PassiveInvestmentDetail, PassiveRecurringDepositInfo, PassiveTransactionRow
from app.services.ledger import PassiveLedgerTxn, compute_net_balance
from app.services.price_service import fetch_fx_rate_to_chf
from app.services.recurring import materialize_due_recurring_deposits


def compute_passive_investment_detail(db: Session, investment_id: int, user_id: str) -> PassiveInvestmentDetail | None:
    investment = (
        db.query(PassiveInvestment)
        .filter(PassiveInvestment.id == investment_id, PassiveInvestment.user_id == user_id)
        .first()
    )
    if investment is None:
        return None

    # Ownership already confirmed above, so this can only ever touch the
    # one investment the caller is authorized for.
    materialize_due_recurring_deposits(db, passive_investment_id=investment_id)

    txns = (
        db.query(PassiveTransaction)
        .filter(PassiveTransaction.passive_investment_id == investment_id)
        .order_by(PassiveTransaction.date.asc())
        .all()
    )
    rule = (
        db.query(PassiveRecurringDeposit)
        .filter(PassiveRecurringDeposit.passive_investment_id == investment_id)
        .first()
    )

    cost_basis_native = compute_net_balance(
        [PassiveLedgerTxn(date=t.date, type=t.type, amount_native=t.amount_native) for t in txns]
    )
    market_value_native = (
        cost_basis_native * (1 + investment.gain_loss_pct / 100) if investment.gain_loss_pct is not None else cost_basis_native
    )
    unrealized_gain_native = market_value_native - cost_basis_native

    detail = PassiveInvestmentDetail(
        id=investment.id,
        name=investment.name,
        type=investment.type,
        currency=investment.currency,
        cost_basis_native=cost_basis_native,
        cost_basis_chf=0.0,
        market_value_native=market_value_native,
        market_value_chf=0.0,
        unrealized_gain_native=unrealized_gain_native,
        unrealized_gain_chf=0.0,
        gain_loss_pct=investment.gain_loss_pct,
        gain_loss_updated_at=investment.gain_loss_updated_at,
        notes=investment.notes,
        transactions=[
            PassiveTransactionRow(id=t.id, date=t.date, type=t.type, amount_native=t.amount_native, notes=t.notes)
            for t in txns
        ],
        recurring_deposit=(
            PassiveRecurringDepositInfo(
                id=rule.id,
                amount_native=rule.amount_native,
                start_date=rule.start_date,
                frequency=rule.frequency,
                end_date=rule.end_date,
                notes=rule.notes,
            )
            if rule
            else None
        ),
    )

    try:
        fx_rate = fetch_fx_rate_to_chf(investment.currency)
        detail.fx_rate_to_chf = fx_rate
        detail.cost_basis_chf = cost_basis_native * fx_rate
        detail.market_value_chf = market_value_native * fx_rate
        detail.unrealized_gain_chf = detail.market_value_chf - detail.cost_basis_chf
    except Exception as exc:  # noqa: BLE001 — never throws; CHF fields stay 0
        detail.fx_error = str(exc)

    return detail
