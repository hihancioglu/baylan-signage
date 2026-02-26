import json
import os
import platform
import socket
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone

import socketio

from idle import get_idle_seconds
from media_manager import MediaManager
from player import BorderlessFullscreenPlayer
from state_machine import ClientState

SERVER_URL = os.getenv("SERVER_URL", "http://baylan-portainer:5080")
SECRET = os.getenv("SHARED_SECRET", "change_me_super_secret")
DEFAULT_IDLE_TIMEOUT_SEC = int(os.getenv("DEFAULT_IDLE_TIMEOUT_SEC", "60"))
HEARTBEAT_INTERVAL_SEC = int(os.getenv("HEARTBEAT_INTERVAL_SEC", "10"))
ACTIVITY_RESUME_SEC = float(os.getenv("ACTIVITY_RESUME_SEC", "1.0"))
STATE_LOG_PATH = os.getenv("STATE_LOG_PATH", "client/state_transitions.jsonl")
ERP_WINDOW_TITLE = os.getenv("ERP_WINDOW_TITLE", "ERP")

sio = socketio.Client(reconnection=True)
hostname = socket.gethostname()

idle_timeout_sec = DEFAULT_IDLE_TIMEOUT_SEC
current_state = ClientState.ACTIVE
emergency_active = False

SUPPORTED_COMMANDS = {
    "REFRESH_CONFIG",
    "EMERGENCY_START",
    "EMERGENCY_STOP",
    "RESTART_AGENT",
    "PING",
}


class _WindowManager:
    def __init__(self):
        import ctypes

        self._user32 = ctypes.windll.user32

    def bring_to_front(self, window_title: str) -> bool:
        hwnd = self._user32.FindWindowW(None, window_title)
        if not hwnd:
            return False

        SW_RESTORE = 9
        self._user32.ShowWindow(hwnd, SW_RESTORE)
        self._user32.SetForegroundWindow(hwnd)
        return True


class DownloadStatusOverlay:
    def __init__(self):
        self._active = False
        self._message = ""
        self._lock = threading.Lock()
        self._thread = None

    def show(self, message: str):
        with self._lock:
            self._message = message
            if self._active:
                return
            self._active = True

        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def update(self, message: str):
        with self._lock:
            self._message = message

    def hide(self):
        with self._lock:
            self._active = False

    def is_active(self) -> bool:
        with self._lock:
            return self._active

    def _run(self):
        try:
            import tkinter as tk
        except Exception as exc:
            print(f"⚠️ overlay açılamadı (tkinter yok): {exc}")
            return

        root = tk.Tk()
        root.configure(bg="black")
        root.attributes("-fullscreen", True)
        root.attributes("-topmost", True)
        root.title("Baylan Dijital Bilgi")

        label = tk.Label(
            root,
            text="",
            fg="white",
            bg="black",
            font=("Arial", 38, "bold"),
            justify="center",
            wraplength=1400,
        )
        label.place(relx=0.5, rely=0.5, anchor="center")

        def refresh():
            with self._lock:
                active = self._active
                message = self._message
            if not active:
                root.destroy()
                return
            label.config(text=message)
            root.after(200, refresh)

        root.after(50, refresh)
        root.mainloop()


