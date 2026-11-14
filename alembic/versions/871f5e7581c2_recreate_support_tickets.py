"""recreate support_tickets

Revision ID: 871f5e7581c2
Revises: 64cd81926291
Create Date: 2026-11-14 09:30:51.475004
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '871f5e7581c2'
down_revision = '64cd81926291'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_table("support_tickets")
    op.create_table(
        "support_tickets",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("topic_id", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(16), server_default="open", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_support_tickets_user_id", "support_tickets", ["user_id"], unique=True)
    op.create_index("ix_support_tickets_topic_id", "support_tickets", ["topic_id"], unique=True)


def downgrade() -> None:
    op.drop_table("support_tickets")
