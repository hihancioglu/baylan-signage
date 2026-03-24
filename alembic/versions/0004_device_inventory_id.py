"""add inventory id to devices

Revision ID: 0004_device_inventory_id
Revises: 0003_call_requests
Create Date: 2026-03-24 00:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0004_device_inventory_id"
down_revision: Union[str, Sequence[str], None] = "0003_call_requests"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("devices", sa.Column("inventory_id", sa.String(length=128), nullable=True))


def downgrade() -> None:
    op.drop_column("devices", "inventory_id")
