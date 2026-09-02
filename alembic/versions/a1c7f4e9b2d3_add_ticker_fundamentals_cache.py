"""add ticker_fundamentals_cache

Revision ID: a1c7f4e9b2d3
Revises: 11d221220fb5
Create Date: 2026-09-02 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'a1c7f4e9b2d3'
down_revision: Union[str, Sequence[str], None] = '11d221220fb5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'ticker_fundamentals_cache',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('provider', sa.String(), nullable=False),
        sa.Column('yahoo_symbol', sa.String(), nullable=False),
        sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('unavailable', sa.Boolean(), nullable=False),
        sa.Column('as_of_date', sa.Date(), nullable=True),
        sa.Column('fetched_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('fetch_error', sa.String(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_ticker_fundamentals_cache')),
        sa.UniqueConstraint(
            'provider', 'yahoo_symbol', name='uq_ticker_fundamentals_cache_provider_yahoo_symbol'
        ),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('ticker_fundamentals_cache')
