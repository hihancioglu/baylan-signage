import json
import os
import platform
import socket
import subprocess
import threading
import ctypes
from pathlib import Path
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
STATE_CHECK_INTERVAL_SEC = float(os.getenv("STATE_CHECK_INTERVAL_SEC", "0.5"))
RECONNECT_RETRY_SEC = float(os.getenv("RECONNECT_RETRY_SEC", "3"))
ACTIVITY_RESUME_SEC = float(os.getenv("ACTIVITY_RESUME_SEC", "1.0"))
MIN_PLAYING_SECONDS = float(os.getenv("MIN_PLAYING_SECONDS", "5.0"))
STATE_LOG_PATH = os.getenv("STATE_LOG_PATH", "client/state_transitions.jsonl")
ERP_WINDOW_TITLE = os.getenv("ERP_WINDOW_TITLE", "ERP")
ERP_WINDOW_MATCH_MODE = os.getenv("ERP_WINDOW_MATCH_MODE", "contains").strip().lower()

sio = socketio.Client(reconnection=True)
hostname = socket.gethostname()

idle_timeout_sec = DEFAULT_IDLE_TIMEOUT_SEC
idle_mode_enabled = True
content_enabled = True
current_state = ClientState.ACTIVE
emergency_active = False
playing_started_at = 0.0

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

        self._ctypes = ctypes
        self._user32 = ctypes.windll.user32

    def _normalize(self, text: str) -> str:
        return " ".join((text or "").strip().lower().split())

    def _find_window_handle(self, window_title: str) -> int:
        if not window_title:
            return 0

        if ERP_WINDOW_MATCH_MODE == "exact":
            return self._user32.FindWindowW(None, window_title)

        matches = []
        needle = self._normalize(window_title)

        EnumWindowsProc = self._ctypes.WINFUNCTYPE(
            self._ctypes.c_bool,
            self._ctypes.c_void_p,
            self._ctypes.c_void_p,
        )

        def callback(hwnd, _):
            if not self._user32.IsWindowVisible(hwnd):
                return True

            length = self._user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return True

            title_buffer = self._ctypes.create_unicode_buffer(length + 1)
            self._user32.GetWindowTextW(hwnd, title_buffer, length + 1)
            normalized_title = self._normalize(title_buffer.value)
            if normalized_title and needle in normalized_title:
                matches.append(hwnd)
                return False
            return True

        self._user32.EnumWindows(EnumWindowsProc(callback), 0)
        return int(matches[0]) if matches else 0

    def bring_to_front(self, window_title: str) -> bool:
        hwnd = self._find_window_handle(window_title)
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
        cached_entries = self.media_manager.load_last_successful_playlist_entries()
        self._playlist_entries: list[dict] = cached_entries or [
            {"local_path": p, "duration_sec": None, "media_type": None}
            for p in self.media_manager.load_last_successful_playlist()
        ]
        self._fallback_media = Path(
            os.getenv("FALLBACK_MEDIA_PATH", "client/assets/digital-screen-preparing.svg")
        )
        self._fallback_warning_emitted = False
        self._configured_fallback: list[dict] = []
        self._fallback_only_mode = False
        self._version = None
        self._loop_mode = "sequential"
        self._lock = threading.Lock()
        self._running = False
        self._worker = None
        self._sync_in_progress = False
        self._sync_percent = 0
        self._waiting_for_media_logged = False
        self._active_item_started_at = None
        self._active_item = None
        self._playback_state = self.media_manager.load_playback_state()

    def _on_sync_progress(self, progress: dict):
        with self._lock:
            self._sync_in_progress = progress.get("state") != "done"
            self._sync_percent = int(progress.get("percent", 0))

    def _overlay_text(self) -> str:
        with self._lock:
            percent = self._sync_percent
        return f"Yeni içerikler indiriliyor %{percent}\nBaylan Dijital Bilgi hazırlanıyor..."

    def _effective_playlist(self, playlist_entries: list[dict]) -> list[dict]:
        if self._fallback_only_mode and self._configured_fallback:
            return list(self._configured_fallback)

        if playlist_entries:
            return playlist_entries

        if self._configured_fallback:
            return list(self._configured_fallback)

        if self._fallback_media.exists() and self.player.supports_media(str(self._fallback_media)):
            return [{"local_path": str(self._fallback_media), "duration_sec": None, "media_type": "image"}]

        if self._fallback_media.exists() and not self._fallback_warning_emitted:
            print(f"⚠️ fallback medya desteklenmiyor, oynatılmayacak: {self._fallback_media}")
            self._fallback_warning_emitted = True
        return []

    @staticmethod
    def _playlist_key(entries: list[dict], loop_mode: str) -> str:
        return f"{loop_mode}::" + "|".join(str(e.get("local_path") or "") for e in entries)

    def _restore_or_init_runtime_state(self, entries: list[dict], loop_mode: str) -> dict:
        key = self._playlist_key(entries, loop_mode)
        paths = [str(e.get("local_path")) for e in entries]
        state = self._playback_state if isinstance(self._playback_state, dict) else {}
        if state.get("playlist_key") != key:
            state = {"playlist_key": key}

        if loop_mode == "random":
            order = state.get("random_order")
            if not isinstance(order, list) or sorted(order) != sorted(paths):
                import random

                order = list(paths)
                random.shuffle(order)
                state["random_order"] = order
                state["random_pos"] = 0
                state["resume_sec"] = 0
            state["random_pos"] = int(state.get("random_pos") or 0)
        else:
            state["index"] = int(state.get("index") or 0)
            state["resume_sec"] = float(state.get("resume_sec") or 0)

        self._playback_state = state
        return state

    def _persist_playback_state(self):
        self.media_manager.save_playback_state(self._playback_state)

    def update_from_config(self, config: dict):
        enabled = bool(config.get("enabled", True))
        videos = config.get("videos") or []
        playlist_version = config.get("playlist_version") or "default"
        media_signatures = config.get("media_signatures") or {}
        fallback_media = config.get("fallback_media")
        fallback_version = config.get("fallback_media_version") or "0"
        loop_mode = str(config.get("loop_mode") or "sequential").strip().lower()
        if loop_mode not in {"sequential", "random"}:
            loop_mode = "sequential"

        fallback_playlist = []
        if fallback_media:
            fallback_entries = self.media_manager.sync_playlist_entries(
                [{"path": fallback_media, "media_type": "image", "duration_sec": None}],
                f"fallback-{fallback_version}",
                {},
                progress_callback=None,
            )
            fallback_playlist = fallback_entries
        with self._lock:
            self._configured_fallback = fallback_playlist
            self._fallback_only_mode = not enabled
            self._loop_mode = loop_mode

            if self._fallback_only_mode:
                self._playlist_entries = []
                self._version = playlist_version
                self._sync_in_progress = False
                self._sync_percent = 100

        if self._fallback_only_mode:
            return

        if self._version == playlist_version and self._playlist_entries:
            return

        normalized_items = []
        for item in videos:
            if isinstance(item, dict):
                normalized_items.append(item)
            elif item:
                normalized_items.append({"path": item, "media_type": None, "duration_sec": None})

        local_entries = self.media_manager.sync_playlist_entries(
            normalized_items,
            playlist_version,
            media_signatures,
            progress_callback=self._on_sync_progress,
        )
        if local_entries:
            with self._lock:
                self._playlist_entries = local_entries
                self._version = playlist_version
                self._sync_in_progress = False
                self._sync_percent = 100
            print(f"📼 Playlist cache refreshed | version={playlist_version} items={len(local_entries)}")
            return

        fallback = self.media_manager.load_last_successful_playlist_entries()
        if fallback:
            with self._lock:
                self._playlist_entries = fallback
                self._sync_in_progress = False
            print("📦 Offline mode: last successful cache playlist ile devam ediliyor")

    def start(self):
        if self._worker and self._worker.is_alive():
            return
        self._running = True
        self._worker = threading.Thread(target=self._run, daemon=True)
        self._worker.start()
        print("▶️ playback worker started")

    def stop(self):
        self._running = False
        self.overlay.hide()
        self.player.stop()

        if self._worker and self._worker.is_alive():
            self._worker.join(timeout=1)

    def pause(self):
        self.overlay.hide()
        if self._active_item and self._active_item_started_at:
            elapsed = max(0.0, time.monotonic() - self._active_item_started_at)
            if self.player._is_video(self._active_item.get("local_path") or ""):
                self._playback_state["resume_sec"] = float(self._playback_state.get("resume_sec", 0)) + elapsed
                self._persist_playback_state()
        self.player.stop()

    def _run(self):
        try:
            while self._running:
                with self._lock:
                    playlist_entries = self._effective_playlist(list(self._playlist_entries))
                    sync_in_progress = self._sync_in_progress
                    loop_mode = self._loop_mode

                if sync_in_progress:
                    if not self.overlay.is_active():
                        self.player.stop()
                        self.overlay.show(self._overlay_text())
                    else:
                        self.overlay.update(self._overlay_text())
                elif self.overlay.is_active():
                    self.overlay.hide()

                if not playlist_entries:
                    if not self._waiting_for_media_logged:
                        print("⚠️ oynatılacak medya yok, içerik bekleniyor")
                        self._waiting_for_media_logged = True
                    time.sleep(1)
                    continue

                self._waiting_for_media_logged = False
                runtime_state = self._restore_or_init_runtime_state(playlist_entries, loop_mode)

                if loop_mode == "random":
                    order = runtime_state.get("random_order") or []
                    pos = int(runtime_state.get("random_pos") or 0)
                    if not order:
                        time.sleep(0.2)
                        continue
                    if pos >= len(order):
                        import random

                        random.shuffle(order)
                        pos = 0
                    target_path = order[pos]
                    item = next((x for x in playlist_entries if x.get("local_path") == target_path), playlist_entries[0])
                else:
                    index = int(runtime_state.get("index") or 0) % len(playlist_entries)
                    item = playlist_entries[index]

                media_path = str(item.get("local_path") or "")
                if not media_path:
                    time.sleep(0.2)
                    continue

                duration_sec = item.get("duration_sec")
                image_duration_sec = None
                if self.player.is_image(media_path):
                    if isinstance(duration_sec, int) and duration_sec > 0:
                        image_duration_sec = duration_sec
                    elif len(playlist_entries) == 1:
                        image_duration_sec = self.player.static_image_duration_sec

                resume_sec = float(runtime_state.get("resume_sec") or 0)
                self._active_item = item
                started_at = time.monotonic()
                self._active_item_started_at = started_at
                ok = self.player.play_blocking(
                    media_path,
                    image_duration_sec=image_duration_sec,
                    start_position_sec=resume_sec if resume_sec > 0 and self.player._is_video(media_path) else None,
                )
                interrupted = self.player.last_play_was_interrupted()
                self._active_item = None
                self._active_item_started_at = None

                if not ok:
                    print(f"⚠️ bozuk/oynatılamayan medya atlandı: {media_path}")

                if interrupted and self.player._is_video(media_path):
                    elapsed = max(0.0, time.monotonic() - started_at)
                    runtime_state["resume_sec"] = resume_sec + elapsed
                else:
                    runtime_state["resume_sec"] = 0
                    if loop_mode == "random":
                        runtime_state["random_pos"] = int(runtime_state.get("random_pos") or 0) + 1
                    else:
                        runtime_state["index"] = (int(runtime_state.get("index") or 0) + 1) % len(playlist_entries)

                self._persist_playback_state()
        except Exception as exc:
            print(f"❌ playback worker crashed: {exc}")
        finally:
            self._running = False

    def current_content_name(self) -> str:
        with self._lock:
            item = dict(self._active_item) if isinstance(self._active_item, dict) else None
        if not item:
            return ""
        media_path = str(item.get("local_path") or "").strip()
        return Path(media_path).name if media_path else ""


