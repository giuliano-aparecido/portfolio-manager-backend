"""add payload_version to ticker_fundamentals_cache

Revision ID: b2d8e0f1c3a4
Revises: a1c7f4e9b2d3
Create Date: 2026-09-02 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b2d8e0f1c3a4'
down_revision: Union[str, Sequence[str], None] = 'a1c7f4e9b2d3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Nullable, no backfill: existing rows stay NULL, which cache.py reads
    # as "older than the current shape" and refetches on next access.
    op.add_column('ticker_fundamentals_cache', sa.Column('payload_version', sa.Integer(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('ticker_fundamentals_cache', 'payload_version')
