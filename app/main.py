import eventlet
eventlet.monkey_patch()

import hashlib
import itertools
import threading
import time
from datetime import datetime, timezone, timedelta

from flask import Flask, request, jsonify
from flask_socketio import SocketIO, emit, join_room

from .db import Base, engine, SessionLocal
from .models import (
    Device,
    Group,
    Playlist,
    PlaylistItem,
    DeviceGroup,
    GroupPlaylist
)
from .config import SHARED_SECRET


app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*")

Base.metadata.create_all(bind=engine)

connected = {}      # hostname -> sid
sid_to_host = {}    # sid -> hostname
command_seq = itertools.count(1)

COMMAND_TYPES = {
    "REFRESH_CONFIG",
    "EMERGENCY_START",
    "EMERGENCY_STOP",
    "RESTART_AGENT",
    "PING",
}


@app.route("/")
def index():
    return "Signage Server Running"


def db_session():
    return SessionLocal()


def _utc_now_iso():
    return datetime.now(timezone.utc).isoformat()


def build_command_contract(command_type: str, payload=None, ttl_sec=30, priority=5):
    if command_type not in COMMAND_TYPES:
        raise ValueError(f"unsupported command_type={command_type}")

    return {
        "type": command_type,
        "command_id": f"cmd-{next(command_seq)}",
        "issued_at": _utc_now_iso(),
        "ttl_sec": int(ttl_sec),
        "payload": payload or {},
        "priority": int(priority),
    }


def join_group_rooms(db, sid, hostname):
    device = db.query(Device).filter_by(hostname=hostname).first()
    if not device:
        return

    memberships = db.query(DeviceGroup).filter_by(device_id=device.id).all()
    for membership in memberships:
        join_room(f"group:{membership.group_id}")


# ------------------------------------------------
# CONFIG BUILD
# ------------------------------------------------
def build_config(hostname):
    db = db_session()
    try:
        device = db.query(Device).filter_by(hostname=hostname).first()
        if not device:
            return {"enabled": False, "videos": []}

        dg = db.query(DeviceGroup).filter_by(device_id=device.id).first()
        if not dg:
            return {"enabled": False, "videos": []}

        gp = db.query(GroupPlaylist).filter_by(group_id=dg.group_id).first()
        if not gp:
            return {"enabled": False, "videos": []}

        playlist = db.query(Playlist).filter_by(id=gp.playlist_id).first()
        if not playlist or not playlist.enabled:
            return {"enabled": False, "videos": []}

        items = (
            db.query(PlaylistItem)
            .filter_by(playlist_id=playlist.id)
            .order_by(PlaylistItem.order_no)
            .all()
        )

        videos = [i.path for i in items]
        media_signatures = {
            path: hashlib.sha256(path.encode("utf-8")).hexdigest() for path in videos
        }
        playlist_version = hashlib.sha256(
            f"{playlist.id}:{'|'.join(videos)}".encode("utf-8")
        ).hexdigest()[:16]

        return {
            "enabled": True,
            "videos": videos,
            "playlist_version": playlist_version,
            "media_signatures": media_signatures,
        }

    finally:
        db.close()


# ------------------------------------------------
# SOCKET EVENTS
# ------------------------------------------------
@socketio.on("connect")
def handle_connect():
    emit("hello", {"msg": "connected"})


@socketio.on("register")
def handle_register(data):
    if data.get("secret") != SHARED_SECRET:
        emit("error", {"msg": "unauthorized"})
        return

    hostname = data.get("hostname")
    if not hostname:
        return

    connected[hostname] = request.sid
    sid_to_host[request.sid] = hostname
    join_room(f"device:{hostname}")

    db = db_session()
    try:
        device = db.query(Device).filter_by(hostname=hostname).first()
        if not device:
            device = Device(hostname=hostname)

        device.ip = data.get("ip")
        device.username = data.get("username")
        device.department = data.get("department")
        device.is_online = True
        device.last_seen = datetime.utcnow()

        db.add(device)
        db.commit()

        join_group_rooms(db, request.sid, hostname)
    finally:
        db.close()

    emit("config", build_config(hostname))


