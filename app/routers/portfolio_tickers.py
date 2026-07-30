from fastapi import APIRouter, Depends
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.dependencies.auth import get_authenticated_user_id
from app.db.session import get_db
from app.exceptions import AppError, NotFoundError
from app.models import PortfolioTransaction, TickerMetadata
from app.schemas.portfolio import TickerCreateRequest, TickerDetail, TickerOut, TickerUpdateRequest
from app.services.ticker_detail import compute_ticker_detail
from app.services.validation import CURRENCIES, MARKETS, CATEGORIES, is_valid_category, is_valid_currency, is_valid_market

router = APIRouter(prefix="/portfolio/tickers", tags=["portfolio-tickers"])


@router.get("", response_model=list[TickerOut])
def list_tickers(
    db: Session = Depends(get_db),
    user_id: str = Depends(get_authenticated_user_id),
) -> list[TickerMetadata]:
    return db.query(TickerMetadata).filter(TickerMetadata.user_id == user_id).order_by(TickerMetadata.ticker.asc()).all()


@router.post("", response_model=TickerOut, status_code=201)
def create_ticker(
    body: TickerCreateRequest,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_authenticated_user_id),
) -> TickerMetadata:
    ticker = (body.ticker or "").strip().upper()
    market = (body.market or "").strip().upper()
    category = (body.category or "").strip()  # not uppercased
    native_currency = (body.native_currency or "").strip().upper()

    if not ticker:
        raise AppError(400, "ticker is required")
    if not is_valid_market(market):
        raise AppError(400, f"market must be one of: {', '.join(MARKETS)}")
    if not is_valid_category(category):
        raise AppError(400, f"category must be one of: {', '.join(CATEGORIES)}")
    if not is_valid_currency(native_currency):
        raise AppError(400, f"nativeCurrency must be one of: {', '.join(CURRENCIES)}")

    row = TickerMetadata(user_id=user_id, ticker=ticker, market=market, category=category, native_currency=native_currency)
    db.add(row)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        if "uq_ticker_metadata_user_id_ticker" in str(exc.orig):
            raise AppError(409, "This ticker is already registered") from exc
        raise AppError(500, "Failed to create ticker") from exc
    db.refresh(row)
    return row


@router.get("/{ticker}", response_model=TickerDetail)
def get_ticker_detail(
    ticker: str,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_authenticated_user_id),
) -> TickerDetail:
    # Fixed vs. the original: this route previously had no auth check at
    # all and never scoped by user — a cross-tenant data leak. Now requires
    # auth and always scopes by the authenticated user.
    ticker = ticker.upper()
    detail = compute_ticker_detail(db, ticker, user_id)
    if detail is None:
        raise NotFoundError(f"Ticker {ticker} is not registered")
    return detail


@router.put("/{ticker}", response_model=TickerOut)
def update_ticker(
    ticker: str,
    body: TickerUpdateRequest,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_authenticated_user_id),
) -> TickerMetadata:
    ticker = ticker.upper()
    existing = db.query(TickerMetadata).filter(TickerMetadata.ticker == ticker, TickerMetadata.user_id == user_id).first()
    if existing is None:
        raise NotFoundError(f"Ticker {ticker} is not registered")

    market = (body.market or "").strip().upper()
    category = (body.category or "").strip()
    native_currency = (body.native_currency or "").strip().upper()

    if not is_valid_market(market):
        raise AppError(400, f"market must be one of: {', '.join(MARKETS)}")
    if not is_valid_category(category):
        raise AppError(400, f"category must be one of: {', '.join(CATEGORIES)}")
    if not is_valid_currency(native_currency):
        raise AppError(400, f"nativeCurrency must be one of: {', '.join(CURRENCIES)}")

    if native_currency != existing.native_currency:
        txn_count = (
            db.query(PortfolioTransaction)
            .filter(PortfolioTransaction.user_id == user_id, PortfolioTransaction.ticker == ticker)
            .count()
        )
        if txn_count > 0:
            raise AppError(
                400,
                f"Cannot change currency — {txn_count} existing transaction(s) were entered in "
                f"{existing.native_currency}. Delete them first if the currency was genuinely wrong.",
            )

    existing.market = market
    existing.category = category
    existing.native_currency = native_currency
    db.commit()
    db.refresh(existing)
    return existing


@router.delete("/{ticker}")
def delete_ticker(
    ticker: str,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_authenticated_user_id),
) -> dict:
    ticker = ticker.upper()
    existing = db.query(TickerMetadata).filter(TickerMetadata.ticker == ticker, TickerMetadata.user_id == user_id).first()
    if existing is None:
        raise NotFoundError(f"Ticker {ticker} is not registered")

    # No DB-level FK between TickerMetadata and PortfolioTransaction (loosely
    # coupled by string ticker) — explicit two-step delete instead.
    db.query(PortfolioTransaction).filter(PortfolioTransaction.user_id == user_id, PortfolioTransaction.ticker == ticker).delete()
    db.delete(existing)
    db.commit()
    return {"success": True}