window_manager = _WindowManager()
playback = PlaybackController()
processed_command_ids = set()
processed_lock = threading.Lock()
shutdown_event = threading.Event()


def hide_console_window():
    if not platform.system().lower().startswith("win"):
        return
    try:
        kernel32 = ctypes.windll.kernel32
        user32 = ctypes.windll.user32
        hwnd = kernel32.GetConsoleWindow()
        if hwnd:
            GWL_EXSTYLE = -20
            WS_EX_APPWINDOW = 0x00040000
            WS_EX_TOOLWINDOW = 0x00000080
            SWP_NOMOVE = 0x0002
            SWP_NOSIZE = 0x0001
            SWP_NOZORDER = 0x0004
            SWP_FRAMECHANGED = 0x0020

            exstyle = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            exstyle = (exstyle & ~WS_EX_APPWINDOW) | WS_EX_TOOLWINDOW
            user32.SetWindowLongW(hwnd, GWL_EXSTYLE, exstyle)
            user32.SetWindowPos(
                hwnd,
                0,
                0,
                0,
                0,
                0,
                SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_FRAMECHANGED,
            )
            SW_HIDE = 0
            user32.ShowWindow(hwnd, SW_HIDE)
    except Exception:
        pass


class SystemTrayController:
    def __init__(self):
        self._icon = None
        self._thread = None

    def start(self):
        if not platform.system().lower().startswith("win"):
            return
        try:
            import pystray
            from PIL import Image, ImageDraw
        except Exception as exc:
            print(f"⚠️ systray başlatılamadı: {exc}")
            return

        image = Image.new("RGB", (64, 64), color="#111111")
        draw = ImageDraw.Draw(image)
        draw.rectangle((8, 8, 56, 56), outline="#00AEEF", width=4)
        draw.rectangle((20, 20, 44, 44), fill="#00AEEF")

        def on_quit(icon, _item):
            shutdown_event.set()
            icon.stop()

        self._icon = pystray.Icon(
            "baylan_signage_client",
            image,
            "Baylan Signage Client",
            menu=pystray.Menu(pystray.MenuItem("Çıkış", on_quit)),
        )
        self._thread = threading.Thread(target=self._icon.run, daemon=True)
        self._thread.start()

    def stop(self):
        if self._icon:
            try:
                self._icon.stop()
            except Exception:
                pass