@socketio.on("heartbeat")
def handle_heartbeat(data):
    hostname = data.get("hostname")
    if not hostname:
        return

    db = db_session()
    try:
        device = db.query(Device).filter_by(hostname=hostname).first()
        if device:
            device.last_seen = datetime.utcnow()
            device.is_online = True
            db.commit()
    finally:
        db.close()

@socketio.on("disconnect")
def handle_disconnect():
    sid = request.sid
    hostname = sid_to_host.pop(sid, None)
    if not hostname:
        return

    connected.pop(hostname, None)

    db = db_session()
    try:
        device = db.query(Device).filter_by(hostname=hostname).first()
        if device:
            device.is_online = False
            db.commit()
    finally:
        db.close()


@socketio.on("pull_config")
def handle_pull_config(data):
    hostname = (data or {}).get("hostname")
    if not hostname:
        return

    emit("config", build_config(hostname), room=request.sid)


@socketio.on("command_ack")
def handle_command_ack(data):
    hostname = sid_to_host.get(request.sid)
    print(f"🧾 command_ack hostname={hostname} data={data}")

# ------------------------------------------------
# OFFLINE CHECKER
# ------------------------------------------------
def offline_checker():
    while True:
        db = db_session()
        try:
            devices = db.query(Device).all()
            now = datetime.utcnow()

            for d in devices:
                if d.last_seen:
                    delta = now - d.last_seen
                    if delta > timedelta(minutes=5):
                        d.is_online = False

            db.commit()
        finally:
            db.close()

        time.sleep(60)


threading.Thread(target=offline_checker, daemon=True).start()


# ------------------------------------------------
# REST API
# ------------------------------------------------
@app.get("/api/devices")
def list_devices():
    if request.headers.get("X-SECRET") != SHARED_SECRET:
        return jsonify({"error": "unauthorized"}), 401

    db = db_session()
    try:
        devices = db.query(Device).all()
        return jsonify([
            {
                "hostname": d.hostname,
                "ip": d.ip,
                "department": d.department,
                "username": d.username,
                "is_online": d.is_online,
                "last_seen": d.last_seen.isoformat() if d.last_seen else None
            }
            for d in devices
        ])
    finally:
        db.close()

@app.get("/api/groups")
def list_groups():
    if request.headers.get("X-SECRET") != SHARED_SECRET:
        return jsonify({"error": "unauthorized"}), 401

    db = db_session()
    try:
        groups = db.query(Group).all()
        return jsonify([
            {"id": g.id, "name": g.name}
            for g in groups
        ])
    finally:
        db.close()

@app.get("/api/playlists")
def list_playlists():
    if request.headers.get("X-SECRET") != SHARED_SECRET:
        return jsonify({"error": "unauthorized"}), 401

    db = db_session()
    try:
        playlists = db.query(Playlist).all()
        return jsonify([
            {"id": p.id, "name": p.name, "enabled": bool(p.enabled)}
            for p in playlists
        ])
    finally:
        db.close()

@app.post("/api/device/<hostname>/group/<int:group_id>")
def bind_device_group(hostname, group_id):
    if request.headers.get("X-SECRET") != SHARED_SECRET:
        return jsonify({"error": "unauthorized"}), 401

    db = db_session()
    try:
        device = db.query(Device).filter_by(hostname=hostname).first()
        if not device:
            return jsonify({"error": "device not found"}), 404

        # varsa tekrar ekleme
        exists = db.query(DeviceGroup).filter_by(device_id=device.id, group_id=group_id).first()
        if not exists:
            db.add(DeviceGroup(device_id=device.id, group_id=group_id))
            db.commit()

        sid = connected.get(hostname)
        if sid:
            socketio.server.enter_room(sid, f"group:{group_id}")

        return jsonify({"ok": True})
    finally:
        db.close()

