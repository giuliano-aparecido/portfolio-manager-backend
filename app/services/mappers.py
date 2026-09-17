"""Shared ORM-row -> pure-dataclass mappers, kept out of the framework-
agnostic services (fifo.py, ledger.py) so those stay unit-testable without
any DB/ORM involved.
"""

from app.models import PortfolioTransaction
from app.services.fifo import ProcessedTransaction


def portfolio_transaction_to_processed(t: PortfolioTransaction) -> ProcessedTransaction:
    return ProcessedTransaction(
        ticker=t.ticker,
        date=t.date,
        type=t.type,
        native_currency=t.native_currency,
        fx_rate_to_chf=t.fx_rate_to_chf,
        quantity=t.quantity,
        price_per_share=t.price_per_share,
        cash_amount=t.cash_amount,
        notes=t.notes,
        id=t.id,
    )
