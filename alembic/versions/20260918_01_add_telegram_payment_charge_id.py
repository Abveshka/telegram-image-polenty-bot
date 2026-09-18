"""Add an idempotency key for Telegram Stars payments.

Revision ID: 20260918_01
Revises:
Create Date: 2026-09-18
"""

from alembic import op
import sqlalchemy as sa


revision = "20260918_01"
down_revision = None
branch_labels = None
depends_on = None

TABLE_NAME = "transactions"
COLUMN_NAME = "telegram_payment_charge_id"
INDEX_NAME = "ix_transactions_telegram_payment_charge_id"


def upgrade() -> None:
    """Update existing installations; remain safe for a freshly created DB."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if TABLE_NAME not in inspector.get_table_names():
        return

    columns = {column["name"] for column in inspector.get_columns(TABLE_NAME)}
    if COLUMN_NAME not in columns:
        op.add_column(TABLE_NAME, sa.Column(COLUMN_NAME, sa.String(length=128), nullable=True))

    indexes = {index["name"] for index in inspector.get_indexes(TABLE_NAME)}
    unique_constraints = {constraint["name"] for constraint in inspector.get_unique_constraints(TABLE_NAME)}
    if INDEX_NAME not in indexes and INDEX_NAME not in unique_constraints:
        op.create_index(INDEX_NAME, TABLE_NAME, [COLUMN_NAME], unique=True)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if TABLE_NAME not in inspector.get_table_names():
        return

    indexes = {index["name"] for index in inspector.get_indexes(TABLE_NAME)}
    if INDEX_NAME in indexes:
        op.drop_index(INDEX_NAME, table_name=TABLE_NAME)

    columns = {column["name"] for column in inspector.get_columns(TABLE_NAME)}
    if COLUMN_NAME in columns:
        op.drop_column(TABLE_NAME, COLUMN_NAME)
