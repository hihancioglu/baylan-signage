"""add call requests table

Revision ID: 0003_call_requests
Revises: 0002_playlist_item_widgets
Create Date: 2026-03-18 00:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0003_call_requests"
down_revision: Union[str, Sequence[str], None] = "0002_playlist_item_widgets"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "call_requests",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("hostname", sa.String(length=128), nullable=False),
        sa.Column("requested_role", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="active"),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_call_requests_hostname", "call_requests", ["hostname"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_call_requests_hostname", table_name="call_requests")
    op.drop_table("call_requests")
