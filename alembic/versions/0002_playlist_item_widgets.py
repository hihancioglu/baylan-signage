"""add widget fields to playlist items

Revision ID: 0002_playlist_item_widgets
Revises: 0001_initial_schema
Create Date: 2026-03-12 00:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0002_playlist_item_widgets"
down_revision: Union[str, Sequence[str], None] = "0001_initial_schema"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "playlist_items",
        sa.Column("item_type", sa.String(length=32), nullable=False, server_default="media"),
    )
    op.add_column("playlist_items", sa.Column("widget_id", sa.Integer(), nullable=True))
    op.add_column("playlist_items", sa.Column("widget_payload", sa.Text(), nullable=True))
    op.add_column("playlist_items", sa.Column("widget_url", sa.String(length=1024), nullable=True))


def downgrade() -> None:
    op.drop_column("playlist_items", "widget_url")
    op.drop_column("playlist_items", "widget_payload")
    op.drop_column("playlist_items", "widget_id")
    op.drop_column("playlist_items", "item_type")
