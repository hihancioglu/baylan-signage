"""add mac address to devices

Revision ID: 0006_device_mac_address
Revises: 0005_group_multi_monitor
Create Date: 2026-07-07
"""

from alembic import op
import sqlalchemy as sa


revision = "0006_device_mac_address"
down_revision = "0005_group_multi_monitor"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("devices") as batch_op:
        batch_op.add_column(sa.Column("mac_address", sa.String(length=64), nullable=True))
        batch_op.create_index("ix_devices_hostname", ["hostname"], unique=False)
        batch_op.create_index("ix_devices_mac_address", ["mac_address"], unique=True)
        try:
            batch_op.drop_constraint("devices_hostname_key", type_="unique")
        except ValueError:
            pass


def downgrade() -> None:
    with op.batch_alter_table("devices") as batch_op:
        batch_op.drop_index("ix_devices_mac_address")
        batch_op.drop_index("ix_devices_hostname")
        batch_op.drop_column("mac_address")
