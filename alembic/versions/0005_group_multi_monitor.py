"""add group monitor fields

Revision ID: 0005_group_multi_monitor
Revises: 0004_device_inventory_id
Create Date: 2026-03-25
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '0005_group_multi_monitor'
down_revision = '0004_device_inventory_id'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('groups') as batch_op:
        batch_op.add_column(sa.Column('monitor_count', sa.Integer(), nullable=False, server_default='1'))

    with op.batch_alter_table('group_playlists') as batch_op:
        batch_op.add_column(sa.Column('monitor_no', sa.Integer(), nullable=False, server_default='1'))


def downgrade() -> None:
    with op.batch_alter_table('group_playlists') as batch_op:
        batch_op.drop_column('monitor_no')

    with op.batch_alter_table('groups') as batch_op:
        batch_op.drop_column('monitor_count')
