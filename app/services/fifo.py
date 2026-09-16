"""FIFO cost-basis engine. Distinct fields per transaction type (rather
than one generic "shares" field) is the whole point: it's what prevents
dividend cash from being mistaken for a share quantity.
"""

from dataclasses import dataclass, field
from datetime import datetime

TOLERANCE = 1e-9


@dataclass
class Lot:
    qty: float
    cost_per_share: float  # native currency
    date: datetime
    fx_rate_to_chf: float  # captured at purchase time, immutable
    is_from_drip: bool = False


@dataclass
class ProcessedTransaction:
    ticker: str
    date: datetime
    type: str  # "BUY" | "SELL" | "DIVIDEND" | "DRIP"
    native_currency: str
    fx_rate_to_chf: float
    quantity: float | None = None  # BUY, SELL, DRIP
    price_per_share: float | None = None  # BUY, SELL, DRIP — native currency
    cash_amount: float | None = None  # DIVIDEND — native currency
    notes: str | None = None
    # Primary key of the underlying row, used only as a same-date tiebreak
    # (see process_ticker's docstring). None for a not-yet-persisted
    # candidate transaction (e.g. the create/update mutation-validation
    # path), which then sorts after every persisted row on the same date.
    id: int | None = None


@dataclass
class ConsumedLot:
    qty: float
    cost_per_share: float
    date: datetime
    fx_rate_to_chf: float


@dataclass
class RealizedGain:
    date: datetime
    qty_sold: float
    proceeds_native: float
    cost_basis_native: float
    gain_native: float
    proceeds_chf: float
    cost_basis_chf: float
    gain_chf: float
    lots_consumed: list[ConsumedLot] = field(default_factory=list)


@dataclass
class FIFOResult:
    remaining_lots: list[Lot]
    current_shares: float
    current_cost_basis_native: float
    current_cost_basis_chf: float
    realized_gains: list[RealizedGain]
    total_realized_gain_native: float
    total_realized_gain_chf: float


def process_ticker(transactions: list[ProcessedTransaction]) -> FIFOResult:
    """Input transactions must already be sorted ascending by date (caller
    responsibility), with ties on the same date broken by ascending `id`
    (insertion order) — a not-yet-persisted candidate (`id is None`) sorts
    after every persisted row sharing that date. Since FIFO lot order
    directly determines cost-basis/realized-gain numbers, this tiebreak
    must be applied identically everywhere the same underlying rows are
    consumed or displayed (see app/services/mutation_validation.py and the
    `ORDER BY date, id` queries in the read-path services) — otherwise two
    same-date transactions can be FIFO-ordered one way for mutation
    validation and a different way for display.
    """
    lots: list[Lot] = []
    realized: list[RealizedGain] = []

    for txn in transactions:
        if txn.type in ("BUY", "DRIP"):
            lots.append(
                Lot(
                    qty=txn.quantity,
                    cost_per_share=txn.price_per_share,
                    date=txn.date,
                    fx_rate_to_chf=txn.fx_rate_to_chf,
                    is_from_drip=(txn.type == "DRIP"),
                )
            )
        elif txn.type == "SELL":
            remaining = txn.quantity
            cost_basis_native = 0.0
            cost_basis_chf = 0.0
            lots_consumed: list[ConsumedLot] = []
            while remaining > TOLERANCE:
                if not lots:
                    raise ValueError(
                        f"Selling more shares than held for {txn.ticker} on "
                        f"{txn.date.date().isoformat()}: {remaining} shares short"
                    )
                lot = lots[0]
                take = min(lot.qty, remaining)
                cost_basis_native += take * lot.cost_per_share
                # Uses the consumed lot's own historical FX rate — never
                # today's rate nor the sell transaction's rate.
                cost_basis_chf += take * lot.cost_per_share * lot.fx_rate_to_chf
                lots_consumed.append(
                    ConsumedLot(
                        qty=take, cost_per_share=lot.cost_per_share, date=lot.date, fx_rate_to_chf=lot.fx_rate_to_chf
                    )
                )
                lot.qty -= take
                remaining -= take
                if lot.qty <= TOLERANCE:
                    lots.pop(0)
            proceeds_native = txn.quantity * txn.price_per_share
            proceeds_chf = proceeds_native * txn.fx_rate_to_chf  # sell's own FX rate — proceeds are realized "now"
            realized.append(
                RealizedGain(
                    date=txn.date,
                    qty_sold=txn.quantity,
                    proceeds_native=proceeds_native,
                    cost_basis_native=cost_basis_native,
                    gain_native=proceeds_native - cost_basis_native,
                    proceeds_chf=proceeds_chf,
                    cost_basis_chf=cost_basis_chf,
                    gain_chf=proceeds_chf - cost_basis_chf,
                    lots_consumed=lots_consumed,
                )
            )
        elif txn.type == "DIVIDEND":
            continue  # no-op — never touches lots or share count
        else:
            raise ValueError(f"Unknown transaction type: {txn.type}")

    current_shares = sum(lot.qty for lot in lots)
    current_cost_basis_native = sum(lot.qty * lot.cost_per_share for lot in lots)
    current_cost_basis_chf = sum(lot.qty * lot.cost_per_share * lot.fx_rate_to_chf for lot in lots)
    total_realized_gain_native = sum(r.gain_native for r in realized)
    total_realized_gain_chf = sum(r.gain_chf for r in realized)

    return FIFOResult(
        remaining_lots=lots,
        current_shares=current_shares,
        current_cost_basis_native=current_cost_basis_native,
        current_cost_basis_chf=current_cost_basis_chf,
        realized_gains=realized,
        total_realized_gain_native=total_realized_gain_native,
        total_realized_gain_chf=total_realized_gain_chf,
    )