class PlaybackController:
    def __init__(self):
        self.media_manager = MediaManager(cache_root=os.getenv("MEDIA_CACHE_DIR", "client/cache"))
        self.player = BorderlessFullscreenPlayer()
        self.overlay = DownloadStatusOverlay()
        self._playlist: list[str] = self.media_manager.load_last_successful_playlist()
        self._version = None
        self._lock = threading.Lock()
        self._running = False
        self._worker = None
        self._sync_in_progress = False
        self._sync_percent = 0

    def _on_sync_progress(self, progress: dict):
        with self._lock:
            self._sync_in_progress = progress.get("state") != "done"
            self._sync_percent = int(progress.get("percent", 0))

    def _overlay_text(self) -> str:
        with self._lock:
            percent = self._sync_percent
        return f"Yeni içerikler indiriliyor %{percent}\nBaylan Dijital Bilgi hazırlanıyor..."

    def update_from_config(self, config: dict):
        videos = config.get("videos") or []
        playlist_version = config.get("playlist_version") or "default"
        media_signatures = config.get("media_signatures") or {}

        if self._version == playlist_version and self._playlist:
            return

        local_playlist = self.media_manager.sync_playlist(
            videos,
            playlist_version,
            media_signatures,
            progress_callback=self._on_sync_progress,
        )
        if local_playlist:
            with self._lock:
                self._playlist = local_playlist
                self._version = playlist_version
                self._sync_in_progress = False
                self._sync_percent = 100
            print(f"📼 Playlist cache refreshed | version={playlist_version} items={len(local_playlist)}")
            return

        fallback = self.media_manager.load_last_successful_playlist()
        if fallback:
            with self._lock:
                self._playlist = fallback
                self._sync_in_progress = False
            print("📦 Offline mode: last successful cache playlist ile devam ediliyor")

    def start(self):
        if self._running:
            return
        self._running = True
        self._worker = threading.Thread(target=self._run, daemon=True)
        self._worker.start()

    def stop(self):
        self._running = False
        self.overlay.hide()
        self.player.stop()

    def pause(self):
        self.overlay.hide()
        self.player.stop()

    def _run(self):
        index = 0
        while self._running:
            with self._lock:
                playlist = list(self._playlist)
                sync_in_progress = self._sync_in_progress

            if sync_in_progress:
                if not self.overlay.is_active():
                    self.player.stop()
                    self.overlay.show(self._overlay_text())
                else:
                    self.overlay.update(self._overlay_text())
            elif self.overlay.is_active():
                self.overlay.hide()

            if not playlist:
                time.sleep(1)
                continue

            media_path = playlist[index % len(playlist)]
            ok = self.player.play_blocking(media_path)
            if not ok:
                print(f"⚠️ bozuk/oynatılamayan medya atlandı: {media_path}")
            index = (index + 1) % len(playlist)


window_manager = _WindowManager()
playback = PlaybackController()
processed_command_ids = set()
processed_lock = threading.Lock()


def _parse_issued_at(value):
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _validate_command(data):
    required = {"type", "command_id", "issued_at", "ttl_sec", "payload", "priority"}
    missing = [field for field in required if field not in data]
    if missing:
        return False, f"missing_fields={','.join(missing)}"
    if data.get("type") not in SUPPORTED_COMMANDS:
        return False, "unsupported_type"
    return True, "ok"


def _ack_command(command, status, detail="", duplicate=False):
    sio.emit(
        "command_ack",
        {
            "hostname": hostname,
            "command_id": command.get("command_id"),
            "type": command.get("type"),
            "status": status,
            "detail": detail,
            "duplicate": duplicate,
            "ack_at": datetime.now(timezone.utc).isoformat(),
        },
    )


def _command_expired(command):
    issued_at = _parse_issued_at(command.get("issued_at"))
    ttl_sec = command.get("ttl_sec")
    if issued_at is None or not isinstance(ttl_sec, (int, float)):
        return True
    expires_at = issued_at + timedelta(seconds=float(ttl_sec))
    return datetime.now(timezone.utc) > expires_at


def _restart_agent():
    if platform.system().lower().startswith("win"):
        subprocess.Popen(["shutdown", "/r", "/t", "0"])
        return "windows_reboot_requested"
    subprocess.Popen(["reboot"])
    return "reboot_requested"


def _handle_command(command):
    global emergency_active
    cmd_type = command.get("type")

    if cmd_type == "PING":
        return "pong"
    if cmd_type == "REFRESH_CONFIG":
        sio.emit("pull_config", {"hostname": hostname})
        return "config_pull_requested"
    if cmd_type == "EMERGENCY_START":
        emergency_active = True
        playback.pause()
        set_state(ClientState.EMERGENCY, "command=EMERGENCY_START")
        return "emergency_started"
    if cmd_type == "EMERGENCY_STOP":
        emergency_active = False
        set_state(ClientState.ACTIVE, "command=EMERGENCY_STOP")
        return "emergency_stopped"
    if cmd_type == "RESTART_AGENT":
        return _restart_agent()
    return "ignored"


def log_state_transition(from_state: ClientState, to_state: ClientState, reason: str):
    os.makedirs(os.path.dirname(STATE_LOG_PATH), exist_ok=True)
    line = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "hostname": hostname,
        "from": from_state.value,
        "to": to_state.value,
        "reason": reason,
    }
    with open(STATE_LOG_PATH, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(line, ensure_ascii=False) + "\n")


def set_state(next_state: ClientState, reason: str):
    global current_state

    if next_state == current_state:
        return

    prev = current_state
    current_state = next_state
    log_state_transition(prev, next_state, reason)
    print(f"🔁 STATE {prev.value} -> {next_state.value} | {reason}")