systray = SystemTrayController()


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
    global playing_started_at

    if next_state == current_state:
        return

    prev = current_state
    current_state = next_state
    if next_state == ClientState.PLAYING:
        playing_started_at = time.monotonic()
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
            "state": current_state.value,
            "os_name": platform.system(),
            "content_name": playback.current_content_name(),
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
    global idle_timeout_sec, idle_mode_enabled, content_enabled

    print("📥 CONFIG RECEIVED:")
    print(data)

    config_timeout = data.get("idle_timeout_sec") if isinstance(data, dict) else None
    idle_mode_enabled = bool(data.get("idle_mode_enabled", True)) if isinstance(data, dict) else True
    content_enabled = bool(data.get("content_enabled", True)) if isinstance(data, dict) else True
    if isinstance(config_timeout, (int, float)) and config_timeout > 0:
        idle_timeout_sec = int(config_timeout)
    else:
        idle_timeout_sec = DEFAULT_IDLE_TIMEOUT_SEC

    if isinstance(data, dict):
        playback.update_from_config(data)

    print(f"🕒 idle_timeout_sec = {idle_timeout_sec} | idle_mode_enabled={idle_mode_enabled} | content_enabled={content_enabled}")


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

    if not content_enabled:
        if current_state in {ClientState.PLAYING, ClientState.IDLE_PENDING}:
            playback.stop()
            set_state(ClientState.ACTIVE, "content_disabled")
        return get_idle_seconds()

    idle_sec = get_idle_seconds()

    if not idle_mode_enabled:
        if current_state in {ClientState.PLAYING, ClientState.IDLE_PENDING, ClientState.RETURNING}:
            playback.stop()
            return_to_erp_window()
            set_state(ClientState.ACTIVE, "idle_mode_disabled")
        return idle_sec

    if current_state == ClientState.ACTIVE and idle_sec >= idle_timeout_sec:
        set_state(ClientState.IDLE_PENDING, f"idle={idle_sec:.1f}s threshold={idle_timeout_sec}s")

    if current_state == ClientState.IDLE_PENDING:
        playback.start()
        set_state(ClientState.PLAYING, "player_started")

    played_for_sec = time.monotonic() - playing_started_at
    if (
        current_state == ClientState.PLAYING
        and played_for_sec >= MIN_PLAYING_SECONDS
        and idle_sec <= ACTIVITY_RESUME_SEC
    ):
        set_state(ClientState.RETURNING, f"activity_detected idle={idle_sec:.1f}s")

    if current_state == ClientState.RETURNING:
        playback.stop()
        return_to_erp_window()
        set_state(ClientState.ACTIVE, "returned_to_erp")

    return idle_sec


