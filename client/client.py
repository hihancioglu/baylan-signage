import json
import hashlib
import logging
import os
import platform
import re
import socket
import subprocess
import sys
import threading
import ctypes
import traceback
import faulthandler
import builtins
import shutil
import tempfile
from copy import deepcopy
from queue import Empty, Queue
from urllib import request as urllib_request
from pathlib import Path
import time
from datetime import datetime, timedelta, timezone

import socketio

CURRENT_DIR = Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

from idle import get_idle_seconds
from media_manager import MediaManager
from player import BorderlessFullscreenPlayer
from state_machine import ClientState


def _runtime_base_dir() -> Path:
    source = Path(sys.executable if getattr(sys, "frozen", False) else __file__)
    return source.resolve().parent


RUNTIME_BASE_DIR = _runtime_base_dir()


def _resolve_runtime_path(path_value: str) -> Path:
    candidate = Path(path_value).expanduser()
    if candidate.is_absolute():
        return candidate
    return RUNTIME_BASE_DIR / candidate

SERVER_URL = os.getenv("SERVER_URL", "http://baylan-portainer:5080")
SECRET = os.getenv("SHARED_SECRET", "change_me_super_secret")
DEFAULT_IDLE_TIMEOUT_SEC = int(os.getenv("DEFAULT_IDLE_TIMEOUT_SEC", "60"))
HEARTBEAT_INTERVAL_SEC = int(os.getenv("HEARTBEAT_INTERVAL_SEC", "10"))
CONFIG_PULL_INTERVAL_SEC = float(os.getenv("CONFIG_PULL_INTERVAL_SEC", "30"))
STATE_CHECK_INTERVAL_SEC = float(os.getenv("STATE_CHECK_INTERVAL_SEC", "0.5"))
RECONNECT_RETRY_SEC = float(os.getenv("RECONNECT_RETRY_SEC", "3"))
ACTIVITY_RESUME_SEC = float(os.getenv("ACTIVITY_RESUME_SEC", "1.0"))
MIN_PLAYING_SECONDS = float(os.getenv("MIN_PLAYING_SECONDS", "5.0"))
STATE_LOG_PATH = str(_resolve_runtime_path(os.getenv("STATE_LOG_PATH", "client/state_transitions.jsonl")))
ERP_WINDOW_TITLE = os.getenv("ERP_WINDOW_TITLE", "ERP")
ERP_WINDOW_MATCH_MODE = os.getenv("ERP_WINDOW_MATCH_MODE", "contains").strip().lower()
DEBUG_LOG_PATH = _resolve_runtime_path(os.getenv("CLIENT_DEBUG_LOG_PATH", "client/logs/client_debug.log"))
UPDATER_LAUNCHER_LOG_PATH = _resolve_runtime_path(
    os.getenv("UPDATER_LAUNCHER_LOG_PATH", "client/logs/updater_launcher.log")
)
EMBEDDED_CLIENT_BUILD_PATTERN = re.compile(rb"BAYLAN_CLIENT_BUILD:(build-\d{14}|\d{14})")
EMBEDDED_UPDATER_BUILD_PATTERN = re.compile(rb"BAYLAN_UPDATER_BUILD:(build-\d{14}|\d{14})")


def _read_embedded_build_version(file_path: Path, pattern: re.Pattern[bytes]) -> str | None:
    try:
        payload = file_path.read_bytes()
    except OSError:
        return None

    match = pattern.search(payload)
    if not match:
        return None

    try:
        return match.group(1).decode("utf-8")
    except UnicodeDecodeError:
        return None


def resolve_client_version() -> str:
    manual_version = (os.getenv("CLIENT_BUILD_VERSION") or "").strip()
    if manual_version:
        return manual_version

    version_source = Path(sys.executable if getattr(sys, "frozen", False) else __file__)

    embedded_version = _read_embedded_build_version(version_source, EMBEDDED_CLIENT_BUILD_PATTERN)
    if embedded_version:
        return embedded_version

    try:
        mtime = datetime.fromtimestamp(version_source.stat().st_mtime, tz=timezone.utc)
        return f"build-{mtime.strftime('%Y%m%d%H%M%S')}"
    except OSError:
        return "build-unknown"


