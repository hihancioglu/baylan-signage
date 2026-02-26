import eventlet
eventlet.monkey_patch()

import hashlib
import itertools
import json
import threading
import time
from datetime import datetime, timezone, timedelta

from flask import Flask, request, jsonify, render_template
from flask_socketio import SocketIO, emit, join_room
from sqlalchemy import func

from .db import SessionLocal, ensure_sqlite_schema
from .models import (
    Device,
    Group,
    Playlist,
    PlaylistItem,
    DeviceGroup,
    GroupPlaylist,
    CommandLog,
    CommandAck,
)
from .config import SHARED_SECRET


app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*")

ensure_sqlite_schema()

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


@app.get("/panel")
def panel():
    return render_template("panel.html")


def db_session():
    return SessionLocal()


def _utc_now_iso():
    return datetime.now(timezone.utc).isoformat()


def _auth_failed():
    return request.headers.get("X-SECRET") != SHARED_SECRET


def _serialize_device(db, device):
    active_group = (
        db.query(Group.name)
        .join(DeviceGroup, DeviceGroup.group_id == Group.id)
        .filter(DeviceGroup.device_id == device.id, DeviceGroup.is_active.is_(True))
        .order_by(DeviceGroup.assigned_at.desc())
        .first()
    )
    return {
        "hostname": device.hostname,
        "ip": device.ip,
        "department": device.department,
        "username": device.username,
        "is_online": bool(device.is_online),
        "last_seen": device.last_seen.isoformat() if device.last_seen else None,
        "last_state": device.last_state,
        "group": active_group[0] if active_group else None,
    }


def _target_hostnames(db, target_type, target_value):
    if target_type == "all":
        return [d.hostname for d in db.query(Device).all()]
    if target_type == "group":
        rows = (
            db.query(Device.hostname)
            .join(DeviceGroup, DeviceGroup.device_id == Device.id)
            .filter(DeviceGroup.group_id == int(target_value), DeviceGroup.is_active.is_(True))
            .all()
        )
        return [r[0] for r in rows]
    if target_type == "device":
        return [str(target_value)]
    if target_type == "devices":
        return [h for h in (target_value or []) if h]
    return []


def _emit_command(db, cmd, target_type, target_value):
    target_hostnames = _target_hostnames(db, target_type, target_value)
    if target_type == "all":
        socketio.emit("command", cmd)
    elif target_type == "group":
        socketio.emit("command", cmd, room=f"group:{target_value}")
    elif target_type == "device":
        socketio.emit("command", cmd, room=f"device:{target_value}")
    elif target_type == "devices":
        for hostname in target_hostnames:
            socketio.emit("command", cmd, room=f"device:{hostname}")

    log = CommandLog(
        command_id=cmd["command_id"],
        command_type=cmd["type"],
        target_type=target_type,
        target_value=json.dumps(target_value) if isinstance(target_value, list) else str(target_value),
        ttl_sec=cmd["ttl_sec"],
        payload=json.dumps(cmd.get("payload") or {}),
        expected_count=len(set(target_hostnames)),
    )
    db.add(log)
    db.commit()


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

    memberships = db.query(DeviceGroup).filter_by(device_id=device.id, is_active=True).all()
    for membership in memberships:
        join_room(f"group:{membership.group_id}")


def build_config(hostname):
    db = db_session()
    try:
        device = db.query(Device).filter_by(hostname=hostname).first()
        if not device:
            return {"enabled": False, "videos": []}

        dg = db.query(DeviceGroup).filter_by(device_id=device.id, is_active=True).first()
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
        device.last_state = data.get("state") or device.last_state
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
            device.last_state = data.get("state") or device.last_state
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
    hostname = sid_to_host.get(request.sid) or (data or {}).get("hostname")
    command_id = (data or {}).get("command_id")
    if not hostname or not command_id:
        return

    db = db_session()
    try:
        status = (data or {}).get("status") or "ok"
        error_detail = (data or {}).get("error")
        exists = db.query(CommandAck).filter_by(command_id=command_id, hostname=hostname).first()
        if not exists:
            db.add(CommandAck(command_id=command_id, hostname=hostname, status=status, error_detail=error_detail))
            db.commit()
    finally:
        db.close()


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


@app.get("/api/devices")
def list_devices():
    if _auth_failed():
        return jsonify({"error": "unauthorized"}), 401

    db = db_session()
    try:
        devices = db.query(Device).all()
        return jsonify([_serialize_device(db, d) for d in devices])
    finally:
        db.close()


@app.post("/api/devices/<hostname>/group/<int:group_id>")
def bind_device_group(hostname, group_id):
    if _auth_failed():
        return jsonify({"error": "unauthorized"}), 401

    db = db_session()
    try:
        device = db.query(Device).filter_by(hostname=hostname).first()
        if not device:
            return jsonify({"error": "device not found"}), 404

        db.query(DeviceGroup).filter_by(device_id=device.id, is_active=True).update(
            {"is_active": False, "unassigned_at": datetime.utcnow()}
        )
        db.add(DeviceGroup(device_id=device.id, group_id=group_id, is_active=True))
        db.commit()

        sid = connected.get(hostname)
        if sid:
            socketio.server.enter_room(sid, f"group:{group_id}")

        return jsonify({"ok": True})
    finally:
        db.close()


@app.get("/api/groups")
def list_groups():
    if _auth_failed():
        return jsonify({"error": "unauthorized"}), 401

    db = db_session()
    try:
        groups = db.query(Group).all()
        result = []
        for g in groups:
            active_playlist = (
                db.query(Playlist.id, Playlist.name)
                .join(GroupPlaylist, GroupPlaylist.playlist_id == Playlist.id)
                .filter(GroupPlaylist.group_id == g.id)
                .first()
            )
            result.append(
                {
                    "id": g.id,
                    "name": g.name,
                    "playlist": {
                        "id": active_playlist[0],
                        "name": active_playlist[1],
                    } if active_playlist else None,
                }
            )
        return jsonify(result)
    finally:
        db.close()


