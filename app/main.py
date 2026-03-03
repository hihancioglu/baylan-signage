import eventlet
eventlet.monkey_patch()

import hashlib
import itertools
import json
import os
import re
import shutil
import threading
import time
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import unquote, urlparse

from flask import Flask, request, jsonify, render_template, send_from_directory, url_for, session, redirect
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
    MediaAsset,
    AppSetting,
)
from .config import (
    SHARED_SECRET,
    PANEL_SESSION_SECRET,
    AD_SERVER_URI,
    AD_DOMAIN,
    AD_USER_DN_TEMPLATE,
    AD_USE_SSL,
    AD_CONNECT_TIMEOUT,
    AD_ALLOWED_USERS,
)


try:
    from ldap3 import Server, Connection, SIMPLE
except ImportError:  # optional dependency during early setup
    Server = None
    Connection = None
    SIMPLE = None

app = Flask(__name__)
app.secret_key = PANEL_SESSION_SECRET
socketio = SocketIO(app, cors_allowed_origins="*")

ensure_sqlite_schema()

MEDIA_ROOT = Path(os.getenv("MEDIA_ROOT", "data/media")).resolve()
UPDATE_ROOT = Path(os.getenv("UPDATE_ROOT", "data/updates")).resolve()
ALLOWED_VIDEO_EXTENSIONS = {".mp4", ".webm", ".mov", ".m4v", ".mkv", ".avi"}
ALLOWED_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp", ".svg"}
MEDIA_ROOT.mkdir(parents=True, exist_ok=True)
UPDATE_ROOT.mkdir(parents=True, exist_ok=True)

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


def _is_allowed_media(filename: str) -> bool:
    suffix = Path(filename or "").suffix.lower()
    return suffix in ALLOWED_VIDEO_EXTENSIONS or suffix in ALLOWED_IMAGE_EXTENSIONS


def _safe_media_filename(original_name: str) -> str:
    ext = Path(original_name).suffix.lower()
    return f"{uuid.uuid4().hex}{ext}"


def _media_url(relative_path: str) -> str:
    return url_for("serve_media", asset_path=relative_path, _external=True)


def _media_kind_from_path(path: str) -> str:
    suffix = Path((path or "").split("?")[0]).suffix.lower()
    if suffix in ALLOWED_IMAGE_EXTENSIONS:
        return "image"
    return "video"


def _extract_relative_media_path(media_url: str) -> str | None:
    if not media_url:
        return None
    parsed = urlparse(media_url)
    media_prefix = "/media/"
    if not parsed.path.startswith(media_prefix):
        return None
    return unquote(parsed.path[len(media_prefix):]) or None


def _safe_update_filename(original_name: str) -> str:
    ext = Path(original_name or "").suffix.lower() or ".bin"
    return f"{uuid.uuid4().hex}{ext}"


def _extract_embedded_build_version(file_path: Path) -> str | None:
    pattern = re.compile(rb"BAYLAN_(?:CLIENT|UPDATER)_BUILD:(build-\d{14}|\d{14})")
    try:
        payload = file_path.read_bytes()
    except Exception:
        return None

    match = pattern.search(payload)
    if not match:
        return None
    try:
        return match.group(1).decode("utf-8")
    except UnicodeDecodeError:
        return None


def _resolve_update_version(explicit_version: str, filename: str, file_path: Path | None = None) -> str:
    if explicit_version:
        return explicit_version

    if file_path:
        embedded_version = _extract_embedded_build_version(file_path)
        if embedded_version:
            return embedded_version

    stem = Path(filename or "").stem
    if stem:
        match = re.search(r"(\d+(?:[._-]\d+)*)$", stem)
        if match:
            return match.group(1).replace("_", ".").replace("-", ".")

    return str(int(time.time()))


def _build_release_payload(db, key_prefix: str):
    version = _get_setting(db, f"{key_prefix}_version")
    file_name = _get_setting(db, f"{key_prefix}_file_name")
    file_path = _get_setting(db, f"{key_prefix}_file_path")
    if not (version and file_name and file_path):
        return None

    return {
        "version": version,
        "url": url_for("download_update_file", file_path=file_path, _external=True),
        "sha256": _get_setting(db, f"{key_prefix}_sha256") or "",
        "file_name": file_name,
        "size": int(_get_setting(db, f"{key_prefix}_size") or 0),
        "published_at": _get_setting(db, f"{key_prefix}_published_at"),
    }


def _build_updater_payload(db):
    return _build_release_payload(db, "updater")


def _build_client_updater_payload(db):
    return _build_release_payload(db, "client_updater")


def _get_setting(db, key: str, default: str | None = None) -> str | None:
    row = db.query(AppSetting).filter_by(key=key).first()
    return row.value if row else default


