from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy.orm import Session

from app.models import PassiveInvestment, PassiveTransaction
from app.schemas.passive import PassiveFxError, PassiveInvestmentRollupRow, PassiveRollup
from app.services.ledger import PassiveLedgerTxn, compute_net_balance
from app.services.price_service import fetch_fx_rate_to_chf
from app.services.recurring import materialize_due_recurring_deposits

# Matches app/services/fundamentals/cache.py's _MAX_FETCH_WORKERS.
_MAX_FX_FETCH_WORKERS = 8


def compute_passive_rollup(db: Session, user_id: str | None = None, *, force_refresh: bool = False) -> PassiveRollup:
    if user_id:
        materialize_due_recurring_deposits(db, user_id=user_id)

    query = db.query(PassiveInvestment).order_by(PassiveInvestment.name.asc())
    if user_id:
        query = query.filter(PassiveInvestment.user_id == user_id)
    investments = query.all()

    # Bulk-queried and grouped in Python rather than relying on
    # inv.transactions' lazy load per iteration below - matches
    # portfolio_rollup_service.py's approach, avoiding an extra SELECT per
    # investment.
    txn_query = db.query(PassiveTransaction)
    if investments:
        txn_query = txn_query.filter(
            PassiveTransaction.passive_investment_id.in_([inv.id for inv in investments])
        )
    transactions_by_investment: dict[int, list[PassiveTransaction]] = defaultdict(list)
    for t in txn_query.all():
        transactions_by_investment[t.passive_investment_id].append(t)

    distinct_currencies = list({inv.currency for inv in investments})
    fx_rate_by_currency: dict[str, float] = {}
    fx_error_by_currency: dict[str, str] = {}

    def fetch_fx(ccy: str) -> tuple[str, float | None, str | None]:
        try:
            return ccy, fetch_fx_rate_to_chf(ccy, force_refresh=force_refresh), None
        except Exception as exc:  # noqa: BLE001
            return ccy, None, str(exc)

    if distinct_currencies:
        with ThreadPoolExecutor(max_workers=min(len(distinct_currencies), _MAX_FX_FETCH_WORKERS)) as pool:
            for ccy, rate, error in pool.map(fetch_fx, distinct_currencies):
                if rate is not None:
                    fx_rate_by_currency[ccy] = rate
                else:
                    fx_error_by_currency[ccy] = error

    # Built from the actual investments being dropped below, rather than
    # guessed from `distinct_currencies` up front, so the reported list is
    # exactly which rows are missing from the totals — not just which
    # currencies failed.
    affected_investments_by_currency: dict[str, list[str]] = defaultdict(list)

    rows: list[PassiveInvestmentRollupRow] = []
    for inv in investments:
        fx_rate = fx_rate_by_currency.get(inv.currency)
        if fx_rate is None:
            affected_investments_by_currency[inv.currency].append(inv.name)
            continue

        cost_basis_native = compute_net_balance(
            [
                PassiveLedgerTxn(date=t.date, type=t.type, amount_native=t.amount_native)
                for t in transactions_by_investment[inv.id]
            ]
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

    fx_errors = [
        PassiveFxError(currency=ccy, error=error, affected_investments=affected_investments_by_currency[ccy])
        for ccy, error in fx_error_by_currency.items()
    ]

    return PassiveRollup(
        rows=rows,
        fx_errors=fx_errors,
        total_cost_basis_chf=sum(r.cost_basis_chf for r in rows),
        total_market_value_chf=sum(r.market_value_chf for r in rows),
        total_unrealized_gain_chf=sum(r.unrealized_gain_chf for r in rows),
    )
