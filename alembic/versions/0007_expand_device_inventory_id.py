"""expand device inventory id for comma-separated values

Revision ID: 0007_expand_device_inventory_id
Revises: 0006_device_mac_address
Create Date: 2026-09-24
"""

from alembic import op
import sqlalchemy as sa


revision = "0007_expand_device_inventory_id"
down_revision = "0006_device_mac_address"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("devices") as batch_op:
        batch_op.alter_column(
            "inventory_id",
            existing_type=sa.String(length=128),
            type_=sa.String(length=1024),
            existing_nullable=True,
        )


def downgrade() -> None:
    with op.batch_alter_table("devices") as batch_op:
        batch_op.alter_column(
            "inventory_id",
            existing_type=sa.String(length=1024),
            type_=sa.String(length=128),
            existing_nullable=True,
        )