CLIENT_VERSION = resolve_client_version()
AUTO_UPDATER_ENABLED = os.getenv("AUTO_UPDATER_ENABLED", "true").strip().lower() in {"1", "true", "yes"}
UPDATER_DOWNLOAD_DIR = _resolve_runtime_path(os.getenv("UPDATER_DOWNLOAD_DIR", "client/updates"))
UPDATER_EXECUTABLE_NAME = os.getenv("UPDATER_EXECUTABLE_NAME", "BaylanUpdater.exe")


def resolve_local_updater_version() -> str:
    updater_path = (_runtime_base_dir() / UPDATER_EXECUTABLE_NAME).resolve()
    embedded_version = _read_embedded_build_version(updater_path, EMBEDDED_UPDATER_BUILD_PATTERN)
    if embedded_version:
        return embedded_version

    try:
        mtime = datetime.fromtimestamp(updater_path.stat().st_mtime, tz=timezone.utc)
        return f"build-{mtime.strftime('%Y%m%d%H%M%S')}"
    except OSError:
        return "build-missing"


CLIENT_UPDATER_VERSION = resolve_local_updater_version()


def print(*args, **kwargs):
    try:
        builtins.print(*args, **kwargs)
    except OSError as exc:
        if getattr(exc, "winerror", None) == 6 or getattr(exc, "errno", None) == 6:
            return
        raise


def _resolve_debug_log_path() -> Path:
    candidates = [DEBUG_LOG_PATH]
    temp_dir = Path(tempfile.gettempdir())
    candidates.append(temp_dir / "baylan-client" / "client_debug.log")

    for candidate in candidates:
        try:
            candidate.parent.mkdir(parents=True, exist_ok=True)
            with open(candidate, "a", encoding="utf-8"):
                pass
            return candidate
        except OSError:
            continue

    return temp_dir / "client_debug_fallback.log"


ACTIVE_DEBUG_LOG_PATH = _resolve_debug_log_path()
INSTANCE_LOCK_PATH = Path(tempfile.gettempdir()) / "baylan-client.lock"
instance_lock_fd: int | None = None


def setup_debug_logging():
    handlers = [logging.FileHandler(ACTIVE_DEBUG_LOG_PATH, encoding="utf-8")]
    if not platform.system().lower().startswith("win"):
        handlers.append(logging.StreamHandler(sys.stdout))

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(threadName)s | %(message)s",
        handlers=handlers,
    )

    fault_log = open(ACTIVE_DEBUG_LOG_PATH, "a", encoding="utf-8")
    faulthandler.enable(file=fault_log, all_threads=True)

    def _close_fault_log():
        try:
            fault_log.close()
        except Exception:
            pass

    import atexit

    atexit.register(_close_fault_log)

    def _log_unhandled(exc_type, exc_value, exc_tb):
        logging.critical("Unhandled exception", exc_info=(exc_type, exc_value, exc_tb))

    def _log_thread_unhandled(args):
        logging.critical(
            "Unhandled thread exception",
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )

    sys.excepthook = _log_unhandled
    threading.excepthook = _log_thread_unhandled


def log_info(message: str):
    print(message)
    logging.info(message)


