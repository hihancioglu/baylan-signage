from sqlalchemy import Column, Integer, String, Boolean, DateTime, ForeignKey
from sqlalchemy.sql import func
from sqlalchemy.orm import relationship
from .db import Base


class Device(Base):
    __tablename__ = "devices"

    id = Column(Integer, primary_key=True)
    hostname = Column(String(128), unique=True, nullable=False)
    ip = Column(String(64))
    username = Column(String(128))
    department = Column(String(128))
    is_online = Column(Boolean, default=False)
    last_seen = Column(DateTime(timezone=True), server_default=func.now())


class Group(Base):
    __tablename__ = "groups"

    id = Column(Integer, primary_key=True)
    name = Column(String(128), unique=True, nullable=False)


class Playlist(Base):
    __tablename__ = "playlists"

    id = Column(Integer, primary_key=True)
    name = Column(String(128), unique=True, nullable=False)
    enabled = Column(Boolean, default=True)


class PlaylistItem(Base):
    __tablename__ = "playlist_items"

    id = Column(Integer, primary_key=True)
    playlist_id = Column(Integer, ForeignKey("playlists.id"))
    path = Column(String(512))
    order_no = Column(Integer, default=0)


class DeviceGroup(Base):
    __tablename__ = "device_groups"

    id = Column(Integer, primary_key=True)
    device_id = Column(Integer, ForeignKey("devices.id"))
    group_id = Column(Integer, ForeignKey("groups.id"))


class GroupPlaylist(Base):
    __tablename__ = "group_playlists"

    id = Column(Integer, primary_key=True)
    group_id = Column(Integer, ForeignKey("groups.id"))
    playlist_id = Column(Integer, ForeignKey("playlists.id"))