def main():
    hide_console_window()
    systray.start()
    print("Connecting to:", SERVER_URL)

    while not shutdown_event.is_set():
        try:
            if not sio.connected:
                sio.connect(SERVER_URL)
            break
        except KeyboardInterrupt:
            print("🛑 Client interrupted during connect")
            return
        except Exception as e:
            print("Connection failed, retrying...", e)
            time.sleep(RECONNECT_RETRY_SEC)

    next_heartbeat_at = time.monotonic()

    while not shutdown_event.is_set():
        try:
            idle_sec = run_state_cycle()
            now = time.monotonic()
            if now >= next_heartbeat_at:
                if not sio.connected:
                    try:
                        sio.connect(SERVER_URL)
                    except Exception as reconnect_err:
                        print(f"⚠️ Reconnect failed: {reconnect_err}")
                        next_heartbeat_at = now + RECONNECT_RETRY_SEC
                        time.sleep(max(0.1, STATE_CHECK_INTERVAL_SEC))
                        continue

                try:
                    sio.emit(
                        "heartbeat",
                        {
                            "hostname": hostname,
                            "current_state": current_state.value,
                            "state": current_state.value,
                            "idle_seconds": round(idle_sec, 1),
                            "os_name": platform.system(),
                            "content_name": playback.current_content_name(),
                        },
                    )
                    print(f"💓 heartbeat sent | state={current_state.value} idle={idle_sec:.1f}s")
                except Exception as heartbeat_err:
                    print(f"⚠️ Heartbeat send failed: {heartbeat_err}")
                    next_heartbeat_at = now + RECONNECT_RETRY_SEC
                    time.sleep(max(0.1, STATE_CHECK_INTERVAL_SEC))
                    continue

                next_heartbeat_at = now + HEARTBEAT_INTERVAL_SEC
            time.sleep(max(0.1, STATE_CHECK_INTERVAL_SEC))
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
    systray.stop()


if __name__ == "__main__":
    main()