def _set_setting(db, key: str, value: str | None):
    row = db.query(AppSetting).filter_by(key=key).first()
    if not row:
        row = AppSetting(key=key)
    row.value = value
    db.add(row)


@app.route("/")
def index():
    return "Signage Server Running"


@app.get("/panel")
def panel():
    if _panel_auth_failed():
        return redirect(url_for("panel_login_page"))
    return render_template("panel.html")


@app.get("/panel/login")
def panel_login_page():
    if _is_panel_authenticated():
        return redirect(url_for("panel"))
    return render_template("panel_login.html")


@app.post("/panel/logout")
def panel_logout():
    session.clear()
    return redirect(url_for("panel_login_page"))


@app.post("/api/panel/login")
def panel_login():
    payload = request.get_json(silent=True) or {}
    username = str(payload.get("username") or "").strip()
    password = str(payload.get("password") or "")
    canonical_username = _canonical_ad_username(username)

    if not username or not password:
        return jsonify({"ok": False, "error": "Kullanıcı adı ve parola zorunlu"}), 400

    if not _ad_signin(username, password):
        return jsonify({"ok": False, "error": "AD kimlik doğrulaması başarısız"}), 401

    if AD_ALLOWED_USERS and canonical_username not in AD_ALLOWED_USERS:
        return jsonify({"ok": False, "error": "Bu kullanıcı panel erişimi için yetkili değil"}), 403

    session["panel_authenticated"] = True
    session["panel_username"] = username
    return jsonify({"ok": True})


@app.get("/media/<path:asset_path>")
def serve_media(asset_path):
    return send_from_directory(MEDIA_ROOT, asset_path)


@app.get("/updates/<path:file_path>")
def download_update_file(file_path):
    return send_from_directory(UPDATE_ROOT, file_path)


def db_session():
    return SessionLocal()


def _utc_now_iso():
    return datetime.now(timezone.utc).isoformat()


def _auth_failed():
    return _panel_auth_failed()


def _is_panel_authenticated() -> bool:
    return bool(session.get("panel_authenticated"))


def _ad_signin(username: str, password: str) -> bool:
    if not (Server and Connection):
        return False
    if not AD_SERVER_URI or not username or not password:
        return False

    server = Server(AD_SERVER_URI, use_ssl=AD_USE_SSL, get_info=None, connect_timeout=AD_CONNECT_TIMEOUT)

    user_dn = AD_USER_DN_TEMPLATE.format(username=username) if AD_USER_DN_TEMPLATE else _normalize_ad_account_name(username)
    conn = Connection(
        server,
        user=user_dn,
        password=password,
        authentication=SIMPLE,
        auto_bind=False,
        raise_exceptions=False,
    )
    ok = bool(conn.bind())
    conn.unbind()
    return ok


def _panel_auth_failed():
    return not _is_panel_authenticated()


def _normalize_ad_account_name(account_name: str) -> str:
    normalized = (account_name or "").strip()
    if not normalized:
        return normalized
    if "\\" not in normalized and "@" not in normalized and "=" not in normalized and AD_DOMAIN:
        return f"{AD_DOMAIN}\\{normalized}"
    return normalized


def _format_device_state(device) -> str:
    state = (device.last_state or "").upper()
    content_name = (device.last_content_name or "").strip()
    os_name = (device.os_version or "").lower()

    if state in {"IDLE", "IDLE_PENDING", "PLAYING"} and content_name:
        return content_name
    if "windows" in os_name:
        return "Windows"
    return device.last_state or ""