@app.post("/api/group/<int:group_id>/playlist/<int:playlist_id>")
def bind_group_playlist(group_id, playlist_id):
    if request.headers.get("X-SECRET") != SHARED_SECRET:
        return jsonify({"error": "unauthorized"}), 401

    db = db_session()
    try:
        exists = db.query(GroupPlaylist).filter_by(group_id=group_id, playlist_id=playlist_id).first()
        if not exists:
            db.add(GroupPlaylist(group_id=group_id, playlist_id=playlist_id))
            db.commit()

        return jsonify({"ok": True})
    finally:
        db.close()

@app.post("/api/groups")
def create_group():
    if request.headers.get("X-SECRET") != SHARED_SECRET:
        return jsonify({"error": "unauthorized"}), 401

    name = (request.json or {}).get("name")
    if not name:
        return jsonify({"error": "name required"}), 400

    db = db_session()
    try:
        existing = db.query(Group).filter_by(name=name).first()
        if existing:
            return jsonify({"ok": True, "id": existing.id, "already_exists": True})

        g = Group(name=name)
        db.add(g)
        db.commit()
        return jsonify({"ok": True, "id": g.id, "already_exists": False})
    finally:
        db.close()

@app.post("/api/playlists")
def create_playlist():
    if request.headers.get("X-SECRET") != SHARED_SECRET:
        return jsonify({"error": "unauthorized"}), 401

    name = request.json.get("name")
    enabled = request.json.get("enabled", True)

    db = db_session()
    try:
        pl = Playlist(name=name, enabled=enabled)
        db.add(pl)
        db.commit()
        return jsonify({"ok": True, "id": pl.id})
    finally:
        db.close()


@app.post("/api/playlists/<int:playlist_id>/items")
def add_playlist_item(playlist_id):
    if request.headers.get("X-SECRET") != SHARED_SECRET:
        return jsonify({"error": "unauthorized"}), 401

    path = request.json.get("path")
    order_no = request.json.get("order_no", 0)

    db = db_session()
    try:
        item = PlaylistItem(
            playlist_id=playlist_id,
            path=path,
            order_no=order_no
        )
        db.add(item)
        db.commit()
        return jsonify({"ok": True})
    finally:
        db.close()


@app.post("/api/push/<hostname>")
def push_refresh(hostname):
    if request.headers.get("X-SECRET") != SHARED_SECRET:
        return jsonify({"error": "unauthorized"}), 401

    cmd = build_command_contract("REFRESH_CONFIG", payload={}, ttl_sec=30, priority=3)
    socketio.emit("command", cmd, room=f"device:{hostname}")
    return jsonify({"ok": True, "command": cmd})


@app.post("/api/command/device/<hostname>")
def push_device_command(hostname):
    if request.headers.get("X-SECRET") != SHARED_SECRET:
        return jsonify({"error": "unauthorized"}), 401

    body = request.json or {}
    command_type = body.get("type")
    if command_type not in COMMAND_TYPES:
        return jsonify({"error": "unsupported command type"}), 400

    cmd = build_command_contract(
        command_type=command_type,
        payload=body.get("payload") or {},
        ttl_sec=body.get("ttl_sec", 30),
        priority=body.get("priority", 5),
    )
    socketio.emit("command", cmd, room=f"device:{hostname}")
    return jsonify({"ok": True, "command": cmd})


@app.post("/api/command/group/<int:group_id>")
def push_group_command(group_id):
    if request.headers.get("X-SECRET") != SHARED_SECRET:
        return jsonify({"error": "unauthorized"}), 401

    body = request.json or {}
    command_type = body.get("type")
    if command_type not in COMMAND_TYPES:
        return jsonify({"error": "unsupported command type"}), 400

    cmd = build_command_contract(
        command_type=command_type,
        payload=body.get("payload") or {},
        ttl_sec=body.get("ttl_sec", 30),
        priority=body.get("priority", 5),
    )
    socketio.emit("command", cmd, room=f"group:{group_id}")
    return jsonify({"ok": True})


if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=5050)
