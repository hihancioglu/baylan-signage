"""initial schema

Revision ID: 0001_initial_schema
Revises: 
Create Date: 2026-02-26 00:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0001_initial_schema"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "devices",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("hostname", sa.String(length=128), nullable=False, unique=True),
        sa.Column("alias", sa.String(length=128), nullable=True),
        sa.Column("ip", sa.String(length=64), nullable=True),
        sa.Column("username", sa.String(length=128), nullable=True),
        sa.Column("department", sa.String(length=128), nullable=True),
        sa.Column("is_online", sa.Boolean(), nullable=True),
        sa.Column("last_seen", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.Column("agent_version", sa.String(length=64), nullable=True),
        sa.Column("os_version", sa.String(length=128), nullable=True),
        sa.Column("last_error", sa.String(length=512), nullable=True),
        sa.Column("last_state", sa.String(length=64), nullable=True),
    )

    op.create_table(
        "groups",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=128), nullable=False, unique=True),
    )

    op.create_table(
        "playlists",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=128), nullable=False, unique=True),
        sa.Column("enabled", sa.Boolean(), nullable=True),
        sa.Column("type", sa.String(length=32), nullable=False, server_default="normal"),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=True),
        sa.Column("valid_to", sa.DateTime(timezone=True), nullable=True),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("loop_mode", sa.String(length=32), nullable=False, server_default="sequential"),
    )

    op.create_table(
        "group_playlists",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("group_id", sa.Integer(), sa.ForeignKey("groups.id"), nullable=True),
        sa.Column("playlist_id", sa.Integer(), sa.ForeignKey("playlists.id"), nullable=True),
    )

    op.create_table(
        "playlist_items",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("playlist_id", sa.Integer(), sa.ForeignKey("playlists.id"), nullable=True),
        sa.Column("path", sa.String(length=512), nullable=True),
        sa.Column("order_no", sa.Integer(), nullable=True),
        sa.Column("media_type", sa.String(length=64), nullable=True, server_default="video"),
        sa.Column("duration_sec", sa.Integer(), nullable=True),
        sa.Column("checksum", sa.String(length=128), nullable=True),
        sa.Column("source_url", sa.String(length=1024), nullable=True),
    )

    op.create_table(
        "device_groups",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("device_id", sa.Integer(), sa.ForeignKey("devices.id"), nullable=True),
        sa.Column("group_id", sa.Integer(), sa.ForeignKey("groups.id"), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("assigned_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("unassigned_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_table(
        "command_logs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("command_id", sa.String(length=64), nullable=False, unique=True),
        sa.Column("command_type", sa.String(length=64), nullable=False),
        sa.Column("target_type", sa.String(length=16), nullable=False),
        sa.Column("target_value", sa.String(length=128), nullable=False),
        sa.Column("ttl_sec", sa.Integer(), nullable=False, server_default="30"),
        sa.Column("payload", sa.String(length=2048), nullable=True),
        sa.Column("expected_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    op.create_table(
        "command_acks",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("command_id", sa.String(length=64), nullable=False),
        sa.Column("hostname", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="ok"),
        sa.Column("error_detail", sa.String(length=1024), nullable=True),
        sa.Column("ack_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("command_acks")
    op.drop_table("command_logs")
    op.drop_table("device_groups")
    op.drop_table("playlist_items")
    op.drop_table("group_playlists")
    op.drop_table("playlists")
    op.drop_table("groups")
    op.drop_table("devices")