def return_to_erp_window():
    if window_manager.bring_to_front(ERP_WINDOW_TITLE):
        print(f"🪟 ERP window brought to front: {ERP_WINDOW_TITLE}")
    else:
        print(f"⚠️ ERP window not found: {ERP_WINDOW_TITLE}")


@sio.event
def connect():
    print("✅ Connected to server")

    sio.emit(
        "register",
        {
            "secret": SECRET,
            "hostname": hostname,
            "ip": socket.gethostbyname(hostname),
            "username": os.getenv("CLIENT_USERNAME", "test_user"),
            "department": os.getenv("CLIENT_DEPARTMENT", "URETIM"),
        },
    )


@sio.event
def disconnect():
    print("❌ Disconnected - offline cache playlist devam edebilir")


@sio.on("hello")
def on_hello(data):
    print("Server hello:", data)


@sio.on("config")
def on_config(data):
    global idle_timeout_sec

    print("📥 CONFIG RECEIVED:")
    print(data)

    config_timeout = data.get("idle_timeout_sec") if isinstance(data, dict) else None
    if isinstance(config_timeout, (int, float)) and config_timeout > 0:
        idle_timeout_sec = int(config_timeout)
    else:
        idle_timeout_sec = DEFAULT_IDLE_TIMEOUT_SEC

    if isinstance(data, dict):
        playback.update_from_config(data)

    print(f"🕒 idle_timeout_sec = {idle_timeout_sec}")


@sio.on("command")
def on_command(data):
    print("⚡ COMMAND RECEIVED:")
    print(data)

    if not isinstance(data, dict):
        _ack_command({"command_id": None, "type": None}, "rejected", "invalid_payload")
        return

    valid, reason = _validate_command(data)
    if not valid:
        _ack_command(data, "rejected", reason)
        return

    command_id = data.get("command_id")
    with processed_lock:
        if command_id in processed_command_ids:
            _ack_command(data, "duplicate", "already_processed", duplicate=True)
            return

    if _command_expired(data):
        _ack_command(data, "expired", "ttl_exceeded")
        return

    try:
        result = _handle_command(data)
        with processed_lock:
            processed_command_ids.add(command_id)
            if len(processed_command_ids) > 2000:
                processed_command_ids.clear()
                processed_command_ids.add(command_id)
        _ack_command(data, "processed", result)
    except Exception as exc:
        _ack_command(data, "failed", str(exc))


def run_state_cycle():
    if emergency_active:
        if current_state != ClientState.EMERGENCY:
            set_state(ClientState.EMERGENCY, "emergency_policy_enforced")
        playback.pause()
        return get_idle_seconds()

    idle_sec = get_idle_seconds()

    if current_state == ClientState.ACTIVE and idle_sec >= idle_timeout_sec:
        set_state(ClientState.IDLE_PENDING, f"idle={idle_sec:.1f}s threshold={idle_timeout_sec}s")

    if current_state == ClientState.IDLE_PENDING:
        playback.start()
        set_state(ClientState.PLAYING, "player_started")

    if current_state == ClientState.PLAYING and idle_sec <= ACTIVITY_RESUME_SEC:
        set_state(ClientState.RETURNING, f"activity_detected idle={idle_sec:.1f}s")

    if current_state == ClientState.RETURNING:
        playback.stop()
        return_to_erp_window()
        set_state(ClientState.ACTIVE, "returned_to_erp")

    return idle_sec


def main():
    print("Connecting to:", SERVER_URL)

    while True:
        try:
            sio.connect(SERVER_URL)
            break
        except KeyboardInterrupt:
            print("🛑 Client interrupted during connect")
            return
        except Exception as e:
            print("Connection failed, retrying...", e)
            time.sleep(3)

    while True:
        try:
            idle_sec = run_state_cycle()
            sio.emit(
                "heartbeat",
                {
                    "hostname": hostname,
                    "current_state": current_state.value,
                    "idle_seconds": round(idle_sec, 1),
                },
            )
            print(f"💓 heartbeat sent | state={current_state.value} idle={idle_sec:.1f}s")
            time.sleep(HEARTBEAT_INTERVAL_SEC)
        except KeyboardInterrupt:
            print("🛑 Client interrupted")
            break
        except Exception as e:
            print("Heartbeat loop stopped:", e)
            break

    playback.stop()
    try:
        sio.disconnect()
    except Exception:
        pass


if __name__ == "__main__":
    main()
