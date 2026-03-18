from sqlalchemy import Column, Integer, String, Boolean, DateTime, ForeignKey, Text
from sqlalchemy.sql import func
from .db import Base


class Device(Base):
    __tablename__ = "devices"

    id = Column(Integer, primary_key=True)
    hostname = Column(String(128), unique=True, nullable=False)
    alias = Column(String(128))
    ip = Column(String(64))
    username = Column(String(128))
    department = Column(String(128))
    is_online = Column(Boolean, default=False)
    last_seen = Column(DateTime(timezone=True), server_default=func.now())
    agent_version = Column(String(64))
    updater_version = Column(String(64))
    os_version = Column(String(128))
    last_error = Column(String(512))
    last_state = Column(String(64))
    last_state_at = Column(DateTime(timezone=True))
    last_content_name = Column(String(255))
    idle_mode_enabled = Column(Boolean)
    content_enabled = Column(Boolean)
    last_client_update_status = Column(String(256))
    last_client_updater_status = Column(String(256))


class Group(Base):
    __tablename__ = "groups"

    id = Column(Integer, primary_key=True)
    name = Column(String(128), unique=True, nullable=False)
    idle_timeout_sec = Column(Integer)
    idle_mode_enabled = Column(Boolean, default=True, nullable=False)
    content_enabled = Column(Boolean, default=True, nullable=False)


class Playlist(Base):
    __tablename__ = "playlists"

    id = Column(Integer, primary_key=True)
    name = Column(String(128), unique=True, nullable=False)
    enabled = Column(Boolean, default=True)
    type = Column(String(32), default="normal", nullable=False)
    valid_from = Column(DateTime(timezone=True))
    valid_to = Column(DateTime(timezone=True))
    priority = Column(Integer, default=0, nullable=False)
    loop_mode = Column(String(32), default="sequential", nullable=False)


class PlaylistItem(Base):
    __tablename__ = "playlist_items"

    id = Column(Integer, primary_key=True)
    playlist_id = Column(Integer, ForeignKey("playlists.id"))
    path = Column(String(512))
    order_no = Column(Integer, default=0)
    media_type = Column(String(64), default="video")
    item_type = Column(String(32), default="media", nullable=False)
    duration_sec = Column(Integer)
    checksum = Column(String(128))
    source_url = Column(String(1024))
    widget_id = Column(Integer)
    widget_payload = Column(Text)
    widget_url = Column(String(1024))


class MediaAsset(Base):
    __tablename__ = "media_assets"

    id = Column(Integer, primary_key=True)
    original_name = Column(String(255), nullable=False)
    stored_name = Column(String(255), nullable=False)
    relative_path = Column(String(1024), unique=True, nullable=False)
    content_type = Column(String(128))
    file_size = Column(Integer)
    checksum = Column(String(128))
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class AppSetting(Base):
    __tablename__ = "app_settings"

    key = Column(String(128), primary_key=True)
    value = Column(String(2048))


class DeviceGroup(Base):
    __tablename__ = "device_groups"

    id = Column(Integer, primary_key=True)
    device_id = Column(Integer, ForeignKey("devices.id"))
    group_id = Column(Integer, ForeignKey("groups.id"))
    is_active = Column(Boolean, default=True, nullable=False)
    assigned_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    unassigned_at = Column(DateTime(timezone=True))


class GroupPlaylist(Base):
    __tablename__ = "group_playlists"

    id = Column(Integer, primary_key=True)
    group_id = Column(Integer, ForeignKey("groups.id"))
    playlist_id = Column(Integer, ForeignKey("playlists.id"))


class CommandLog(Base):
    __tablename__ = "command_logs"

    id = Column(Integer, primary_key=True)
    command_id = Column(String(64), unique=True, nullable=False)
    command_type = Column(String(64), nullable=False)
    target_type = Column(String(16), nullable=False)
    target_value = Column(String(128), nullable=False)
    ttl_sec = Column(Integer, default=30, nullable=False)
    payload = Column(String(2048))
    expected_count = Column(Integer, default=0, nullable=False)
    sent_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class CommandAck(Base):
    __tablename__ = "command_acks"

    id = Column(Integer, primary_key=True)
    command_id = Column(String(64), nullable=False)
    hostname = Column(String(128), nullable=False)
    status = Column(String(32), default="ok", nullable=False)
    error_detail = Column(String(1024))
    ack_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class Announcement(Base):
    __tablename__ = "announcements"

    id = Column(Integer, primary_key=True)
    title = Column(String(128), nullable=False)
    message = Column(String(1024), nullable=False)
    target_type = Column(String(16), default="group", nullable=False)
    target_value = Column(String(128), nullable=False)
    ttl_sec = Column(Integer, default=120, nullable=False)
    is_persistent = Column(Boolean, default=False, nullable=False)
    display_mode = Column(String(16), default="normal", nullable=False)
    is_active = Column(Boolean, default=False, nullable=False)
    published_at = Column(DateTime(timezone=True))
    unpublished_at = Column(DateTime(timezone=True))
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class CallRequest(Base):
    __tablename__ = "call_requests"

    id = Column(Integer, primary_key=True)
    hostname = Column(String(128), nullable=False, index=True)
    requested_role = Column(String(64), nullable=False)
    status = Column(String(32), default="active", nullable=False)
    requested_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    closed_at = Column(DateTime(timezone=True))
