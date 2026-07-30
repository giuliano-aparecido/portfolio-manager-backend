from datetime import datetime

from app.schemas.common import CamelModel

# --- PassiveInvestment ---------------------------------------------------


class PassiveInvestmentCreateRequest(CamelModel):
    name: str | None = None
    type: str | None = None
    currency: str | None = None
    notes: str | None = None


class PassiveInvestmentUpdateRequest(CamelModel):
    name: str | None = None
    type: str | None = None
    currency: str | None = None
    notes: str | None = None
    gain_loss_pct: float | str | None = None


class PassiveInvestmentOut(CamelModel):
    id: int
    user_id: str
    name: str
    type: str
    currency: str
    notes: str | None
    gain_loss_pct: float | None
    gain_loss_updated_at: datetime | None
    created_at: datetime
    updated_at: datetime


# --- PassiveTransaction ---------------------------------------------------


class PassiveTransactionCreateRequest(CamelModel):
    type: str | None = None
    date: str | None = None
    amount_native: float | None = None
    notes: str | None = None


class PassiveTransactionUpdateRequest(CamelModel):
    type: str | None = None
    date: str | None = None
    amount_native: float | None = None
    notes: str | None = None


class PassiveTransactionOut(CamelModel):
    id: int
    passive_investment_id: int
    type: str
    date: datetime
    amount_native: float
    notes: str | None
    created_at: datetime


# --- PassiveRecurringDeposit ------------------------------------------------


class RecurringDepositCreateRequest(CamelModel):
    amount_native: float | None = None
    frequency: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    notes: str | None = None


class RecurringDepositUpdateRequest(CamelModel):
    amount_native: float | None = None
    frequency: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    notes: str | None = None


class RecurringDepositOut(CamelModel):
    id: int
    passive_investment_id: int
    amount_native: float
    start_date: datetime
    frequency: str
    end_date: datetime | None
    notes: str | None


# --- Detail / rollup -----------------------------------------------------


class PassiveTransactionRow(CamelModel):
    id: int
    date: datetime
    type: str
    amount_native: float
    notes: str | None


class PassiveRecurringDepositInfo(CamelModel):
    id: int
    amount_native: float
    start_date: datetime
    frequency: str
    end_date: datetime | None
    notes: str | None


class PassiveInvestmentDetail(CamelModel):
    id: int
    name: str
    type: str
    currency: str
    cost_basis_native: float
    cost_basis_chf: float
    market_value_native: float
    market_value_chf: float
    unrealized_gain_native: float
    unrealized_gain_chf: float
    gain_loss_pct: float | None
    gain_loss_updated_at: datetime | None
    fx_rate_to_chf: float | None = None
    fx_error: str | None = None
    notes: str | None
    transactions: list[PassiveTransactionRow]
    recurring_deposit: PassiveRecurringDepositInfo | None


class PassiveInvestmentRollupRow(CamelModel):
    id: int
    name: str
    type: str
    currency: str
    cost_basis_native: float
    cost_basis_chf: float
    market_value_native: float
    market_value_chf: float
    unrealized_gain_native: float
    unrealized_gain_chf: float
    gain_loss_pct: float | None
    gain_loss_updated_at: datetime | None
    notes: str | None


class PassiveFxError(CamelModel):
    currency: str
    error: str


class PassiveRollup(CamelModel):
    rows: list[PassiveInvestmentRollupRow]
    fx_errors: list[PassiveFxError]
    total_cost_basis_chf: float
    total_market_value_chf: float
    total_unrealized_gain_chf: float