def _idle_minutes_since_last_state(device):
    state = (device.last_state or "").upper()
    if state != "IDLE" or not device.last_state_at:
        return None

    now = datetime.utcnow()
    last_state_at = device.last_state_at
    if getattr(last_state_at, "tzinfo", None) is not None:
        last_state_at = last_state_at.replace(tzinfo=None)

    elapsed = now - last_state_at
    if elapsed.total_seconds() < 0:
        return 0
    return int(elapsed.total_seconds() // 60)


def _canonical_ad_username(username: str) -> str:
    normalized = (username or "").strip().lower()
    if not normalized:
        return ""
    if "\\" in normalized:
        return normalized.split("\\", 1)[1]
    if "@" in normalized:
        return normalized.split("@", 1)[0]
    return normalized


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
        "alias": device.alias,
        "ip": device.ip,
        "department": device.department,
        "username": device.username,
        "is_online": bool(device.is_online),
        "last_seen": device.last_seen.isoformat() if device.last_seen else None,
        "last_state": device.last_state,
        "last_state_at": device.last_state_at.isoformat() if device.last_state_at else None,
        "idle_minutes": _idle_minutes_since_last_state(device),
        "state_display": _format_device_state(device),
        "last_content_name": device.last_content_name,
        "agent_version": device.agent_version,
        "idle_mode_enabled": device.idle_mode_enabled,
        "content_enabled": device.content_enabled,
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


def _emit_config_update(hostnames):
    for hostname in sorted(set(hostnames or [])):
        if hostname not in connected:
            continue
        socketio.emit("config", build_config(hostname), room=f"device:{hostname}")


def _hostnames_for_group(db, group_id):
    rows = (
        db.query(Device.hostname)
        .join(DeviceGroup, DeviceGroup.device_id == Device.id)
        .filter(DeviceGroup.group_id == int(group_id), DeviceGroup.is_active.is_(True))
        .all()
    )
    return [r[0] for r in rows]


def _hostnames_for_playlist(db, playlist_id):
    rows = (
        db.query(Device.hostname)
        .join(DeviceGroup, DeviceGroup.device_id == Device.id)
        .join(GroupPlaylist, GroupPlaylist.group_id == DeviceGroup.group_id)
        .filter(
            DeviceGroup.is_active.is_(True),
            GroupPlaylist.playlist_id == int(playlist_id),
        )
        .all()
    )
    return [r[0] for r in rows]


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
        fallback_media = _get_setting(db, "fallback_media_url")
        fallback_version = _get_setting(db, "fallback_media_version", "0")

        base_config = {
            "enabled": False,
            "videos": [],
            "fallback_media": fallback_media,
            "fallback_media_version": fallback_version,
            "loop_mode": "sequential",
            "idle_timeout_sec": None,
            "idle_mode_enabled": True,
            "content_enabled": True,
            "updater": _build_updater_payload(db),
            "client_updater": _build_client_updater_payload(db),
        }

        device = db.query(Device).filter_by(hostname=hostname).first()
        if not device:
            return base_config

        dg = db.query(DeviceGroup).filter_by(device_id=device.id, is_active=True).first()
        if not dg:
            return base_config

        group = db.query(Group).filter_by(id=dg.group_id).first()
        if group:
            if isinstance(group.idle_timeout_sec, int) and group.idle_timeout_sec > 0:
                base_config["idle_timeout_sec"] = group.idle_timeout_sec
            if group.idle_mode_enabled is not None:
                base_config["idle_mode_enabled"] = bool(group.idle_mode_enabled)
            if group.content_enabled is not None:
                base_config["content_enabled"] = bool(group.content_enabled)

        gp = db.query(GroupPlaylist).filter_by(group_id=dg.group_id).first()
        if not gp:
            return base_config

        playlist = db.query(Playlist).filter_by(id=gp.playlist_id).first()
        if not playlist or not playlist.enabled:
            return base_config

        items = (
            db.query(PlaylistItem)
            .filter_by(playlist_id=playlist.id)
            .order_by(PlaylistItem.order_no)
            .all()
        )

        videos = [
            {
                "path": i.path,
                "media_type": i.media_type or _media_kind_from_path(i.path),
                "duration_sec": i.duration_sec,
                "order_no": i.order_no,
            }
            for i in items
            if i.path
        ]
        media_signatures = {
            item["path"]: hashlib.sha256(item["path"].encode("utf-8")).hexdigest() for item in videos
        }
        playlist_fingerprint = "|".join(
            (
                f"{item['path']}:{item.get('duration_sec') or 0}:"
                f"{item.get('media_type') or ''}:{item.get('order_no') or 0}"
            )
            for item in videos
        )
        playlist_version = hashlib.sha256(
            f"{playlist.id}:{playlist.loop_mode}:{playlist_fingerprint}".encode("utf-8")
        ).hexdigest()[:16]

        return {
            **base_config,
            "enabled": True,
            "videos": videos,
            "playlist_version": playlist_version,
            "media_signatures": media_signatures,
            "loop_mode": playlist.loop_mode or "sequential",
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
        incoming_state = data.get("state") or data.get("current_state")
        if incoming_state:
            device.last_state = incoming_state
            device.last_state_at = datetime.utcnow()
        device.agent_version = data.get("agent_version") or device.agent_version
        device.os_version = data.get("os_name") or device.os_version
        device.last_content_name = data.get("content_name") or ""
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
            incoming_state = data.get("state") or data.get("current_state")
            if incoming_state:
                device.last_state = incoming_state
                device.last_state_at = datetime.utcnow()
            device.agent_version = data.get("agent_version") or device.agent_version
            device.os_version = data.get("os_name") or device.os_version
            device.last_content_name = data.get("content_name") or ""
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


@app.patch("/api/devices/<hostname>/alias")
def update_device_alias(hostname):
    if _auth_failed():
        return jsonify({"error": "unauthorized"}), 401

    payload = request.get_json(silent=True) or {}
    alias = (payload.get("alias") or "").strip()
    if len(alias) > 128:
        return jsonify({"error": "alias too long (max 128)"}), 400

    db = db_session()
    try:
        device = db.query(Device).filter_by(hostname=hostname).first()
        if not device:
            return jsonify({"error": "device not found"}), 404

        device.alias = alias or None
        db.commit()
        return jsonify({"ok": True, "device": _serialize_device(db, device)})
    finally:
        db.close()


@app.patch("/api/devices/<hostname>/settings")
def update_device_settings(hostname):
    if _auth_failed():
        return jsonify({"error": "unauthorized"}), 401
    return jsonify({"error": "device-level settings are disabled; use group settings"}), 410


@app.post("/api/devices/<hostname>/group/<int:group_id>")
def bind_device_group(hostname, group_id):
    if _auth_failed():
        return jsonify({"error": "unauthorized"}), 401

    db = db_session()
    try:
        device = db.query(Device).filter_by(hostname=hostname).first()
        if not device:
            return jsonify({"error": "device not found"}), 404

        group = db.query(Group).filter_by(id=group_id).first()
        if not group:
            return jsonify({"error": "group not found"}), 404

        active_memberships = db.query(DeviceGroup).filter_by(device_id=device.id, is_active=True).all()

        for membership in active_memberships:
            membership.is_active = False
            membership.unassigned_at = datetime.utcnow()
        db.add(DeviceGroup(device_id=device.id, group_id=group_id, is_active=True))
        db.commit()

        _emit_config_update([hostname])

        sid = connected.get(hostname)
        if sid:
            for membership in active_memberships:
                socketio.server.leave_room(sid, f"group:{membership.group_id}")
            socketio.server.enter_room(sid, f"group:{group_id}")

        return jsonify({"ok": True})
    finally:
        db.close()


@app.delete("/api/devices/<hostname>/group")
def unbind_device_group(hostname):
    if _auth_failed():
        return jsonify({"error": "unauthorized"}), 401

    db = db_session()
    try:
        device = db.query(Device).filter_by(hostname=hostname).first()
        if not device:
            return jsonify({"error": "device not found"}), 404

        active_memberships = db.query(DeviceGroup).filter_by(device_id=device.id, is_active=True).all()
        if not active_memberships:
            return jsonify({"ok": True, "removed": 0})

        for membership in active_memberships:
            membership.is_active = False
            membership.unassigned_at = datetime.utcnow()

        db.commit()

        _emit_config_update([hostname])

        sid = connected.get(hostname)
        if sid:
            for membership in active_memberships:
                socketio.server.leave_room(sid, f"group:{membership.group_id}")

        return jsonify({"ok": True, "removed": len(active_memberships)})
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
                    "idle_timeout_sec": g.idle_timeout_sec,
                    "idle_mode_enabled": bool(g.idle_mode_enabled) if g.idle_mode_enabled is not None else True,
                    "content_enabled": bool(g.content_enabled) if g.content_enabled is not None else True,
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

    payload = request.json or {}
    name = payload.get("name")
    if not name:
        return jsonify({"error": "name required"}), 400

    idle_timeout_sec = payload.get("idle_timeout_sec")
    idle_mode_enabled = payload.get("idle_mode_enabled")
    content_enabled = payload.get("content_enabled")
    if idle_timeout_sec is not None:
        if not isinstance(idle_timeout_sec, int) or idle_timeout_sec <= 0:
            return jsonify({"error": "idle_timeout_sec must be a positive integer"}), 400

    if idle_mode_enabled is not None and not isinstance(idle_mode_enabled, bool):
        return jsonify({"error": "idle_mode_enabled must be a boolean"}), 400

    if content_enabled is not None and not isinstance(content_enabled, bool):
        return jsonify({"error": "content_enabled must be a boolean"}), 400

    db = db_session()
    try:
        existing = db.query(Group).filter_by(name=name).first()
        if existing:
            return jsonify({"ok": True, "id": existing.id, "already_exists": True})

        g = Group(
            name=name,
            idle_timeout_sec=idle_timeout_sec,
            idle_mode_enabled=True if idle_mode_enabled is None else idle_mode_enabled,
            content_enabled=True if content_enabled is None else content_enabled,
        )
        db.add(g)
        db.commit()
        return jsonify({"ok": True, "id": g.id, "already_exists": False})
    finally:
        db.close()


@app.patch("/api/groups/<int:group_id>")
def update_group(group_id):
    if _auth_failed():
        return jsonify({"error": "unauthorized"}), 401

    payload = request.json or {}
    name = (payload.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name required"}), 400

    idle_timeout_sec = payload.get("idle_timeout_sec")
    idle_mode_enabled = payload.get("idle_mode_enabled")
    content_enabled = payload.get("content_enabled")
    if idle_timeout_sec is not None:
        if not isinstance(idle_timeout_sec, int) or idle_timeout_sec <= 0:
            return jsonify({"error": "idle_timeout_sec must be a positive integer"}), 400

    if idle_mode_enabled is not None and not isinstance(idle_mode_enabled, bool):
        return jsonify({"error": "idle_mode_enabled must be a boolean"}), 400

    if content_enabled is not None and not isinstance(content_enabled, bool):
        return jsonify({"error": "content_enabled must be a boolean"}), 400

    db = db_session()
    try:
        group = db.query(Group).filter_by(id=group_id).first()
        if not group:
            return jsonify({"error": "group not found"}), 404

        existing = db.query(Group).filter(Group.name == name, Group.id != group_id).first()
        if existing:
            return jsonify({"error": "group name already exists"}), 409

        previous_idle_timeout_sec = group.idle_timeout_sec
        previous_idle_mode_enabled = group.idle_mode_enabled
        previous_content_enabled = group.content_enabled

        group.name = name
        group.idle_timeout_sec = idle_timeout_sec
        if idle_mode_enabled is not None:
            group.idle_mode_enabled = idle_mode_enabled
        if content_enabled is not None:
            group.content_enabled = content_enabled
        db.commit()

        if (
            previous_idle_timeout_sec != idle_timeout_sec
            or previous_idle_mode_enabled != group.idle_mode_enabled
            or previous_content_enabled != group.content_enabled
        ):
            _emit_config_update(_hostnames_for_group(db, group_id))

        return jsonify({"ok": True})
    finally:
        db.close()


@app.delete("/api/groups/<int:group_id>")
def delete_group(group_id):
    if _auth_failed():
        return jsonify({"error": "unauthorized"}), 401

    db = db_session()
    try:
        group = db.query(Group).filter_by(id=group_id).first()
        if not group:
            return jsonify({"error": "group not found"}), 404

        affected_hostnames = _hostnames_for_group(db, group_id)

        deactivated_memberships = db.query(DeviceGroup).filter_by(group_id=group_id, is_active=True).update(
            {"is_active": False, "unassigned_at": datetime.utcnow()},
            synchronize_session=False,
        )
        db.query(GroupPlaylist).filter_by(group_id=group_id).delete()
        db.query(DeviceGroup).filter_by(group_id=group_id).delete()
        db.delete(group)
        db.commit()

        _emit_config_update(affected_hostnames)

        for hostname, sid in connected.items():
            socketio.server.leave_room(sid, f"group:{group_id}")

        return jsonify({"ok": True, "deactivated_memberships": deactivated_memberships})
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

        _emit_config_update(_hostnames_for_group(db, group_id))

        return jsonify({"ok": True})
    finally:
        db.close()


@app.delete("/api/groups/<int:group_id>/playlist")
def unbind_group_playlist(group_id):
    if _auth_failed():
        return jsonify({"error": "unauthorized"}), 401

    db = db_session()
    try:
        group = db.query(Group).filter_by(id=group_id).first()
        if not group:
            return jsonify({"error": "group not found"}), 404

        db.query(GroupPlaylist).filter_by(group_id=group_id).delete()
        db.commit()

        _emit_config_update(_hostnames_for_group(db, group_id))

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
            {"id": p.id, "name": p.name, "enabled": bool(p.enabled), "loop_mode": p.loop_mode or "sequential"}
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
    loop_mode = str((request.json or {}).get("loop_mode", "sequential") or "sequential").strip().lower()
    if loop_mode not in {"sequential", "random"}:
        return jsonify({"error": "invalid loop_mode"}), 400

    db = db_session()
    try:
        pl = Playlist(name=name, enabled=enabled, loop_mode=loop_mode)
        db.add(pl)
        db.commit()
        return jsonify({"ok": True, "id": pl.id})
    finally:
        db.close()


@app.get("/api/media")
def list_media_assets():
    if _auth_failed():
        return jsonify({"error": "unauthorized"}), 401

    db = db_session()
    try:
        assets = db.query(MediaAsset).order_by(MediaAsset.created_at.desc(), MediaAsset.id.desc()).all()
        return jsonify([
            {
                "id": a.id,
                "name": a.original_name,
                "content_type": a.content_type,
                "file_size": a.file_size,
                "created_at": a.created_at.isoformat() if a.created_at else None,
                "url": _media_url(a.relative_path),
                "relative_path": a.relative_path,
            }
            for a in assets
        ])
    finally:
        db.close()


@app.post("/api/media/upload")
def upload_media_asset():
    if _auth_failed():
        return jsonify({"error": "unauthorized"}), 401

    uploaded = request.files.get("file")
    if not uploaded or not uploaded.filename:
        return jsonify({"error": "file required"}), 400

    if not _is_allowed_media(uploaded.filename):
        return jsonify({"error": "unsupported file type"}), 400

    stored_name = _safe_media_filename(uploaded.filename)
    stored_path = MEDIA_ROOT / stored_name
    uploaded.save(stored_path)

    try:
        checksum = hashlib.sha256(stored_path.read_bytes()).hexdigest()
        file_size = stored_path.stat().st_size
        content_type = uploaded.mimetype
        display_name = uploaded.filename
        stored_relative_path = stored_name
    except Exception as exc:
        stored_path.unlink(missing_ok=True)
        return jsonify({"error": str(exc)}), 400

    db = db_session()
    try:
        asset = MediaAsset(
            original_name=display_name,
            stored_name=stored_name,
            relative_path=stored_relative_path,
            content_type=content_type,
            file_size=file_size,
            checksum=checksum,
        )
        db.add(asset)
        db.commit()

        return jsonify(
            {
                "ok": True,
                "asset": {
                    "id": asset.id,
                    "name": asset.original_name,
                    "url": _media_url(asset.relative_path),
                    "content_type": asset.content_type,
                    "file_size": asset.file_size,
                },
            }
        )
    finally:
        db.close()


@app.get("/api/updater")
def get_updater_settings():
    if _auth_failed():
        return jsonify({"error": "unauthorized"}), 401

    db = db_session()
    try:
        payload = _build_updater_payload(db)
        return jsonify({"ok": True, "release": payload})
    finally:
        db.close()


@app.get("/api/client-updater")
def get_client_updater_settings():
    if _auth_failed():
        return jsonify({"error": "unauthorized"}), 401

    db = db_session()
    try:
        payload = _build_client_updater_payload(db)
        return jsonify({"ok": True, "release": payload})
    finally:
        db.close()


@app.post("/api/updater/upload")
def upload_client_update():
    if _auth_failed():
        return jsonify({"error": "unauthorized"}), 401

    requested_version = str(request.form.get("version") or "").strip()
    uploaded = request.files.get("file")
    if not uploaded or not uploaded.filename:
        return jsonify({"error": "file required"}), 400

    stored_name = _safe_update_filename(uploaded.filename)
    temp_name = f".tmp-{uuid.uuid4().hex}{Path(stored_name).suffix}"
    temp_path = UPDATE_ROOT / temp_name
    uploaded.save(temp_path)

    version = _resolve_update_version(requested_version, uploaded.filename, temp_path)
    relative_path = f"{version}/{stored_name}"
    stored_path = UPDATE_ROOT / relative_path
    stored_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path.replace(stored_path)

    try:
        checksum = hashlib.sha256(stored_path.read_bytes()).hexdigest()
        file_size = stored_path.stat().st_size
        published_at = datetime.now(timezone.utc).isoformat()
    except Exception as exc:
        temp_path.unlink(missing_ok=True)
        stored_path.unlink(missing_ok=True)
        return jsonify({"error": str(exc)}), 400

    db = db_session()
    try:
        old_relative_path = _get_setting(db, "updater_file_path")

        _set_setting(db, "updater_version", version)
        _set_setting(db, "updater_file_name", uploaded.filename)
        _set_setting(db, "updater_file_path", relative_path)
        _set_setting(db, "updater_sha256", checksum)
        _set_setting(db, "updater_size", str(file_size))
        _set_setting(db, "updater_published_at", published_at)
        db.commit()

        if old_relative_path and old_relative_path != relative_path:
            old_path = UPDATE_ROOT / old_relative_path
            old_path.unlink(missing_ok=True)

        hostnames = [row[0] for row in db.query(Device.hostname).all()]
        _emit_config_update(hostnames)

        return jsonify({"ok": True, "release": _build_updater_payload(db)})
    finally:
        db.close()


@app.post("/api/client-updater/upload")
def upload_client_updater_update():
    if _auth_failed():
        return jsonify({"error": "unauthorized"}), 401

    requested_version = str(request.form.get("version") or "").strip()
    uploaded = request.files.get("file")
    if not uploaded or not uploaded.filename:
        return jsonify({"error": "file required"}), 400

    stored_name = _safe_update_filename(uploaded.filename)
    temp_name = f".tmp-{uuid.uuid4().hex}{Path(stored_name).suffix}"
    temp_path = UPDATE_ROOT / temp_name
    uploaded.save(temp_path)

    version = _resolve_update_version(requested_version, uploaded.filename, temp_path)
    relative_path = f"client-updater/{version}/{stored_name}"
    stored_path = UPDATE_ROOT / relative_path
    stored_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path.replace(stored_path)

    try:
        checksum = hashlib.sha256(stored_path.read_bytes()).hexdigest()
        file_size = stored_path.stat().st_size
        published_at = datetime.now(timezone.utc).isoformat()
    except Exception as exc:
        temp_path.unlink(missing_ok=True)
        stored_path.unlink(missing_ok=True)
        return jsonify({"error": str(exc)}), 400

    db = db_session()
    try:
        old_relative_path = _get_setting(db, "client_updater_file_path")

        _set_setting(db, "client_updater_version", version)
        _set_setting(db, "client_updater_file_name", uploaded.filename)
        _set_setting(db, "client_updater_file_path", relative_path)
        _set_setting(db, "client_updater_sha256", checksum)
        _set_setting(db, "client_updater_size", str(file_size))
        _set_setting(db, "client_updater_published_at", published_at)
        db.commit()

        if old_relative_path and old_relative_path != relative_path:
            old_path = UPDATE_ROOT / old_relative_path
            old_path.unlink(missing_ok=True)

        hostnames = [row[0] for row in db.query(Device.hostname).all()]
        _emit_config_update(hostnames)

        return jsonify({"ok": True, "release": _build_client_updater_payload(db)})
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

        media_assets_by_relative_path = {
            media.relative_path: media.original_name
            for media in db.query(MediaAsset.relative_path, MediaAsset.original_name).all()
        }

        def _resolve_playlist_label(item_path: str) -> str:
            relative_path = _extract_relative_media_path(item_path)
            if relative_path and relative_path in media_assets_by_relative_path:
                return media_assets_by_relative_path[relative_path]
            return Path((item_path or "").split("?")[0]).name or item_path

        return jsonify([
            {
                "id": i.id,
                "path": i.path,
                "order_no": i.order_no,
                "media_type": i.media_type or _media_kind_from_path(i.path),
                "duration_sec": i.duration_sec,
                "label": _resolve_playlist_label(i.path),
            }
            for i in items
        ])
    finally:
        db.close()




@app.get("/api/settings/fallback-media")
def get_fallback_media_setting():
    if _auth_failed():
        return jsonify({"error": "unauthorized"}), 401

    db = db_session()
    try:
        media_url = _get_setting(db, "fallback_media_url")
        media_name = _get_setting(db, "fallback_media_name")
        media_id = _get_setting(db, "fallback_media_id")
        return jsonify({
            "media_url": media_url,
            "media_name": media_name,
            "media_id": int(media_id) if media_id and media_id.isdigit() else None,
        })
    finally:
        db.close()


@app.post("/api/settings/fallback-media")
def set_fallback_media_setting():
    if _auth_failed():
        return jsonify({"error": "unauthorized"}), 401

    media_id = (request.json or {}).get("media_id")
    if not media_id:
        return jsonify({"error": "media_id required"}), 400

    db = db_session()
    try:
        asset = db.query(MediaAsset).filter_by(id=media_id).first()
        if not asset:
            return jsonify({"error": "media not found"}), 404

        _set_setting(db, "fallback_media_id", str(asset.id))
        _set_setting(db, "fallback_media_url", _media_url(asset.relative_path))
        _set_setting(db, "fallback_media_name", asset.original_name)
        _set_setting(db, "fallback_media_version", str(int(time.time())))
        db.commit()

        hostnames = [row[0] for row in db.query(Device.hostname).all()]
        _emit_config_update(hostnames)

        return jsonify({"ok": True, "media_url": _media_url(asset.relative_path), "media_name": asset.original_name})
    finally:
        db.close()


@app.post("/api/playlists/<int:playlist_id>/items")
def add_playlist_item(playlist_id):
    if _auth_failed():
        return jsonify({"error": "unauthorized"}), 401

    body = request.json or {}
    media_id = body.get("media_id")
    order_no = body.get("order_no", 0)
    duration_sec = body.get("duration_sec")

    db = db_session()
    try:
        asset = db.query(MediaAsset).filter_by(id=media_id).first()
        if not asset:
            return jsonify({"error": "media not found"}), 404

        media_path = _media_url(asset.relative_path)
        media_type = _media_kind_from_path(media_path)
        duration = None
        if media_type == "image":
            try:
                duration = int(duration_sec) if duration_sec is not None else 8
            except (TypeError, ValueError):
                return jsonify({"error": "invalid duration_sec"}), 400
            if duration <= 0:
                return jsonify({"error": "duration_sec must be > 0"}), 400

        item = PlaylistItem(playlist_id=playlist_id, path=media_path, order_no=order_no, media_type=media_type, duration_sec=duration)
        db.add(item)
        db.commit()

        _emit_config_update(_hostnames_for_playlist(db, playlist_id))

        return jsonify({"ok": True, "id": item.id})
    finally:
        db.close()


@app.delete("/api/playlists/<int:playlist_id>/items/<int:item_id>")
def delete_playlist_item(playlist_id, item_id):
    if _auth_failed():
        return jsonify({"error": "unauthorized"}), 401

    db = db_session()
    try:
        item = db.query(PlaylistItem).filter_by(id=item_id, playlist_id=playlist_id).first()
        if not item:
            return jsonify({"error": "playlist item not found"}), 404
        db.delete(item)
        db.commit()

        _emit_config_update(_hostnames_for_playlist(db, playlist_id))

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

        _emit_config_update(_hostnames_for_playlist(db, playlist_id))

        return jsonify({"ok": True})
    finally:
        db.close()


@app.patch("/api/playlists/<int:playlist_id>")
def update_playlist(playlist_id):
    if _auth_failed():
        return jsonify({"error": "unauthorized"}), 401

    body = request.json or {}
    db = db_session()
    try:
        playlist = db.query(Playlist).filter_by(id=playlist_id).first()
        if not playlist:
            return jsonify({"error": "playlist not found"}), 404

        if "name" in body and body.get("name"):
            playlist.name = body["name"]
        if "enabled" in body:
            playlist.enabled = bool(body.get("enabled"))
        if "loop_mode" in body:
            loop_mode = str(body.get("loop_mode") or "sequential").strip().lower()
            if loop_mode not in {"sequential", "random"}:
                return jsonify({"error": "invalid loop_mode"}), 400
            playlist.loop_mode = loop_mode

        db.commit()

        _emit_config_update(_hostnames_for_playlist(db, playlist_id))

        return jsonify({"ok": True})
    finally:
        db.close()


@app.delete("/api/playlists/<int:playlist_id>")
def delete_playlist(playlist_id):
    if _auth_failed():
        return jsonify({"error": "unauthorized"}), 401

    db = db_session()
    try:
        playlist = db.query(Playlist).filter_by(id=playlist_id).first()
        if not playlist:
            return jsonify({"error": "playlist not found"}), 404

        impacted_hosts = _hostnames_for_playlist(db, playlist_id)
        db.query(GroupPlaylist).filter_by(playlist_id=playlist_id).delete()
        db.query(PlaylistItem).filter_by(playlist_id=playlist_id).delete()
        db.delete(playlist)
        db.commit()

        _emit_config_update(impacted_hosts)

        return jsonify({"ok": True})
    finally:
        db.close()


@app.delete("/api/media/<int:media_id>")
def delete_media_asset(media_id):
    if _auth_failed():
        return jsonify({"error": "unauthorized"}), 401

    db = db_session()
    try:
        asset = db.query(MediaAsset).filter_by(id=media_id).first()
        if not asset:
            return jsonify({"error": "media not found"}), 404

        media_url = _media_url(asset.relative_path)
        relative_path = asset.relative_path
        impacted_playlist_ids = [
            row[0]
            for row in (
                db.query(PlaylistItem.playlist_id)
                .filter(PlaylistItem.path == media_url)
                .distinct()
                .all()
            )
        ]
        impacted_hosts = []
        for playlist_id in impacted_playlist_ids:
            impacted_hosts.extend(_hostnames_for_playlist(db, playlist_id))

        removed_playlist_item_count = (
            db.query(PlaylistItem)
            .filter(PlaylistItem.path == media_url)
            .delete(synchronize_session=False)
        )

        fallback_media_id = _get_setting(db, "fallback_media_id")
        if fallback_media_id and str(fallback_media_id) == str(media_id):
            _set_setting(db, "fallback_media_id", None)
            _set_setting(db, "fallback_media_url", None)
            _set_setting(db, "fallback_media_name", None)
            _set_setting(db, "fallback_media_version", str(int(time.time())))
            all_hosts = [row[0] for row in db.query(Device.hostname).all()]
            impacted_hosts.extend(all_hosts)

        db.delete(asset)
        db.commit()

        _emit_config_update(impacted_hosts)

        file_path = MEDIA_ROOT / relative_path
        if file_path.exists():
            file_path.unlink()

        if relative_path.startswith("slides/") and relative_path.endswith(".json"):
            slides_folder = MEDIA_ROOT / "slides" / Path(relative_path).stem
            if slides_folder.exists():
                shutil.rmtree(slides_folder, ignore_errors=True)

        original_upload = MEDIA_ROOT / asset.stored_name
        if original_upload.exists() and original_upload != file_path:
            original_upload.unlink()

        return jsonify({"ok": True, "removed_playlist_item_count": removed_playlist_item_count})
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