@app.post("/api/groups")
def create_group():
    if _auth_failed():
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


@app.post("/api/groups/<int:group_id>/playlist/<int:playlist_id>")
def bind_group_playlist(group_id, playlist_id):
    if _auth_failed():
        return jsonify({"error": "unauthorized"}), 401

    db = db_session()
    try:
        db.query(GroupPlaylist).filter_by(group_id=group_id).delete()
        db.add(GroupPlaylist(group_id=group_id, playlist_id=playlist_id))
        db.commit()
        return jsonify({"ok": True})
    finally:
        db.close()


@app.get("/api/playlists")
def list_playlists():
    if _auth_failed():
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


@app.post("/api/playlists")
def create_playlist():
    if _auth_failed():
        return jsonify({"error": "unauthorized"}), 401

    name = (request.json or {}).get("name")
    enabled = (request.json or {}).get("enabled", True)

    db = db_session()
    try:
        pl = Playlist(name=name, enabled=enabled)
        db.add(pl)
        db.commit()
        return jsonify({"ok": True, "id": pl.id})
    finally:
        db.close()


@app.get("/api/playlists/<int:playlist_id>/items")
def list_playlist_items(playlist_id):
    if _auth_failed():
        return jsonify({"error": "unauthorized"}), 401

    db = db_session()
    try:
        items = (
            db.query(PlaylistItem)
            .filter_by(playlist_id=playlist_id)
            .order_by(PlaylistItem.order_no.asc())
            .all()
        )
        return jsonify([
            {"id": i.id, "path": i.path, "order_no": i.order_no}
            for i in items
        ])
    finally:
        db.close()


@app.post("/api/playlists/<int:playlist_id>/items")
def add_playlist_item(playlist_id):
    if _auth_failed():
        return jsonify({"error": "unauthorized"}), 401

    path = (request.json or {}).get("path")
    order_no = (request.json or {}).get("order_no", 0)

    db = db_session()
    try:
        item = PlaylistItem(playlist_id=playlist_id, path=path, order_no=order_no)
        db.add(item)
        db.commit()
        return jsonify({"ok": True})
    finally:
        db.close()


@app.post("/api/playlists/<int:playlist_id>/items/reorder")
def reorder_playlist_items(playlist_id):
    if _auth_failed():
        return jsonify({"error": "unauthorized"}), 401

    order_ids = (request.json or {}).get("item_ids") or []
    db = db_session()
    try:
        items = db.query(PlaylistItem).filter_by(playlist_id=playlist_id).all()
        by_id = {i.id: i for i in items}
        for idx, item_id in enumerate(order_ids):
            if item_id in by_id:
                by_id[item_id].order_no = idx
        db.commit()
        return jsonify({"ok": True})
    finally:
        db.close()


@app.post("/api/announcements/push")
def push_announcement():
    if _auth_failed():
        return jsonify({"error": "unauthorized"}), 401

    body = request.json or {}
    target = body.get("target") or {}
    target_type = target.get("type")
    target_value = target.get("value")
    message = body.get("message")
    ttl_sec = int(body.get("ttl_sec", 120))

    if target_type not in {"all", "group", "device", "devices"}:
        return jsonify({"error": "invalid target"}), 400

    cmd = build_command_contract(
        command_type="EMERGENCY_START",
        payload={"message": message, "preview": body.get("preview", message)},
        ttl_sec=ttl_sec,
        priority=10,
    )

    db = db_session()
    try:
        _emit_command(db, cmd, target_type, target_value)
    finally:
        db.close()

    return jsonify({"ok": True, "command": cmd})


@app.get("/api/commands/logs")
def command_logs():
    if _auth_failed():
        return jsonify({"error": "unauthorized"}), 401

    db = db_session()
    try:
        logs = db.query(CommandLog).order_by(CommandLog.sent_at.desc()).limit(100).all()
        result = []
        for l in logs:
            ack_count = db.query(func.count(CommandAck.id)).filter_by(command_id=l.command_id).scalar() or 0
            errors = (
                db.query(CommandAck.hostname, CommandAck.error_detail)
                .filter(CommandAck.command_id == l.command_id, CommandAck.status != "ok")
                .all()
            )
            ack_ratio = (ack_count / l.expected_count) if l.expected_count else 0
            result.append(
                {
                    "command_id": l.command_id,
                    "type": l.command_type,
                    "target": {"type": l.target_type, "value": l.target_value},
                    "sent_at": l.sent_at.isoformat() if l.sent_at else None,
                    "ack_count": ack_count,
                    "expected_count": l.expected_count,
                    "ack_ratio": ack_ratio,
                    "errors": [{"hostname": e[0], "detail": e[1]} for e in errors],
                }
            )
        return jsonify(result)
    finally:
        db.close()


@app.post("/api/commands/device/<hostname>")
def push_device_command(hostname):
    if _auth_failed():
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

    db = db_session()
    try:
        _emit_command(db, cmd, "device", hostname)
    finally:
        db.close()
    return jsonify({"ok": True, "command": cmd})


@app.post("/api/commands/group/<int:group_id>")
def push_group_command(group_id):
    if _auth_failed():
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

    db = db_session()
    try:
        _emit_command(db, cmd, "group", group_id)
    finally:
        db.close()
    return jsonify({"ok": True, "command": cmd})


if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=5050)