def flush_and_shutdown_logging():
    """Best-effort flush so last shutdown messages are not lost on forced exits."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except Exception:
            pass

    for handler in logging.getLogger().handlers:
        try:
            handler.flush()
        except Exception:
            pass

    try:
        logging.shutdown()
    except Exception:
        pass


def _pid_is_running(pid: int) -> bool:
    if pid <= 0:
        return False

    if platform.system().lower().startswith("win"):
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        SYNCHRONIZE = 0x00100000
        STILL_ACTIVE = 259

        kernel32 = ctypes.windll.kernel32
        process_handle = kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE,
            False,
            pid,
        )
        if not process_handle:
            return False

        try:
            exit_code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(process_handle, ctypes.byref(exit_code)):
                return False
            return exit_code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(process_handle)

    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def already_running() -> bool:
    """Ensure only one client process is active by acquiring a pid lock file."""
    global instance_lock_fd

    for _ in range(2):
        try:
            instance_lock_fd = os.open(str(INSTANCE_LOCK_PATH), os.O_CREAT | os.O_EXCL | os.O_RDWR)
            os.write(instance_lock_fd, str(os.getpid()).encode("utf-8"))
            return False
        except FileExistsError:
            try:
                try:
                    raw_pid = INSTANCE_LOCK_PATH.read_text(encoding="utf-8").strip()
                    existing_pid = int(raw_pid)
                except (OSError, ValueError):
                    existing_pid = 0

                if not _pid_is_running(existing_pid):
                    try:
                        INSTANCE_LOCK_PATH.unlink(missing_ok=True)
                    except OSError:
                        pass
                    continue

                log_info(f"⚠️ client zaten çalışıyor (pid={existing_pid}), çıkılıyor.")
                return True
            except Exception as lock_err:
                # Tekrarlı başlatma kontrolü çökerse istemciyi düşürmeyelim.
                # Bu durumda güvenli davranış: ikinci süreç devam etmesin.
                print(f"⚠️ lock doğrulama hatası: {lock_err}")
                return True

    return False


def release_instance_lock() -> None:
    global instance_lock_fd
    if instance_lock_fd is not None:
        try:
            os.close(instance_lock_fd)
        except OSError:
            pass
        instance_lock_fd = None
    try:
        INSTANCE_LOCK_PATH.unlink(missing_ok=True)
    except OSError:
        pass


setup_debug_logging()
log_info(f"🧾 debug logs: {ACTIVE_DEBUG_LOG_PATH}")
log_info(f"🏷️ client version: {CLIENT_VERSION}")

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
        self._enabled = platform.system().lower().startswith("win") and hasattr(ctypes, "windll")
        self._user32 = ctypes.windll.user32 if self._enabled else None

    def _normalize(self, text: str) -> str:
        return " ".join((text or "").strip().lower().split())

    def _find_window_handle(self, window_title: str) -> int:
        if not self._enabled or not window_title:
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


class GuiRuntime:
    def __init__(self):
        self._queue = Queue()
        self._thread = None
        self._started = threading.Event()
        self._shutdown = False
        self._download_overlay_visible = False
        self._state_lock = threading.Lock()

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._shutdown = False
        self._started.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="gui-thread")
        self._thread.start()
        self._started.wait(timeout=3)

    def stop(self):
        self._shutdown = True
        self.post("shutdown")
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1)

    def post(self, event_name: str, payload=None):
        self._queue.put((event_name, payload))

    def download_overlay_active(self) -> bool:
        with self._state_lock:
            return self._download_overlay_visible

    def _set_download_overlay_state(self, visible: bool):
        with self._state_lock:
            self._download_overlay_visible = visible

    def _run(self):
        try:
            import tkinter as tk
        except Exception as exc:
            logging.warning("GUI runtime başlatılamadı: %s", exc)
            self._started.set()
            return

        root = tk.Tk()
        root.withdraw()
        self._started.set()

        idle_window = None
        download_window = None
        download_label = None

        def _show_idle_overlay():
            nonlocal idle_window
            if idle_window is not None and idle_window.winfo_exists():
                return
            idle_window = tk.Toplevel(root)
            idle_window.configure(bg="black")
            idle_window.attributes("-fullscreen", True)
            idle_window.attributes("-topmost", True)
            idle_window.overrideredirect(True)
            idle_window.title("Baylan Idle Background")

        def _hide_idle_overlay():
            nonlocal idle_window
            if idle_window is not None and idle_window.winfo_exists():
                idle_window.destroy()
            idle_window = None

        def _show_download_overlay(message: str):
            nonlocal download_window, download_label
            if download_window is None or not download_window.winfo_exists():
                download_window = tk.Toplevel(root)
                download_window.configure(bg="black")
                download_window.attributes("-fullscreen", True)
                download_window.attributes("-topmost", True)
                download_window.title("Baylan Dijital Bilgi")
                download_label = tk.Label(
                    download_window,
                    text="",
                    fg="white",
                    bg="black",
                    font=("Arial", 38, "bold"),
                    justify="center",
                    wraplength=1400,
                )
                download_label.place(relx=0.5, rely=0.5, anchor="center")
            if download_label is not None:
                download_label.config(text=message)
            self._set_download_overlay_state(True)

        def _hide_download_overlay():
            nonlocal download_window, download_label
            if download_window is not None and download_window.winfo_exists():
                download_window.destroy()
            download_window = None
            download_label = None
            self._set_download_overlay_state(False)

        _next_tick = None

        def process_events():
            nonlocal _next_tick
            try:
                _next_tick = root.after(50, process_events)
            except tk.TclError:
                return

            if self._shutdown:
                _hide_download_overlay()
                _hide_idle_overlay()
                root.destroy()
                return

            while True:
                try:
                    event_name, payload = self._queue.get_nowait()
                except Empty:
                    break

                if event_name == "idle_overlay_show":
                    _show_idle_overlay()
                elif event_name == "idle_overlay_hide":
                    _hide_idle_overlay()
                elif event_name == "download_overlay_show":
                    _show_download_overlay(str(payload or ""))
                elif event_name == "download_overlay_update":
                    if self.download_overlay_active():
                        _show_download_overlay(str(payload or ""))
                elif event_name == "download_overlay_hide":
                    _hide_download_overlay()
                elif event_name == "shutdown":
                    self._shutdown = True
                    break

        root.after(50, process_events)
        root.mainloop()


class DownloadStatusOverlay:
    def __init__(self, gui_runtime: GuiRuntime):
        self._gui_runtime = gui_runtime

    def show(self, message: str):
        self._gui_runtime.post("download_overlay_show", message)

    def update(self, message: str):
        self._gui_runtime.post("download_overlay_update", message)

    def hide(self):
        self._gui_runtime.post("download_overlay_hide")

    def is_active(self) -> bool:
        return self._gui_runtime.download_overlay_active()


class IdleBackgroundOverlay:
    def __init__(self, gui_runtime: GuiRuntime):
        self._gui_runtime = gui_runtime

    def show(self):
        if not platform.system().lower().startswith("win"):
            return
        self._gui_runtime.post("idle_overlay_show")

    def hide(self):
        self._gui_runtime.post("idle_overlay_hide")


class PlaybackController:
    def __init__(self, gui_runtime: GuiRuntime):
        self.media_manager = MediaManager(
            cache_root=str(_resolve_runtime_path(os.getenv("MEDIA_CACHE_DIR", "client/cache")))
        )
        self.player = BorderlessFullscreenPlayer()
        self.overlay = DownloadStatusOverlay(gui_runtime)
        cached_entries = self.media_manager.load_last_successful_playlist_entries()
        self._playlist_entries: list[dict] = cached_entries or [
            {"local_path": p, "duration_sec": None, "media_type": None}
            for p in self.media_manager.load_last_successful_playlist()
        ]
        self._fallback_media = _resolve_runtime_path(
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
        self._playback_state_lock = threading.Lock()
        self._playback_state = self._sanitize_playback_state(self.media_manager.load_playback_state())
        self._background_overlay = IdleBackgroundOverlay(gui_runtime)

    @staticmethod
    def _sanitize_playback_state(raw_state: dict) -> dict:
        """
        Keep persisted playback state JSON-safe and limited to known primitive fields.

        This avoids traversing arbitrary object graphs from background worker threads,
        which can trigger unstable cross-thread behavior in some Windows runtimes.
        """
        state = raw_state if isinstance(raw_state, dict) else {}
        sanitized: dict = {}

        playlist_key = state.get("playlist_key")
        if playlist_key is not None:
            sanitized["playlist_key"] = str(playlist_key)

        random_order = state.get("random_order")
        if isinstance(random_order, list):
            sanitized["random_order"] = [str(item) for item in random_order if item is not None]

        try:
            sanitized["random_pos"] = max(0, int(state.get("random_pos") or 0))
        except (TypeError, ValueError):
            sanitized["random_pos"] = 0

        try:
            sanitized["index"] = max(0, int(state.get("index") or 0))
        except (TypeError, ValueError):
            sanitized["index"] = 0

        try:
            sanitized["resume_sec"] = max(0.0, float(state.get("resume_sec") or 0.0))
        except (TypeError, ValueError):
            sanitized["resume_sec"] = 0.0

        return sanitized

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
        with self._playback_state_lock:
            state = dict(self._playback_state) if isinstance(self._playback_state, dict) else {}
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
        with self._playback_state_lock:
            state_snapshot = deepcopy(self._playback_state) if isinstance(self._playback_state, dict) else {}
            state_snapshot = self._sanitize_playback_state(state_snapshot)
            self._playback_state = state_snapshot
        self.media_manager.save_playback_state(state_snapshot)

    def _can_use_mpv_playlist_mode(self, playlist_entries: list[dict]) -> bool:
        for entry in playlist_entries:
            media_path = str((entry or {}).get("local_path") or "")
            if not media_path or not self.player.is_image(media_path):
                continue

            duration_sec = (entry or {}).get("duration_sec")
            if isinstance(duration_sec, int) and duration_sec > 0 and duration_sec != self.player.image_duration_sec:
                return False

        return True

    def update_from_config(self, config: dict):
        with self._lock:
            was_fallback_only_mode = self._fallback_only_mode

        enabled = bool(config.get("enabled", True))
        videos = config.get("videos") or []
        playlist_version = config.get("playlist_version") or "default"
        media_signatures = config.get("media_signatures") or {}
        fallback_media = config.get("fallback_media")
        fallback_version = config.get("fallback_media_version") or "0"
        loop_mode = str(config.get("loop_mode") or "sequential").strip().lower()
        if loop_mode not in {"sequential", "random"}:
            loop_mode = "sequential"

        first_items = [
            f"{idx + 1}:{Path(str((item or {}).get('path') or '')).name}"
            for idx, item in enumerate(videos[:5])
            if isinstance(item, dict)
        ]
        print(
            "🧩 Playback config summary | "
            f"enabled={enabled} loop_mode={loop_mode} "
            f"playlist_version={playlist_version} items={len(videos)} "
            f"first_items={first_items}"
        )

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
            if was_fallback_only_mode:
                self.player.stop()
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
            if was_fallback_only_mode:
                self.player.stop()
            print(f"📼 Playlist cache refreshed | version={playlist_version} items={len(local_entries)}")
            return

        fallback = self.media_manager.load_last_successful_playlist_entries()
        if fallback:
            with self._lock:
                self._playlist_entries = fallback
                self._sync_in_progress = False
            if was_fallback_only_mode:
                self.player.stop()
            print("📦 Offline mode: last successful cache playlist ile devam ediliyor")

    def start(self):
        if self._worker and self._worker.is_alive():
            return
        self._background_overlay.show()
        self._running = True
        self._worker = threading.Thread(target=self._run, daemon=True)
        self._worker.start()
        print("▶️ playback worker started")

    def stop(self):
        self._running = False
        self.overlay.hide()
        self._background_overlay.hide()
        self.player.stop()

        if self._worker and self._worker.is_alive():
            self._worker.join(timeout=1)

    def pause(self):
        self.overlay.hide()
        self._background_overlay.hide()
        if self._active_item and self._active_item_started_at:
            elapsed = max(0.0, time.monotonic() - self._active_item_started_at)
            if self.player._is_video(self._active_item.get("local_path") or ""):
                with self._playback_state_lock:
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

                if loop_mode == "sequential":
                    playlist_paths = [str(entry.get("local_path") or "") for entry in playlist_entries]
                    playlist_paths = [path for path in playlist_paths if path]
                    if self._can_use_mpv_playlist_mode(playlist_entries) and self.player.can_play_with_mpv_playlist(playlist_paths):
                        self._active_item = {"local_path": "MPV Playlist", "media_type": "playlist"}
                        self._active_item_started_at = time.monotonic()
                        ok = self.player.play_mpv_playlist_blocking(playlist_paths)
                        self._active_item = None
                        self._active_item_started_at = None
                        if ok:
                            time.sleep(0.2)
                            continue

                        print("⚠️ mpv playlist oynatma başarısız, tekli oynatma moduna dönülüyor")

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
                    print(
                        "🎲 Random seçim | "
                        f"pos={pos}/{len(order)} media={Path(str(target_path)).name}"
                    )
                else:
                    index = int(runtime_state.get("index") or 0) % len(playlist_entries)
                    item = playlist_entries[index]
                    print(
                        "▶️ Sequential seçim | "
                        f"index={index}/{len(playlist_entries)} "
                        f"media={Path(str(item.get('local_path') or '')).name}"
                    )

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
            logging.exception("Playback worker crashed")
            print(f"❌ playback worker crashed: {exc}\n{traceback.format_exc()}")
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
gui_runtime = GuiRuntime()
playback = PlaybackController(gui_runtime)
idle_background = IdleBackgroundOverlay(gui_runtime)
processed_command_ids = set()
processed_lock = threading.Lock()
shutdown_event = threading.Event()
_console_hider_started = False
update_shutdown_requested = False


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
            try:
                kernel32.FreeConsole()
            except Exception:
                pass
    except Exception:
        pass


def start_console_hider():
    global _console_hider_started
    if _console_hider_started:
        return
    _console_hider_started = True

    def _worker():
        while not shutdown_event.is_set():
            hide_console_window()
            time.sleep(1)

    threading.Thread(target=_worker, daemon=True).start()


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
            f"Baylan Signage Client | {CLIENT_VERSION}",
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




def _is_newer_version(incoming: str, current: str) -> bool:
    incoming = (incoming or "").strip()
    current = (current or "").strip()
    if not incoming:
        return False
    if incoming == current:
        return False

    missing_markers = {"", "unknown", "build-unknown", "build-missing", "build-unversioned"}
    if current.strip().lower() in missing_markers and incoming.strip().lower() not in missing_markers:
        return True

    def _tokenize(value: str):
        value = value.strip().lower()
        if value.startswith("build-"):
            value = value[6:]

        out = []
        for part in re.split(r"[._-]+", value):
            if not part:
                continue
            sub_parts = re.findall(r"\d+|[a-z]+", part)
            if not sub_parts:
                out.append((1, part))
                continue
            for sub_part in sub_parts:
                if sub_part.isdigit():
                    out.append((0, int(sub_part)))
                else:
                    out.append((1, sub_part))
        return out

    try:
        return _tokenize(incoming) > _tokenize(current)
    except Exception:
        return incoming != current


def _is_missing_or_unversioned_build(version: str) -> bool:
    normalized = (version or "").strip().lower()
    return normalized in {"", "unknown", "build-unknown", "build-missing", "build-unversioned"}


def _download_release(update_info: dict) -> Path:
    url = update_info.get("url")
    file_name = update_info.get("file_name") or Path(url or "update.bin").name
    version = update_info.get("version") or "unknown"
    safe_name = f"{version}_{file_name}".replace("/", "_").replace("\\", "_")
    UPDATER_DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    target = UPDATER_DOWNLOAD_DIR / safe_name

    with urllib_request.urlopen(url, timeout=90) as resp:
        with open(target, "wb") as fh:
            shutil.copyfileobj(resp, fh)

    expected_sha = (update_info.get("sha256") or "").strip().lower()
    if expected_sha:
        file_sha = hashlib.sha256(target.read_bytes()).hexdigest().lower()
        if file_sha != expected_sha:
            target.unlink(missing_ok=True)
            raise RuntimeError("update_checksum_mismatch")

    return target


def _apply_update_package(local_file: Path):
    global update_shutdown_requested
    if platform.system().lower().startswith("win"):
        local_file = local_file.resolve()
        if local_file.suffix.lower() != ".exe":
            return "windows_update_downloaded_manual_install"

        if not getattr(sys, "frozen", False):
            return "windows_update_downloaded_manual_install"

        current_exe = Path(sys.executable).resolve()
        work_dir = current_exe.parent
        updater_exe = (work_dir / UPDATER_EXECUTABLE_NAME).resolve()
        launcher_log_file = UPDATER_LAUNCHER_LOG_PATH.resolve()
        launcher_log_file.parent.mkdir(parents=True, exist_ok=True)
        log_info(f"🛠️ updater launcher log path: {launcher_log_file}")
        if not updater_exe.exists():
            log_info(f"⚠️ Updater executable not found: {updater_exe}")
            return "windows_update_downloaded_missing_updater_exe"

        launch_args = [
            str(updater_exe),
            "--src",
            str(local_file),
            "--dst",
            str(current_exe),
            "--old-pid",
            str(os.getpid()),
            "--work-dir",
            str(work_dir),
            "--log-file",
            str(launcher_log_file),
        ]
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        subprocess.Popen(launch_args, cwd=str(work_dir), creationflags=creation_flags)

        log_info("🛑 Agent shutting down for update...")
        update_shutdown_requested = True
        shutdown_event.set()
        return "windows_update_shutdown_requested"
    return "update_downloaded_manual_install"


def _apply_client_updater_package(local_file: Path):
    if not platform.system().lower().startswith("win"):
        log_info("ℹ️ Updater swap atlandı: windows ortamı değil")
        return "client_updater_downloaded_manual_install"

    local_file = local_file.resolve()
    if local_file.suffix.lower() != ".exe":
        log_info(f"ℹ️ Updater swap atlandı: exe olmayan dosya indirildi ({local_file})")
        return "client_updater_downloaded_manual_install"

    target_updater = (_runtime_base_dir() / UPDATER_EXECUTABLE_NAME).resolve()
    target_updater.parent.mkdir(parents=True, exist_ok=True)
    log_info(f"🧩 Updater swap başlatıldı: src={local_file} dst={target_updater}")

    if local_file == target_updater:
        log_info("ℹ️ Updater swap atlandı: indirilen dosya zaten hedef updater dosyası")
        return "client_updater_already_in_place"

    for idx in range(1, 11):
        try:
            shutil.copy2(local_file, target_updater)
            local_file.unlink(missing_ok=True)
            log_info(f"✅ Updater swap başarılı: attempt={idx} dst={target_updater}")
            return "client_updater_swapped"
        except OSError as exc:
            log_info(f"⚠️ Updater swap denemesi başarısız: attempt={idx} err={exc}")
            if idx == 10:
                raise RuntimeError(f"client_updater_swap_failed:{exc}") from exc
            time.sleep(0.5)


def _maybe_run_client_updater_update(config_data):
    if not AUTO_UPDATER_ENABLED or not isinstance(config_data, dict):
        if not AUTO_UPDATER_ENABLED:
            log_info("ℹ️ Updater auto update devre dışı (AUTO_UPDATER_ENABLED=false)")
        return

    update_info = config_data.get("client_updater") or {}
    incoming_version = str(update_info.get("version") or "").strip()
    if not incoming_version:
        log_info("ℹ️ Updater update atlandı: config içinde version alanı yok")
        return

    local_version = resolve_local_updater_version()
    should_force_update = _is_missing_or_unversioned_build(local_version)
    if not should_force_update and not _is_newer_version(incoming_version, local_version):
        log_info(
            f"ℹ️ Updater update atlandı: yeni sürüm bulunamadı "
            f"(incoming={incoming_version}, current={local_version})"
        )
        return

    if should_force_update:
        log_info(
            f"⬆️ Updater sürümü dosyada bulunamadı, en güncel sürüm zorunlu indiriliyor: "
            f"{incoming_version} (current={local_version})"
        )
    else:
        log_info(f"⬆️ Yeni updater sürümü bulundu: {incoming_version} (current={local_version})")
    try:
        local_file = _download_release(update_info)
        result = _apply_client_updater_package(local_file)
        log_info(f"✅ Updater auto update sonucu: {result} | file={local_file}")
    except Exception as exc:
        log_info(f"❌ Updater auto update başarısız: {exc}")


def _maybe_run_auto_update(config_data):
    if not AUTO_UPDATER_ENABLED or not isinstance(config_data, dict):
        return

    update_info = config_data.get("updater") or {}
    incoming_version = str(update_info.get("version") or "").strip()
    if not incoming_version:
        return
    if not _is_newer_version(incoming_version, CLIENT_VERSION):
        return

    log_info(f"⬆️ Yeni client sürümü bulundu: {incoming_version} (current={CLIENT_VERSION})")
    try:
        local_file = _download_release(update_info)
        result = _apply_update_package(local_file)
        log_info(f"✅ Auto update sonucu: {result} | file={local_file}")
    except Exception as exc:
        log_info(f"❌ Auto update başarısız: {exc}")

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
            "agent_version": CLIENT_VERSION,
            "content_name": playback.current_content_name(),
        },
    )
    sio.emit("pull_config", {"hostname": hostname})


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
    if isinstance(data, dict):
        videos = data.get("videos") or []
        mode = str(data.get("loop_mode") or "sequential")
        version = str(data.get("playlist_version") or "default")
        order_map = [
            f"{idx + 1}:{Path(str((item or {}).get('path') or '')).name}"
            for idx, item in enumerate(videos)
            if isinstance(item, dict)
        ]
        print(
            "🧾 CONFIG PLAYLIST DETAIL | "
            f"mode={mode} version={version} items={len(videos)} order={order_map}"
        )

    config_timeout = data.get("idle_timeout_sec") if isinstance(data, dict) else None
    idle_mode_enabled = bool(data.get("idle_mode_enabled", True)) if isinstance(data, dict) else True
    content_enabled = bool(data.get("content_enabled", True)) if isinstance(data, dict) else True
    if isinstance(config_timeout, (int, float)) and config_timeout > 0:
        idle_timeout_sec = int(config_timeout)
    else:
        idle_timeout_sec = DEFAULT_IDLE_TIMEOUT_SEC

    if isinstance(data, dict):
        playback.update_from_config(data)
        _maybe_run_client_updater_update(data)
        _maybe_run_auto_update(data)

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
        idle_background.hide()
        if current_state != ClientState.EMERGENCY:
            set_state(ClientState.EMERGENCY, "emergency_policy_enforced")
        playback.pause()
        return get_idle_seconds()

    if not content_enabled:
        idle_background.hide()
        if current_state in {ClientState.PLAYING, ClientState.IDLE_PENDING}:
            playback.stop()
            set_state(ClientState.ACTIVE, "content_disabled")
        return get_idle_seconds()

    idle_sec = get_idle_seconds()

    if not idle_mode_enabled:
        idle_background.hide()
        if current_state in {ClientState.PLAYING, ClientState.IDLE_PENDING, ClientState.RETURNING}:
            playback.stop()
            return_to_erp_window()
            set_state(ClientState.ACTIVE, "idle_mode_disabled")
        return idle_sec

    if current_state == ClientState.ACTIVE and idle_sec >= idle_timeout_sec:
        idle_background.show()
        set_state(ClientState.IDLE_PENDING, f"idle={idle_sec:.1f}s threshold={idle_timeout_sec}s")

    if current_state == ClientState.IDLE_PENDING:
        playback.start()
        # Idle overlay'i hemen kapatırsak, player içerik açmadan önce kısa bir
        # pencere oluşabiliyor ve Windows masaüstü görünür kalabiliyor.
        # Önce PLAYING durumuna geçip içerik gerçekten seçildiğinde kapatıyoruz.
        set_state(ClientState.PLAYING, "player_started")

    if current_state == ClientState.PLAYING and playback.current_content_name():
        idle_background.hide()

    played_for_sec = time.monotonic() - playing_started_at
    if (
        current_state == ClientState.PLAYING
        and played_for_sec >= MIN_PLAYING_SECONDS
        and idle_sec <= ACTIVITY_RESUME_SEC
    ):
        set_state(ClientState.RETURNING, f"activity_detected idle={idle_sec:.1f}s")

    if current_state == ClientState.RETURNING:
        idle_background.hide()
        playback.stop()
        return_to_erp_window()
        set_state(ClientState.ACTIVE, "returned_to_erp")

    return idle_sec


def main():
    global update_shutdown_requested
    if already_running():
        return

    try:
        hide_console_window()
        start_console_hider()
        gui_runtime.start()
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
        next_config_pull_at = time.monotonic()

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
                                "agent_version": CLIENT_VERSION,
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

                if CONFIG_PULL_INTERVAL_SEC > 0 and now >= next_config_pull_at:
                    if sio.connected:
                        try:
                            sio.emit("pull_config", {"hostname": hostname})
                            print("🔄 periodic config pull requested")
                        except Exception as pull_err:
                            print(f"⚠️ Periodic config pull failed: {pull_err}")
                    next_config_pull_at = now + CONFIG_PULL_INTERVAL_SEC
                time.sleep(max(0.1, STATE_CHECK_INTERVAL_SEC))
            except KeyboardInterrupt:
                print("🛑 Client interrupted")
                break
            except Exception as e:
                logging.exception("Heartbeat loop error")
                print(f"⚠️ Heartbeat loop error, retrying: {e}\n{traceback.format_exc()}")
                time.sleep(max(RECONNECT_RETRY_SEC, STATE_CHECK_INTERVAL_SEC))
                continue

        idle_background.hide()
        playback.stop()
        try:
            sio.disconnect()
        except Exception:
            pass
        systray.stop()
        gui_runtime.stop()

        if update_shutdown_requested:
            # Updater waits for this PID to end before swapping the executable.
            # os._exit guarantees immediate process exit even if background threads are still alive.
            release_instance_lock()
            flush_and_shutdown_logging()
            os._exit(0)
    finally:
        release_instance_lock()


if __name__ == "__main__":
    main()
