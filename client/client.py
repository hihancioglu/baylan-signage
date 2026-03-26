import argparse
import json
import hashlib
import base64
import importlib
import importlib.util
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
from io import BytesIO
from logging.handlers import TimedRotatingFileHandler, WatchedFileHandler

import socketio

if platform.system().lower().startswith("win"):
    import msvcrt
else:
    import fcntl

def _module_base() -> Path:
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent)).resolve()
    return Path(__file__).resolve().parent


BASE_DIR = _module_base()
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

FROZEN_EXECUTABLE_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else None
if FROZEN_EXECUTABLE_DIR and str(FROZEN_EXECUTABLE_DIR) not in sys.path:
    sys.path.insert(0, str(FROZEN_EXECUTABLE_DIR))

for module_dir in (BASE_DIR / "client", FROZEN_EXECUTABLE_DIR / "client" if FROZEN_EXECUTABLE_DIR else None):
    if module_dir and module_dir.is_dir() and str(module_dir) not in sys.path:
        sys.path.insert(0, str(module_dir))

CLIENT_PACKAGE_PARENT = BASE_DIR.parent
if str(CLIENT_PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(CLIENT_PACKAGE_PARENT))

builtins.print(
    f"[startup] import-resolution mode={'frozen' if getattr(sys, 'frozen', False) else 'source'} "
    f"added_path={BASE_DIR}"
)


def _load_env_from_client_config() -> Path | None:
    config_candidates: list[Path] = []

    if FROZEN_EXECUTABLE_DIR:
        config_candidates.extend(
            [
                FROZEN_EXECUTABLE_DIR / "client.env.json",
                FROZEN_EXECUTABLE_DIR / "client" / "client.env.json",
            ]
        )

    config_candidates.extend(
        [
            BASE_DIR / "client.env.json",
            BASE_DIR / "client" / "client.env.json",
        ]
    )

    for candidate in config_candidates:
        if not candidate.is_file():
            continue

        try:
            payload = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            builtins.print(f"[startup] config-load-failed path={candidate} error={exc}")
            continue

        if not isinstance(payload, dict):
            builtins.print(f"[startup] config-invalid-format path={candidate} expected=object")
            continue

        for key, value in payload.items():
            if not isinstance(key, str):
                continue

            if isinstance(value, bool):
                normalized = "true" if value else "false"
            elif value is None:
                normalized = ""
            else:
                normalized = str(value)

            os.environ.setdefault(key, normalized)

        builtins.print(f"[startup] config-loaded path={candidate}")
        return candidate

    builtins.print("[startup] config-loaded path=none")
    return None


_load_env_from_client_config()


def _import_client_modules():
    def _load_by_name(module_name: str):
        return importlib.import_module(module_name)

    def _load_from_file(module_name: str):
        module_filenames = [f"{module_name}.py", f"{module_name}.pyc"]
        candidate_roots = [BASE_DIR, BASE_DIR / "client"]
        if FROZEN_EXECUTABLE_DIR:
            candidate_roots.extend([FROZEN_EXECUTABLE_DIR, FROZEN_EXECUTABLE_DIR / "client"])

        candidates = [root / module_filename for root in candidate_roots for module_filename in module_filenames]

        for candidate in candidates:
            if not candidate.is_file():
                continue

            module_spec = importlib.util.spec_from_file_location(module_name, candidate)
            if module_spec is None or module_spec.loader is None:
                continue

            module = importlib.util.module_from_spec(module_spec)
            sys.modules[module_name] = module
            module_spec.loader.exec_module(module)
            return module

        raise ModuleNotFoundError(module_name)

    if __package__ in {None, ""}:
        try:
            idle_module = _load_by_name("idle")
            media_manager_module = _load_by_name("media_manager")
            player_module = _load_by_name("player")
            state_machine_module = _load_by_name("state_machine")

            return idle_module, media_manager_module, player_module, state_machine_module
        except ModuleNotFoundError:
            try:
                idle_module = _load_by_name("client.idle")
                media_manager_module = _load_by_name("client.media_manager")
                player_module = _load_by_name("client.player")
                state_machine_module = _load_by_name("client.state_machine")

                return idle_module, media_manager_module, player_module, state_machine_module
            except ModuleNotFoundError:
                idle_module = _load_from_file("idle")
                media_manager_module = _load_from_file("media_manager")
                player_module = _load_from_file("player")
                state_machine_module = _load_from_file("state_machine")

                return idle_module, media_manager_module, player_module, state_machine_module

    from . import idle as idle_module
    from . import media_manager as media_manager_module
    from . import player as player_module
    from . import state_machine as state_machine_module

    return idle_module, media_manager_module, player_module, state_machine_module


_idle_module, _media_manager_module, _player_module, _state_machine_module = _import_client_modules()
get_idle_seconds = _idle_module.get_idle_seconds
MediaManager = _media_manager_module.MediaManager
BorderlessFullscreenPlayer = _player_module.BorderlessFullscreenPlayer
ClientState = _state_machine_module.ClientState


def _runtime_base_dir() -> Path:
    source = Path(sys.executable if getattr(sys, "frozen", False) else __file__)
    return source.resolve().parent


RUNTIME_BASE_DIR = _runtime_base_dir()


def _resolve_runtime_path(path_value: str) -> Path:
    candidate = Path(path_value).expanduser()
    if candidate.is_absolute():
        return candidate
    return RUNTIME_BASE_DIR / candidate


def _resolve_windows_writable_path(path_value: str | None, default_relative_path: str) -> Path:
    if path_value and path_value.strip():
        return _resolve_runtime_path(path_value.strip())

    if platform.system().lower().startswith("win"):
        program_data_root = Path(os.getenv("ProgramData") or r"C:\ProgramData")
        return program_data_root / "BaylanSignage" / default_relative_path

    return _resolve_runtime_path(default_relative_path)


def _env_bool(name: str, default: bool) -> bool:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    return raw_value.strip().lower() in {"1", "true", "yes", "on"}


def _is_widget_viewer_process(argv: list[str] | None = None) -> bool:
    args = argv if argv is not None else sys.argv[1:]
    normalized = {str(arg).strip().lower() for arg in args if isinstance(arg, str)}
    return "--widget" in normalized or "--runtime-ipc" in normalized


IS_WIDGET_VIEWER_PROCESS = _is_widget_viewer_process()

SERVER_URL = os.getenv("SERVER_URL", "http://baylan-portainer:5080")
SECRET = os.getenv("SHARED_SECRET", "change_me_super_secret")
DEFAULT_IDLE_TIMEOUT_SEC = int(os.getenv("DEFAULT_IDLE_TIMEOUT_SEC", "60"))
HEARTBEAT_INTERVAL_SEC = int(os.getenv("HEARTBEAT_INTERVAL_SEC", "10"))
CONFIG_PULL_INTERVAL_SEC = float(os.getenv("CONFIG_PULL_INTERVAL_SEC", "30"))
STATE_CHECK_INTERVAL_SEC = float(os.getenv("STATE_CHECK_INTERVAL_SEC", "0.5"))
RECONNECT_RETRY_SEC = float(os.getenv("RECONNECT_RETRY_SEC", "3"))
PLAYBACK_FAILURE_RETRY_SEC = float(os.getenv("PLAYBACK_FAILURE_RETRY_SEC", "0.5"))
ACTIVITY_RESUME_SEC = float(os.getenv("ACTIVITY_RESUME_SEC", "1.0"))
ACTIVITY_IDLE_DROP_SEC = float(os.getenv("ACTIVITY_IDLE_DROP_SEC", "0.4"))
ACTIVITY_DROP_CONFIRM_COUNT = int(os.getenv("ACTIVITY_DROP_CONFIRM_COUNT", "2"))
OFFLINE_IDLE_TIMEOUT_CAP_SEC = float(os.getenv("OFFLINE_IDLE_TIMEOUT_CAP_SEC", "10"))
MIN_PLAYING_SECONDS = float(os.getenv("MIN_PLAYING_SECONDS", "5.0"))
WIDGET_OVERLAY_HOLD_SEC = float(os.getenv("WIDGET_OVERLAY_HOLD_SEC", "0.8"))
WIDGET_ACTIVITY_GRACE_SEC = float(os.getenv("WIDGET_ACTIVITY_GRACE_SEC", "1.5"))
WIDGET_RETURN_REQUIRES_ERP_FOREGROUND = _env_bool("WIDGET_RETURN_REQUIRES_ERP_FOREGROUND", True)
STATE_LOG_PATH = _resolve_windows_writable_path(os.getenv("STATE_LOG_PATH"), "state_transitions.jsonl")
ERP_WINDOW_TITLE = os.getenv("ERP_WINDOW_TITLE", "ERP")
ERP_WINDOW_MATCH_MODE = os.getenv("ERP_WINDOW_MATCH_MODE", "contains").strip().lower()
DEBUG_LOG_PATH = _resolve_runtime_path(os.getenv("CLIENT_DEBUG_LOG_PATH", "client/logs/client_debug.log"))
CLIENT_DEBUG_MODE = os.getenv("CLIENT_DEBUG_MODE", "0").strip().lower() in {"1", "true", "yes", "on", "debug"}
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
SOCKETIO_LOG_ENABLED = os.getenv("SOCKETIO_LOG_ENABLED", "false").strip().lower() in {"1", "true", "yes"}
SOCKETIO_TRANSPORTS = [
    part.strip()
    for part in os.getenv("SOCKETIO_TRANSPORTS", "polling,websocket").split(",")
    if part.strip()
]
RUNTIME_TMP_DIR = _resolve_windows_writable_path(os.getenv("RUNTIME_TMP_DIR"), "RuntimeTmp")
RUNTIME_TMP_CLEANUP_ENABLED = _env_bool("RUNTIME_TMP_CLEANUP_ENABLED", True)
RUNTIME_TMP_CLEANUP_MAX_AGE_HOURS = max(1, int(os.getenv("RUNTIME_TMP_CLEANUP_MAX_AGE_HOURS", "24")))
RUNTIME_TMP_CLEANUP_MAX_ENTRIES = max(1, int(os.getenv("RUNTIME_TMP_CLEANUP_MAX_ENTRIES", "30")))
CLIENT_DEBUG_LOG_ROTATE_BACKUP_COUNT = max(1, int(os.getenv("CLIENT_DEBUG_LOG_ROTATE_BACKUP_COUNT", "30")))
AUTO_UPDATE_ROLLOUT_WINDOW_SEC = max(0, int(os.getenv("AUTO_UPDATE_ROLLOUT_WINDOW_SEC", "300")))
_rollout_waited_versions: set[str] = set()
_rollout_inflight_versions: set[str] = set()
_rollout_wait_lock = threading.Lock()
_client_updater_update_lock = threading.Lock()
_client_updater_inflight_versions: set[str] = set()
_client_updater_completed_versions: set[str] = set()
_client_update_lock = threading.Lock()
_client_update_inflight_versions: set[str] = set()
_client_update_completed_versions: set[str] = set()
_last_update_status_lock = threading.Lock()
_last_update_statuses: dict[str, str] = {
    "client": "",
    "client_updater": "",
}


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


def get_runtime_updater_version() -> str:
    return resolve_local_updater_version()


def print(*args, **kwargs):
    try:
        builtins.print(*args, **kwargs)
    except UnicodeEncodeError:
        stream = kwargs.get("file", sys.stdout)
        encoding = getattr(stream, "encoding", None) or "utf-8"

        def _safe_text(value):
            text = str(value)
            return text.encode(encoding, errors="replace").decode(encoding, errors="replace")

        safe_args = tuple(_safe_text(arg) for arg in args)
        builtins.print(*safe_args, **kwargs)
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
FAULT_LOG_PATH = ACTIVE_DEBUG_LOG_PATH.with_name(f"{ACTIVE_DEBUG_LOG_PATH.stem}_fault{ACTIVE_DEBUG_LOG_PATH.suffix}")
INSTANCE_LOCK_PATH = Path(tempfile.gettempdir()) / "baylan-client.lock"
instance_lock_fd: int | None = None
instance_mutex_handle = None


def setup_debug_logging():
    if platform.system().lower().startswith("win"):
        file_handler = TimedRotatingFileHandler(
            ACTIVE_DEBUG_LOG_PATH,
            when="midnight",
            interval=1,
            backupCount=CLIENT_DEBUG_LOG_ROTATE_BACKUP_COUNT,
            encoding="utf-8",
        )
        file_handler.suffix = "%Y-%m-%d"
    else:
        # Linux'ta dış logrotate kullanıldığında dosya inode'u değişir.
        # WatchedFileHandler, değişikliği algılayıp yeni dosyayı otomatik açar.
        file_handler = WatchedFileHandler(ACTIVE_DEBUG_LOG_PATH, encoding="utf-8")
    handlers = [file_handler]
    if not platform.system().lower().startswith("win"):
        handlers.append(logging.StreamHandler(sys.stdout))

    logging.basicConfig(
        level=logging.DEBUG if CLIENT_DEBUG_MODE else logging.INFO,
        format="%(asctime)s | %(levelname)s | %(threadName)s | %(message)s",
        handlers=handlers,
    )

    fault_log = open(FAULT_LOG_PATH, "a", encoding="utf-8")
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


def log_debug(message: str):
    if not CLIENT_DEBUG_MODE:
        return
    print(f"[DEBUG] {message}")
    logging.debug(message)


def log_warning(message: str):
    print(message)
    logging.warning(message)


def log_error(message: str):
    print(message)
    logging.error(message)


def _set_update_status(channel: str, status: str) -> None:
    normalized_channel = (channel or "").strip().lower()
    if not normalized_channel:
        return
    with _last_update_status_lock:
        _last_update_statuses[normalized_channel] = (status or "").strip()


def _get_update_status_payload() -> dict[str, str]:
    with _last_update_status_lock:
        return {
            "client_update_status": _last_update_statuses.get("client", ""),
            "client_updater_status": _last_update_statuses.get("client_updater", ""),
        }


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
    """Ensure only one client process is active via a non-blocking file lock."""
    global instance_lock_fd, instance_mutex_handle

    if platform.system().lower().startswith("win"):
        mutex_name = "Global\\BaylanSignageClientMainInstance"
        kernel32 = ctypes.windll.kernel32
        mutex_handle = kernel32.CreateMutexW(None, False, mutex_name)
        if not mutex_handle:
            log_warning("⚠️ instance mutex oluşturulamadı, güvenli tarafta çıkılıyor.")
            return True

        ERROR_ALREADY_EXISTS = 183
        if kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
            log_info("⚠️ client zaten çalışıyor (mutex), çıkılıyor.")
            kernel32.CloseHandle(mutex_handle)
            return True

        instance_mutex_handle = mutex_handle

    try:
        instance_lock_fd = os.open(str(INSTANCE_LOCK_PATH), os.O_CREAT | os.O_RDWR, 0o644)
    except OSError as open_err:
        print(f"⚠️ lock dosyası açılamadı: {open_err}")
        return True

    try:
        if platform.system().lower().startswith("win"):
            os.lseek(instance_lock_fd, 0, os.SEEK_SET)
            os.write(instance_lock_fd, b" ")
            os.lseek(instance_lock_fd, 0, os.SEEK_SET)
            msvcrt.locking(instance_lock_fd, msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(instance_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError):
        try:
            os.lseek(instance_lock_fd, 0, os.SEEK_SET)
            raw_pid = os.read(instance_lock_fd, 64).decode("utf-8", errors="ignore").strip()
        except OSError:
            raw_pid = ""
        log_info(f"⚠️ client zaten çalışıyor (pid={raw_pid or 'unknown'}), çıkılıyor.")
        try:
            os.close(instance_lock_fd)
        except OSError:
            pass
        instance_lock_fd = None
        return True

    os.ftruncate(instance_lock_fd, 0)
    os.lseek(instance_lock_fd, 0, os.SEEK_SET)
    os.write(instance_lock_fd, str(os.getpid()).encode("utf-8"))
    os.fsync(instance_lock_fd)
    return False


def _resolve_widget_launch_options(
    *,
    clone_requested: bool,
    requested_target_monitor_index: int | None,
    has_multiple_monitors: bool,
) -> tuple[int | None, bool]:
    normalized_target_monitor_index = (
        requested_target_monitor_index
        if isinstance(requested_target_monitor_index, int) and requested_target_monitor_index >= 0
        else None
    )
    clone_enabled = bool(clone_requested) and os.name == "nt" and has_multiple_monitors
    if clone_enabled:
        # Klon modunda tüm monitörler için ayrı komutlar üretilmesi gerekir.
        # Hedef monitör index'i verilmiş olsa bile bunu yok sayıp çoklu komut
        # yoluna düşür.
        return None, True
    return normalized_target_monitor_index, False


def _has_multiple_connected_monitors() -> bool:
    if os.name != "nt":
        return False
    try:
        return len(BorderlessFullscreenPlayer._windows_connected_monitor_bounds()) > 1
    except Exception:
        return False


def release_instance_lock() -> None:
    global instance_lock_fd, instance_mutex_handle
    if instance_lock_fd is not None:
        try:
            if platform.system().lower().startswith("win"):
                os.lseek(instance_lock_fd, 0, os.SEEK_SET)
                msvcrt.locking(instance_lock_fd, msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(instance_lock_fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(instance_lock_fd)
        except OSError:
            pass
        instance_lock_fd = None

    if platform.system().lower().startswith("win") and instance_mutex_handle:
        try:
            ctypes.windll.kernel32.CloseHandle(instance_mutex_handle)
        except Exception:
            pass
        instance_mutex_handle = None


def _start_parent_watchdog(parent_pid: int | None) -> None:
    if not isinstance(parent_pid, int) or parent_pid <= 0:
        return

    def _watch_parent():
        while True:
            if not _pid_is_running(parent_pid):
                os._exit(0)
            time.sleep(1.0)

    threading.Thread(target=_watch_parent, daemon=True).start()


def _is_same_or_child_path(path: Path, maybe_parent: Path) -> bool:
    try:
        path.resolve().relative_to(maybe_parent.resolve())
        return True
    except ValueError:
        return False


def cleanup_runtime_tmp_dir(
    runtime_tmp_dir: Path | None = None,
    max_age_hours: int | None = None,
    now_utc: datetime | None = None,
) -> None:
    target_root = (runtime_tmp_dir or RUNTIME_TMP_DIR).expanduser()
    if not target_root.exists():
        return

    age_hours = max_age_hours if max_age_hours is not None else RUNTIME_TMP_CLEANUP_MAX_AGE_HOURS
    age_hours = max(1, int(age_hours))
    current_time = now_utc or datetime.now(timezone.utc)
    cutoff_timestamp = (current_time - timedelta(hours=age_hours)).timestamp()

    active_runtime_dir: Path | None = None
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            active_runtime_dir = Path(meipass).resolve()

    max_entries = max(1, int(RUNTIME_TMP_CLEANUP_MAX_ENTRIES))

    cleaned_count = 0
    cleaned_by_age_count = 0
    cleaned_by_count_count = 0
    skipped_active_count = 0
    skipped_recent_count = 0
    error_count = 0

    try:
        entries = list(target_root.iterdir())
    except OSError as exc:
        log_warning(f"⚠️ RuntimeTmp okunamadı: path={target_root} err={exc}")
        return

    eligible_entries: list[tuple[Path, float]] = []

    for entry in entries:
        if active_runtime_dir and (
            _is_same_or_child_path(entry, active_runtime_dir) or _is_same_or_child_path(active_runtime_dir, entry)
        ):
            skipped_active_count += 1
            continue

        try:
            stat_info = entry.stat()
        except OSError:
            continue

        eligible_entries.append((entry, stat_info.st_mtime))

    delete_targets: dict[Path, str] = {}

    for entry, mtime in eligible_entries:
        if mtime < cutoff_timestamp:
            delete_targets[entry] = "age"

    recent_entries = [(entry, mtime) for entry, mtime in eligible_entries if entry not in delete_targets]
    recent_entries.sort(key=lambda item: item[1], reverse=True)
    if len(recent_entries) > max_entries:
        for entry, _ in recent_entries[max_entries:]:
            delete_targets[entry] = "count"


    for entry, reason in delete_targets.items():
        try:
            if entry.is_dir():
                shutil.rmtree(entry)
            else:
                entry.unlink(missing_ok=True)
            cleaned_count += 1
            if reason == "age":
                cleaned_by_age_count += 1
            elif reason == "count":
                cleaned_by_count_count += 1
        except OSError as exc:
            error_count += 1
            log_warning(f"⚠️ RuntimeTmp silinemedi: path={entry} reason={reason} err={exc}")

    skipped_recent_count = max(0, len(recent_entries) - cleaned_by_count_count)

    log_info(
        "🧹 RuntimeTmp cleanup tamamlandı | "
        f"path={target_root} cleaned={cleaned_count} "
        f"cleaned_by_age={cleaned_by_age_count} cleaned_by_count={cleaned_by_count_count} "
        f"kept_recent={skipped_recent_count} max_entries={max_entries} "
        f"skipped_active={skipped_active_count} errors={error_count}"
    )


if not IS_WIDGET_VIEWER_PROCESS:
    setup_debug_logging()
    log_info(f"🧾 debug logs: {ACTIVE_DEBUG_LOG_PATH}")
    log_info(f"🏷️ client version: {CLIENT_VERSION}")
    log_info(f"🐞 client debug mode: {CLIENT_DEBUG_MODE}")

sio = socketio.Client(
    reconnection=False,
    logger=SOCKETIO_LOG_ENABLED,
    engineio_logger=SOCKETIO_LOG_ENABLED,
)
hostname = socket.gethostname()
connection_lock = threading.Lock()
next_connect_attempt_at = 0.0
connection_outage_active = False

idle_timeout_sec = DEFAULT_IDLE_TIMEOUT_SEC
idle_mode_enabled = True
content_enabled = True
current_state = ClientState.ACTIVE
_last_observed_idle_sec: float | None = None
_activity_drop_streak = 0
_low_idle_streak = 0
emergency_active = False
work_order_alert_active = False
work_order_alert_message = "İŞEMRİ BAŞLATILMAMIŞ"
announcement_active = False
announcement_message = ""
announcement_display_mode = "normal"
call_feature_enabled = False
active_call = None
playing_started_at = 0.0

SUPPORTED_COMMANDS = {
    "REFRESH_CONFIG",
    "EMERGENCY_START",
    "EMERGENCY_STOP",
    "RESTART_AGENT",
    "PING",
    "CAPTURE_SCREEN",
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

    def is_window_foreground(self, window_title: str) -> bool:
        if not self._enabled:
            return False

        hwnd = self._find_window_handle(window_title)
        if not hwnd:
            return False

        get_foreground_window = getattr(self._user32, "GetForegroundWindow", None)
        if get_foreground_window is None:
            return False

        foreground_hwnd = int(get_foreground_window())
        return foreground_hwnd == int(hwnd)


class GuiRuntime:
    def __init__(self):
        self._queue = Queue()
        self._thread = None
        self._started = threading.Event()
        self._shutdown = False
        self._download_overlay_visible = False
        self._work_order_overlay_visible = False
        self._call_overlay_visible = False
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

    def work_order_overlay_active(self) -> bool:
        with self._state_lock:
            return self._work_order_overlay_visible

    def _set_work_order_overlay_state(self, visible: bool):
        with self._state_lock:
            self._work_order_overlay_visible = visible

    def call_overlay_active(self) -> bool:
        with self._state_lock:
            return self._call_overlay_visible

    def _set_call_overlay_state(self, visible: bool):
        with self._state_lock:
            self._call_overlay_visible = visible

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
        work_order_window = None
        work_order_label = None
        work_order_container = None
        call_window = None
        call_menu_window = None
        call_button = None
        call_button_circle = None
        call_button_label = None
        call_feature_active = False
        call_has_active_request = False

        def _work_order_geometry(screen_w: int, screen_h: int) -> str:
            panel_w = max(640, int(screen_w * 0.7))
            panel_h = max(180, int(screen_h * 0.28))
            panel_w = min(panel_w, max(640, screen_w - 80))
            panel_h = min(panel_h, max(180, screen_h - 80))
            x = max(0, (screen_w - panel_w) // 2)
            y = max(0, (screen_h - panel_h) // 2)
            return f"{panel_w}x{panel_h}+{x}+{y}"

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


        work_order_flash_job = None
        work_order_flash_on = False

        def _set_work_order_colors(is_flash_on: bool):
            nonlocal work_order_container, work_order_label
            bg_color = "#FFFFFF" if is_flash_on else "#8B0000"
            fg_color = "#000000" if is_flash_on else "#FFFFFF"
            if work_order_container is not None and work_order_container.winfo_exists():
                work_order_container.configure(bg=bg_color)
            if work_order_label is not None and work_order_label.winfo_exists():
                work_order_label.configure(bg=bg_color, fg=fg_color)

        def _start_work_order_flash():
            nonlocal work_order_window, work_order_flash_job, work_order_flash_on
            if work_order_window is None or not work_order_window.winfo_exists():
                return
            work_order_flash_on = not work_order_flash_on
            _set_work_order_colors(work_order_flash_on)
            work_order_flash_job = work_order_window.after(800, _start_work_order_flash)

        def _stop_work_order_flash():
            nonlocal work_order_window, work_order_flash_job, work_order_flash_on
            if work_order_flash_job is not None and work_order_window is not None and work_order_window.winfo_exists():
                try:
                    work_order_window.after_cancel(work_order_flash_job)
                except tk.TclError:
                    pass
            work_order_flash_job = None
            work_order_flash_on = False
            _set_work_order_colors(False)

        def _show_work_order_overlay(message: str, flash: bool = False):
            nonlocal work_order_window, work_order_label, work_order_container, work_order_flash_job, work_order_flash_on
            if work_order_window is None or not work_order_window.winfo_exists():
                work_order_window = tk.Toplevel(root)
                work_order_window.configure(bg="black")
                work_order_window.attributes("-topmost", True)
                work_order_window.overrideredirect(True)
                work_order_window.title("Baylan İş Emri Uyarısı")
                work_order_window.geometry(
                    _work_order_geometry(
                        work_order_window.winfo_screenwidth(),
                        work_order_window.winfo_screenheight(),
                    )
                )
                work_order_container = tk.Frame(
                    work_order_window,
                    bg="#8B0000",
                    bd=6,
                    relief="ridge",
                )
                work_order_container.place(relx=0.5, rely=0.5, relwidth=1.0, relheight=1.0, anchor="center")
                work_order_label = tk.Label(
                    work_order_container,
                    text="",
                    fg="white",
                    bg="#8B0000",
                    font=("Arial", 52, "bold"),
                    justify="center",
                    wraplength=max(600, int(work_order_window.winfo_screenwidth() * 0.62)),
                )
                work_order_label.place(relx=0.5, rely=0.5, anchor="center")
            try:
                work_order_window.attributes("-topmost", True)
                work_order_window.lift()
            except tk.TclError:
                pass
            if work_order_label is not None:
                work_order_label.config(text=message)
            if flash:
                if work_order_flash_job is None:
                    _start_work_order_flash()
            else:
                _stop_work_order_flash()
            self._set_work_order_overlay_state(True)

        def _hide_work_order_overlay():
            nonlocal work_order_window, work_order_label, work_order_container
            _stop_work_order_flash()
            if work_order_window is not None and work_order_window.winfo_exists():
                work_order_window.destroy()
            work_order_window = None
            work_order_label = None
            work_order_container = None
            self._set_work_order_overlay_state(False)

        def _close_call_menu():
            nonlocal call_menu_window
            if call_menu_window is not None and call_menu_window.winfo_exists():
                call_menu_window.destroy()
            call_menu_window = None

        def _emit_call_request(role: str):
            role_value = str(role or "").strip()
            if not role_value:
                return
            try:
                sio.emit("call_request_create", {"hostname": hostname, "requested_role": role_value})
            except Exception as exc:
                log_warning(f"call_request_create emit failed | error={exc}")
            finally:
                _close_call_menu()

        def _open_call_menu():
            nonlocal call_menu_window
            if call_menu_window is not None and call_menu_window.winfo_exists():
                try:
                    call_menu_window.lift()
                except tk.TclError:
                    pass
                return

            parent = call_window if call_window is not None and call_window.winfo_exists() else root
            call_menu_window = tk.Toplevel(parent)
            call_menu_window.configure(bg="#0F172A")
            call_menu_window.attributes("-topmost", True)
            call_menu_window.overrideredirect(True)
            call_menu_window.title("Baylan Çağrı Menüsü")

            screen_w = call_menu_window.winfo_screenwidth()
            screen_h = call_menu_window.winfo_screenheight()
            panel_w = min(900, max(640, int(screen_w * 0.58)))
            panel_h = min(760, max(580, int(screen_h * 0.62)))
            x = max(20, (screen_w - panel_w) // 2)
            y = max(20, (screen_h - panel_h) // 2)
            call_menu_window.geometry(f"{panel_w}x{panel_h}+{x}+{y}")

            container = tk.Frame(call_menu_window, bg="#0F172A", bd=8, relief="ridge")
            container.pack(fill="both", expand=True)

            title = tk.Label(
                container,
                text="Kime çağrı açılacak?",
                fg="white",
                bg="#0F172A",
                font=("Arial", 34, "bold"),
            )
            title.pack(pady=(24, 16))

            role_frame = tk.Frame(container, bg="#0F172A")
            role_frame.pack(fill="x", padx=28, pady=(0, 8))

            for role in ("Vardiya Amiri", "Bilgi İşlem", "Bakımcı"):
                role_btn = tk.Button(
                    role_frame,
                    text=role,
                    command=lambda value=role: _emit_call_request(value),
                    font=("Arial", 30, "bold"),
                    bg="#0284C7",
                    fg="white",
                    activebackground="#0369A1",
                    activeforeground="white",
                    relief="flat",
                    padx=24,
                    pady=14,
                )
                role_btn.pack(fill="x", pady=9)

            close_btn = tk.Button(
                container,
                text="Kapat / Geri",
                command=_close_call_menu,
                font=("Arial", 22, "bold"),
                bg="#334155",
                fg="white",
                activebackground="#1E293B",
                activeforeground="white",
                relief="flat",
                padx=18,
                pady=12,
            )
            close_btn.pack(fill="x", padx=28, pady=(12, 20))

        def _emit_call_cancel():
            try:
                sio.emit("call_request_cancel", {"hostname": hostname})
            except Exception as exc:
                log_warning(f"call_request_cancel emit failed | error={exc}")

        def _show_call_overlay(enabled: bool, has_active: bool, active_role: str = ""):
            nonlocal call_window, call_button, call_button_circle, call_button_label, call_feature_active, call_has_active_request
            call_feature_active = bool(enabled)
            call_has_active_request = bool(has_active)

            if not call_feature_active:
                _hide_call_overlay()
                return

            if call_window is None or not call_window.winfo_exists():
                call_window = tk.Toplevel(root)
                transparent_bg = "#FF00FF"
                call_window.configure(bg=transparent_bg)
                call_window.attributes("-topmost", True)
                call_window.overrideredirect(True)
                call_window.title("Baylan Çağır Paneli")
                try:
                    call_window.wm_attributes("-transparentcolor", transparent_bg)
                except tk.TclError:
                    pass
                screen_w = call_window.winfo_screenwidth()
                screen_h = call_window.winfo_screenheight()
                panel_w = 120
                panel_h = 120
                x = 20
                y = max(20, screen_h - panel_h - 90)
                call_window.geometry(f"{panel_w}x{panel_h}+{x}+{y}")

                call_button = tk.Canvas(
                    call_window,
                    width=104,
                    height=104,
                    bg=transparent_bg,
                    highlightthickness=0,
                    bd=0,
                )
                call_button_circle = call_button.create_oval(2, 2, 102, 102, fill="#2563EB", outline="#1D4ED8", width=3)
                call_button_label = call_button.create_text(52, 52, text="Çağır", fill="white", font=("Arial", 20, "bold"))
                call_button.tag_bind(call_button_circle, "<Button-1>", lambda _e: _on_call_button_click())
                call_button.tag_bind(call_button_label, "<Button-1>", lambda _e: _on_call_button_click())
                call_button.place(x=8, y=8)

            if call_button is not None and call_button_circle is not None and call_button_label is not None:
                if has_active:
                    role_text = str(active_role or "").strip()
                    button_text = role_text if role_text else "Çağrı Aktif"
                    call_button.itemconfig(call_button_circle, fill="#DC2626", outline="#B91C1C")
                    call_button.itemconfig(call_button_label, text=button_text, font=("Arial", 11, "bold"))
                    _close_call_menu()
                else:
                    call_button.itemconfig(call_button_circle, fill="#2563EB", outline="#1D4ED8")
                    call_button.itemconfig(call_button_label, text="Çağır", font=("Arial", 20, "bold"))

            self._set_call_overlay_state(True)

        def _on_call_button_click():
            if call_has_active_request:
                _emit_call_cancel()
                return
            _open_call_menu()

        def _hide_call_overlay():
            nonlocal call_window, call_menu_window, call_button, call_button_circle, call_button_label
            _close_call_menu()
            if call_window is not None and call_window.winfo_exists():
                call_window.destroy()
            call_window = None
            call_button = None
            call_button_circle = None
            call_button_label = None
            self._set_call_overlay_state(False)

        _next_tick = None

        def process_events():
            nonlocal _next_tick
            try:
                _next_tick = root.after(50, process_events)
            except tk.TclError:
                return

            if self._shutdown:
                _hide_download_overlay()
                _hide_work_order_overlay()
                _hide_idle_overlay()
                _hide_call_overlay()
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
                elif event_name == "work_order_alert_show":
                    message = str(payload.get("message") or "İŞEMRİ BAŞLATILMAMIŞ") if isinstance(payload, dict) else str(payload or "İŞEMRİ BAŞLATILMAMIŞ")
                    flash = bool(payload.get("flash", False)) if isinstance(payload, dict) else False
                    _show_work_order_overlay(message, flash)
                elif event_name == "work_order_alert_update":
                    if self.work_order_overlay_active():
                        message = str(payload.get("message") or "İŞEMRİ BAŞLATILMAMIŞ") if isinstance(payload, dict) else str(payload or "İŞEMRİ BAŞLATILMAMIŞ")
                        flash = bool(payload.get("flash", False)) if isinstance(payload, dict) else False
                        _show_work_order_overlay(message, flash)
                elif event_name == "work_order_alert_hide":
                    _hide_work_order_overlay()
                elif event_name == "call_overlay_update":
                    payload = payload if isinstance(payload, dict) else {}
                    _show_call_overlay(
                        bool(payload.get("enabled")),
                        bool(payload.get("active")),
                        str(payload.get("role") or "").strip(),
                    )
                elif event_name == "call_overlay_hide":
                    _hide_call_overlay()
                elif event_name == "shutdown":
                    self._shutdown = True
                    break

            if work_order_window is not None and work_order_window.winfo_exists():
                try:
                    work_order_window.attributes("-topmost", True)
                    work_order_window.lift()
                except tk.TclError:
                    pass
            if call_window is not None and call_window.winfo_exists():
                try:
                    call_window.attributes("-topmost", True)
                    call_window.lift()
                except tk.TclError:
                    pass
            if call_menu_window is not None and call_menu_window.winfo_exists():
                try:
                    call_menu_window.attributes("-topmost", True)
                    call_menu_window.lift()
                except tk.TclError:
                    pass

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


class WorkOrderAlertOverlay:
    def __init__(self, gui_runtime: GuiRuntime):
        self._gui_runtime = gui_runtime

    def show(self, message: str, flash: bool = False):
        self._gui_runtime.post("work_order_alert_show", {"message": message, "flash": bool(flash)})

    def update(self, message: str, flash: bool = False):
        self._gui_runtime.post("work_order_alert_update", {"message": message, "flash": bool(flash)})

    def hide(self):
        self._gui_runtime.post("work_order_alert_hide")

    def is_active(self) -> bool:
        return self._gui_runtime.work_order_overlay_active()


class CallRequestOverlay:
    def __init__(self, gui_runtime: GuiRuntime):
        self._gui_runtime = gui_runtime

    def update(self, enabled: bool, active: bool, role: str = ""):
        self._gui_runtime.post(
            "call_overlay_update",
            {"enabled": bool(enabled), "active": bool(active), "role": str(role or "").strip()},
        )

    def hide(self):
        self._gui_runtime.post("call_overlay_hide")

    def is_active(self) -> bool:
        return self._gui_runtime.call_overlay_active()


class MultiMonitorPlayback:
    def __init__(self, media_manager: MediaManager):
        self.media_manager = media_manager
        self._monitor_states: dict[int, dict] = {}
        self._monitor_versions: dict[int, str] = {}
        self._players: dict[int, BorderlessFullscreenPlayer] = {}
        self._workers: dict[int, threading.Thread] = {}
        self._running_monitors: dict[int, bool] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _use_windows_display_ids() -> bool:
        return os.getenv("MONITOR_PLAYLIST_USE_WINDOWS_DISPLAY_IDS", "1").strip().lower() in {"1", "true", "yes", "on"}

    @staticmethod
    def _connected_monitor_count() -> int | None:
        if os.name != "nt":
            return None
        bounds = BorderlessFullscreenPlayer._windows_connected_monitor_bounds()
        if not bounds:
            return None
        count = max(1, len(bounds))
        # Windows servis/uzak oturum senaryolarında monitör enumerasyonu
        # güvenilir olmayıp tek ekran döndürebiliyor. Bu durumda çoklu
        # monitör playlist'lerini hatalı biçimde devre dışı bırakmamak için
        # filtreyi uygulamayalım.
        if count <= 1:
            return None
        return count

    @staticmethod
    def _windows_monitor_id_to_index_map() -> dict[int, int]:
        if os.name != "nt":
            return {}
        return BorderlessFullscreenPlayer._windows_monitor_id_to_index_map()

    @classmethod
    def _windows_primary_monitor_id(cls) -> int | None:
        monitor_map = cls._windows_monitor_id_to_index_map()
        if not monitor_map:
            return 1
        for monitor_id, monitor_index in monitor_map.items():
            if int(monitor_index) == 0:
                return int(monitor_id)
        return 1

    @classmethod
    def _target_monitor_index_for_monitor_no(cls, monitor_no: int) -> int:
        if os.name == "nt" and cls._use_windows_display_ids():
            monitor_map = cls._windows_monitor_id_to_index_map()
            mapped_index = monitor_map.get(int(monitor_no))
            if mapped_index is not None:
                return int(mapped_index)
        return max(0, int(monitor_no) - 1)

    def update_from_config(self, monitor_playlists_payload: dict | None):
        monitor_states: dict[int, dict] = {}
        monitor_playlists = monitor_playlists_payload if isinstance(monitor_playlists_payload, dict) else {}
        connected_monitor_count = self._connected_monitor_count()
        windows_monitor_map = self._windows_monitor_id_to_index_map()
        primary_monitor_id = self._windows_primary_monitor_id()
        use_windows_display_ids = self._use_windows_display_ids()
        for monitor_no_text, payload in monitor_playlists.items():
            try:
                monitor_no = int(monitor_no_text)
            except (TypeError, ValueError):
                continue
            if monitor_no < 1:
                continue
            if os.name == "nt":
                if use_windows_display_ids and windows_monitor_map:
                    if monitor_no == primary_monitor_id or monitor_no not in windows_monitor_map:
                        continue
                elif connected_monitor_count is not None and monitor_no > connected_monitor_count:
                    continue
                elif monitor_no < 2:
                    continue
            elif monitor_no < 2:
                continue
            if not isinstance(payload, dict):
                continue

            # Ana playlist davranışıyla uyumlu olarak "enabled" alanı yoksa
            # monitor playlist'ini varsayılan olarak açık kabul et.
            enabled = bool(payload.get("enabled", True))
            videos = payload.get("videos") or []
            playlist_version = str(payload.get("playlist_version") or f"monitor{monitor_no}-empty")
            media_signatures = payload.get("media_signatures") or {}
            loop_mode = str(payload.get("loop_mode") or "sequential").strip().lower()
            if loop_mode not in {"sequential", "random"}:
                loop_mode = "sequential"

            normalized_items = []
            widget_items = []
            for item in videos:
                if not isinstance(item, dict):
                    continue
                item_type = str(item.get("item_type") or "media").strip().lower()
                if item_type == "widget":
                    widget_items.append(
                        {
                            "item_type": "widget",
                            "widget_url": str(item.get("widget_url") or item.get("path") or "").strip(),
                            "widget_payload": item.get("widget_payload"),
                            "duration_sec": item.get("duration_sec"),
                        }
                    )
                    continue
                normalized_items.append(
                    {
                        "path": item.get("path"),
                        "media_type": item.get("media_type"),
                        "duration_sec": item.get("duration_sec"),
                        "item_type": "media",
                    }
                )

            if enabled and normalized_items:
                local_entries = self.media_manager.sync_playlist_entries(
                    normalized_items,
                    f"monitor{monitor_no}-{playlist_version}",
                    media_signatures,
                    progress_callback=None,
                )
            else:
                local_entries = []

            entries = list(local_entries or []) + widget_items
            monitor_states[monitor_no] = {
                "enabled": bool(enabled and entries),
                "entries": entries,
                "loop_mode": loop_mode,
                "playlist_version": playlist_version,
            }

        players_to_restart: list[BorderlessFullscreenPlayer] = []
        with self._lock:
            previous_versions = dict(self._monitor_versions)
            self._monitor_states = monitor_states
            self._monitor_versions = {
                monitor_no: str((state or {}).get("playlist_version") or "")
                for monitor_no, state in monitor_states.items()
            }
            for monitor_no, player in self._players.items():
                if player is None:
                    continue
                if previous_versions.get(monitor_no) != self._monitor_versions.get(monitor_no):
                    players_to_restart.append(player)

        for player in players_to_restart:
            try:
                player.stop()
            except Exception:
                pass

    def has_active_playlist(self) -> bool:
        with self._lock:
            return any(state.get("enabled") and state.get("entries") for state in self._monitor_states.values())

    def start(self):
        with self._lock:
            monitor_numbers = sorted(self._monitor_states.keys())
        for monitor_no in monitor_numbers:
            self._ensure_worker(monitor_no)

    def stop(self):
        workers: list[threading.Thread] = []
        with self._lock:
            monitor_numbers = set(self._workers.keys()) | set(self._players.keys()) | set(self._running_monitors.keys())
            for monitor_no in monitor_numbers:
                self._running_monitors[monitor_no] = False
                player = self._players.get(monitor_no)
                if player:
                    player.stop()
                worker = self._workers.get(monitor_no)
                if worker and worker.is_alive():
                    workers.append(worker)
        for worker in workers:
            worker.join(timeout=1)

    def pause(self):
        with self._lock:
            players = list(self._players.values())
        for player in players:
            player.stop()

    def _ensure_worker(self, monitor_no: int):
        with self._lock:
            existing = self._workers.get(monitor_no)
            if existing and existing.is_alive():
                return
            self._running_monitors[monitor_no] = True
            worker = threading.Thread(target=self._run, args=(monitor_no,), daemon=True)
            self._workers[monitor_no] = worker
        worker.start()

    @staticmethod
    def _normalize_widget_config(widget_payload: object) -> dict | None:
        payload = widget_payload
        if isinstance(payload, str):
            text_payload = payload.strip()
            if text_payload:
                try:
                    decoded_payload = json.loads(text_payload)
                except Exception:
                    decoded_payload = None
                payload = decoded_payload
            else:
                payload = None

        if isinstance(payload, list):
            return {"widgets": list(payload)}

        if not isinstance(payload, dict):
            return None

        payload_copy = dict(payload)
        widgets = payload_copy.get("widgets")
        if isinstance(widgets, list):
            normalized = {"widgets": list(widgets)}
            if isinstance(payload_copy.get("columns"), (int, list)):
                normalized["columns"] = payload_copy.get("columns")
            if isinstance(payload_copy.get("rows"), int):
                normalized["rows"] = payload_copy.get("rows")
            return normalized

        widget_type = str(payload_copy.get("type") or "").strip().lower()
        if widget_type:
            return {"widgets": [payload_copy]}
        return None

    def _run(self, monitor_no: int):
        index = 0
        while True:
            with self._lock:
                if not self._running_monitors.get(monitor_no, False):
                    return
                monitor_state = dict(self._monitor_states.get(monitor_no) or {})
                enabled = bool(monitor_state.get("enabled"))
                entries = list(monitor_state.get("entries") or [])
                loop_mode = str(monitor_state.get("loop_mode") or "sequential")

            if not enabled or not entries:
                time.sleep(0.5)
                continue

            if loop_mode == "random":
                import random
                item = random.choice(entries)
            else:
                item = entries[index % len(entries)]
                index = (index + 1) % len(entries)

            media_path = str(item.get("local_path") or "")

            with self._lock:
                player = self._players.get(monitor_no)
                if player is None:
                    player = BorderlessFullscreenPlayer(keep_widget_runtime_warm=False)
                    self._players[monitor_no] = player
            item_type = str(item.get("item_type") or "media").strip().lower()
            if item_type == "widget":
                widget_url = str(item.get("widget_url") or item.get("path") or "").strip()
                widget_payload = item.get("widget_payload")
                widget_config = self._normalize_widget_config(widget_payload)
                raw_widget_duration = item.get("duration_sec")
                try:
                    widget_duration_sec = int(raw_widget_duration)
                except (TypeError, ValueError):
                    widget_duration_sec = 0
                if widget_duration_sec <= 0:
                    if len(entries) == 1:
                        widget_duration_sec = max(30, int(os.getenv("SECONDARY_WIDGET_PERSIST_SEC", "3600")))
                    else:
                        widget_duration_sec = 15
                if not widget_url and not widget_config:
                    time.sleep(0.2)
                    continue
                target_monitor_index = self._target_monitor_index_for_monitor_no(monitor_no)
                widget_target_monitor_index, widget_clone_enabled = _resolve_widget_launch_options(
                    clone_requested=False,
                    requested_target_monitor_index=target_monitor_index,
                    has_multiple_monitors=True,
                )
                player.play_widget_blocking(
                    widget_url,
                    int(widget_duration_sec),
                    widget_config=widget_config,
                    target_monitor_index=widget_target_monitor_index,
                    clone_to_all_monitors=widget_clone_enabled,
                )
            else:
                if not media_path:
                    time.sleep(0.2)
                    continue
                duration_sec = item.get("duration_sec")
                media_duration_sec = duration_sec if isinstance(duration_sec, int) and duration_sec > 0 else None
                target_monitor_index = self._target_monitor_index_for_monitor_no(monitor_no)
                log_debug(
                    "monitor_media_launch | "
                    f"monitor_no={monitor_no} target_monitor_index={target_monitor_index} "
                    f"path={media_path} media_duration_sec={media_duration_sec}"
                )
                player.play_blocking(
                    media_path,
                    image_duration_sec=media_duration_sec,
                    target_monitor_index=target_monitor_index,
                    clone_to_all_monitors=False,
                )


class PlaybackController:
    def __init__(self, gui_runtime: GuiRuntime):
        self.media_manager = MediaManager(
            cache_root=str(_resolve_windows_writable_path(os.getenv("MEDIA_CACHE_DIR"), "cache"))
        )
        self.player = BorderlessFullscreenPlayer()
        self.overlay = DownloadStatusOverlay(gui_runtime)
        cached_entries = self.media_manager.load_last_successful_playlist_entries()
        self._playlist_entries: list[dict] = cached_entries or [
            {"local_path": p, "duration_sec": None, "media_type": None, "item_type": "media", "display_name": None}
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
        self._active_widget_signature: str | None = None
        self._prewarmed_widget_signature: str | None = None
        # Klon modu devre dışı: her monitör bağımsız playlist ile yönetilir.
        self._clone_to_all_monitors = False
        self.multi_monitor_playback = MultiMonitorPlayback(self.media_manager)

        # İlk idle geçişinde mpv'den widget viewer'a geçerken masaüstü parlamasını
        # azaltmak için widget runtime'ı varsayılan olarak önceden ayağa kaldır.
        if (
            os.getenv("WIDGET_PREWARM_ON_STARTUP", "1").strip().lower() in {"1", "true", "yes"}
            and not _is_widget_viewer_process()
        ):
            self.player.start_widget_engine_if_needed()

    def _primary_target_monitor_index(self) -> int | None:
        if os.name != "nt":
            return None
        if self._clone_to_all_monitors:
            return None
        if not self.multi_monitor_playback.has_active_playlist():
            return None
        return 0

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

    @staticmethod
    def _normalize_video_resume_sec(resume_sec: float, media_duration_sec: int | None) -> float:
        if not isinstance(resume_sec, (int, float)):
            return 0.0

        safe_resume = max(0.0, float(resume_sec))
        if not isinstance(media_duration_sec, int) or media_duration_sec <= 0:
            return safe_resume

        wrapped = safe_resume % float(media_duration_sec)
        if wrapped >= max(0.0, float(media_duration_sec) - 0.25):
            return 0.0
        return wrapped

    @staticmethod
    def _normalize_widget_columns(columns):
        if isinstance(columns, list):
            return columns
        if isinstance(columns, int) and columns > 0:
            return columns
        return None

    @staticmethod
    def _widget_config_has_playable_content(widget_config: dict | None) -> bool:
        if not isinstance(widget_config, dict):
            return False

        widgets = widget_config.get("widgets")
        if not isinstance(widgets, list) or not widgets:
            return False

        for widget in widgets:
            if not isinstance(widget, dict):
                continue

            widget_type = str(widget.get("type") or "").strip().lower()
            if widget_type == "empty":
                continue

            if widget_type in {"iframe", "url", "video", "image"}:
                candidate = str(widget.get("url") or widget.get("content") or widget.get("source") or "").strip()
                if candidate:
                    return True
                continue

            if widget_type == "embed":
                html = str(widget.get("html") or widget.get("content") or "").strip()
                if html:
                    return True
                continue

            # Bilinmeyen tipleri, custom renderer tarafından oynatılabilir olabilir varsayımıyla kabul et.
            return True

        return False

    @staticmethod
    def _derive_widget_source_from_config(widget_config: dict | None) -> str:
        if not isinstance(widget_config, dict):
            return ""

        widgets = widget_config.get("widgets")
        if not isinstance(widgets, list):
            return ""

        for widget in widgets:
            if not isinstance(widget, dict):
                continue

            widget_type = str(widget.get("type") or "").strip().lower()
            if widget_type in {"iframe", "url", "video", "image"}:
                candidate = str(widget.get("url") or widget.get("content") or widget.get("source") or "").strip()
                if candidate:
                    return candidate
                continue

            if widget_type == "embed":
                candidate = str(widget.get("html") or widget.get("content") or "").strip()
                if candidate:
                    return candidate

        return ""

    @staticmethod
    def _parse_dashboard_widget_payload(raw_payload: dict) -> dict | None:
        if not isinstance(raw_payload, dict):
            return None

        widget_type = str(raw_payload.get("type") or "").strip().lower()
        if widget_type != "dashboard":
            return None

        raw_content = str(raw_payload.get("content") or "").strip()
        if not raw_content:
            return None

        name = str(raw_payload.get("name") or "").strip()

        def _with_name(payload: dict[str, object]) -> dict[str, object]:
            normalized_payload = dict(payload)
            if name:
                normalized_payload["name"] = name
            return normalized_payload

        lowered_content = raw_content.lower()
        if lowered_content.startswith(("http://", "https://")):
            return _with_name({"widgets": [{"type": "iframe", "url": raw_content}]})

        try:
            parsed = json.loads(raw_content)
        except (TypeError, ValueError, json.JSONDecodeError):
            parsed = None

        if isinstance(parsed, dict):
            widgets = parsed.get("widgets")
            if isinstance(widgets, list):
                normalized: dict[str, object] = {"widgets": list(widgets)}
                columns = PlaybackController._normalize_widget_columns(parsed.get("columns"))
                if columns is not None:
                    normalized["columns"] = columns

                rows = parsed.get("rows")
                if isinstance(rows, int) and rows > 0:
                    normalized["rows"] = rows

                return _with_name(normalized)

        looks_like_embed_html = any(tag in lowered_content for tag in ("<iframe", "<script", "<html", "<body"))
        if looks_like_embed_html:
            return _with_name({"widgets": [{"type": "embed", "html": raw_content}]})

        return _with_name({"widgets": [{"type": "iframe", "url": raw_content}]})

    @staticmethod
    def _normalize_item(item: dict) -> dict:
        normalized = dict(item) if isinstance(item, dict) else {}
        normalized["item_type"] = str(normalized.get("item_type") or normalized.get("media_type") or "media").strip().lower() or "media"
        normalized["display_name"] = str(normalized.get("title") or normalized.get("name") or normalized.get("display_name") or "").strip() or None
        if normalized["item_type"] == "widget":
            normalized["media_type"] = normalized.get("media_type") or "widget"
            widget_payload = normalized.get("widget_payload")
            if isinstance(widget_payload, dict):
                normalized["widget_payload"] = deepcopy(widget_payload)
            elif isinstance(widget_payload, list):
                normalized["widget_payload"] = list(widget_payload)
            elif isinstance(widget_payload, str):
                parsed_widget_payload = None
                text_payload = widget_payload.strip()
                if text_payload:
                    try:
                        decoded_widget_payload = json.loads(text_payload)
                    except (TypeError, ValueError, json.JSONDecodeError):
                        decoded_widget_payload = None
                    if isinstance(decoded_widget_payload, dict):
                        parsed_widget_payload = deepcopy(decoded_widget_payload)
                    elif isinstance(decoded_widget_payload, list):
                        parsed_widget_payload = list(decoded_widget_payload)
                normalized["widget_payload"] = parsed_widget_payload
            else:
                normalized["widget_payload"] = None

            widget_url = str(normalized.get("widget_url") or normalized.get("local_path") or normalized.get("path") or "").strip()
            normalized["widget_url"] = widget_url or None

            normalized["columns"] = PlaybackController._normalize_widget_columns(normalized.get("columns"))
        return normalized

    @staticmethod
    def _item_label(item: dict) -> str:
        normalized = PlaybackController._normalize_item(item or {})
        media_name = Path(str(normalized.get("local_path") or normalized.get("path") or "")).name
        widget_name = str(normalized.get("widget_url") or "").strip()
        return str(normalized.get("display_name") or media_name or widget_name)

    @staticmethod
    def _resolve_widget_duration_sec(item: dict) -> int:
        default_duration = max(1, int(os.getenv("WIDGET_DEFAULT_DURATION_SEC", "30")))
        duration = (item or {}).get("duration_sec")
        if isinstance(duration, int) and duration > 0:
            return duration
        return default_duration

    def _build_widget_playback_spec(
        self,
        item: dict,
        playlist_index: int | None = None,
    ) -> tuple[str, dict | None, str] | None:
        normalized = self._normalize_item(item or {})
        if normalized.get("item_type") != "widget":
            return None

        raw_widget_payload = normalized.get("widget_payload")
        if isinstance(raw_widget_payload, dict):
            parsed_dashboard_payload = self._parse_dashboard_widget_payload(raw_widget_payload)
            if parsed_dashboard_payload:
                raw_widget_payload = parsed_dashboard_payload
        payload_columns = None
        widget_entries = None
        if isinstance(raw_widget_payload, list):
            widget_entries = raw_widget_payload
        elif isinstance(raw_widget_payload, dict):
            if isinstance(raw_widget_payload.get("widgets"), list):
                widget_entries = raw_widget_payload.get("widgets")
            elif raw_widget_payload:
                widget_entries = [raw_widget_payload]
            payload_columns = self._normalize_widget_columns(raw_widget_payload.get("columns"))

        resolved_columns = self._normalize_widget_columns(normalized.get("columns"))
        if resolved_columns is None:
            resolved_columns = payload_columns

        widget_config = {
            "widgets": widget_entries,
            "columns": resolved_columns,
        }
        widget_url = str(normalized.get("widget_url") or normalized.get("local_path") or "").strip()
        if not widget_url:
            widget_url = self._derive_widget_source_from_config(widget_config)
        if widget_config["widgets"] is None and widget_url:
            widget_config["widgets"] = [{"type": "url", "url": widget_url}]
        if widget_config["columns"] is None:
            widget_config.pop("columns", None)
        if widget_config.get("widgets") is None:
            widget_config = None

        if not widget_url and not self._widget_config_has_playable_content(widget_config):
            log_debug("widget spec skipped | reason=no_playable_widget_source")
            return None

        widget_signature = hashlib.sha256(
            json.dumps(
                {
                    "widget_url": widget_url,
                    "widget_config": widget_config,
                    "playlist_index": playlist_index,
                },
                ensure_ascii=False,
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        return widget_url, widget_config, widget_signature

    def _prewarm_next_widget(
        self,
        playlist_entries: list[dict],
        loop_mode: str,
        runtime_state: dict,
        just_played_index: int | None = None,
    ) -> None:
        if loop_mode != "sequential" or not playlist_entries:
            return

        if just_played_index is None:
            current_index = int(runtime_state.get("index") or 0) % len(playlist_entries)
        else:
            current_index = int(just_played_index) % len(playlist_entries)
        next_index = (current_index + 1) % len(playlist_entries)
        next_item = playlist_entries[next_index]
        next_spec = self._build_widget_playback_spec(next_item, playlist_index=next_index)
        if not next_spec:
            return

        widget_url, widget_config, widget_signature = next_spec
        if widget_signature == self._active_widget_signature:
            return

        if self.player.update_widget_layout(widget_url, widget_config=widget_config, widget_signature=widget_signature):
            self._prewarmed_widget_signature = widget_signature

    def _sync_widget_runtime_playlist(
        self,
        playlist_entries: list[dict],
        active_widget_signature: str | None = None,
    ) -> None:
        runtime_items: list[dict] = []
        for index, entry in enumerate(playlist_entries):
            widget_spec = self._build_widget_playback_spec(entry, playlist_index=index)
            if not widget_spec:
                continue
            widget_url, widget_config, widget_signature = widget_spec
            if self.player.is_direct_url_widget(widget_config):
                continue
            runtime_items.append(
                {
                    "signature": widget_signature,
                    "widget_source": widget_url,
                    "widget_config": widget_config,
                }
            )

        self.player.sync_widget_runtime_playlist(runtime_items, active_signature=active_widget_signature)

    def _can_use_mpv_playlist_mode(self, playlist_entries: list[dict]) -> bool:
        return False

    def update_from_config(self, config: dict):
        with self._lock:
            was_fallback_only_mode = self._fallback_only_mode

        monitor_playlists = config.get("monitor_playlists") if isinstance(config, dict) else {}
        primary_payload = monitor_playlists.get("1") if isinstance(monitor_playlists, dict) else None
        selected_payload = primary_payload if isinstance(primary_payload, dict) else config

        enabled = bool(selected_payload.get("enabled", True))
        videos = selected_payload.get("videos") or []
        playlist_version = selected_payload.get("playlist_version") or "default"
        media_signatures = selected_payload.get("media_signatures") or {}
        fallback_media = config.get("fallback_media")
        fallback_version = config.get("fallback_media_version") or "0"
        loop_mode = str(selected_payload.get("loop_mode") or "sequential").strip().lower()
        if loop_mode not in {"sequential", "random"}:
            loop_mode = "sequential"
        self.multi_monitor_playback.update_from_config(monitor_playlists)
        clone_flag_raw = selected_payload.get("clone_to_all_monitors")
        if clone_flag_raw is None and isinstance(config, dict):
            clone_flag_raw = config.get("clone_to_all_monitors")
        if clone_flag_raw is None:
            clone_requested = False
        else:
            clone_requested = bool(clone_flag_raw)
        self._clone_to_all_monitors = clone_requested

        normalized_items = []
        for item in videos:
            if isinstance(item, dict):
                normalized_items.append(self._normalize_item(item))
            elif item:
                normalized_items.append(self._normalize_item({"path": item, "media_type": None, "duration_sec": None}))

        first_items = [
            f"{idx + 1}:{self._item_label(item)}[{item.get('item_type')}]"
            for idx, item in enumerate(normalized_items[:5])
        ]
        print(
            "🧩 Playback config summary | "
            f"enabled={enabled} loop_mode={loop_mode} "
            f"playlist_version={playlist_version} items={len(normalized_items)} "
            f"clone_to_all_monitors={self._clone_to_all_monitors} "
            f"first_items={first_items}"
        )

        fallback_playlist = []
        if fallback_media:
            fallback_entries = self.media_manager.sync_playlist_entries(
                [{"path": fallback_media, "media_type": "image", "duration_sec": None, "item_type": "media"}],
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

        local_entries = self.media_manager.sync_playlist_entries(
            normalized_items,
            playlist_version,
            media_signatures,
            progress_callback=self._on_sync_progress,
        )
        if local_entries:
            with self._lock:
                self._playlist_entries = [self._normalize_item(entry) for entry in local_entries]
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
                self._playlist_entries = [self._normalize_item(entry) for entry in fallback]
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
        self.multi_monitor_playback.start()
        print("▶️ playback worker started")

    def stop(self, stop_widget_runtime: bool | None = None):
        self._running = False
        self.overlay.hide()
        self._background_overlay.hide()
        self.player.stop(stop_widget_runtime=stop_widget_runtime)
        self.multi_monitor_playback.stop()

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
        self.multi_monitor_playback.pause()

    def _run(self):
        try:
            while self._running:
                with self._lock:
                    playlist_entries = [self._normalize_item(entry) for entry in self._effective_playlist(list(self._playlist_entries))]
                    sync_in_progress = self._sync_in_progress
                    loop_mode = self._loop_mode

                if sync_in_progress:
                    if not playlist_entries:
                        if not self.overlay.is_active():
                            self.player.stop()
                            self.overlay.show(self._overlay_text())
                        else:
                            self.overlay.update(self._overlay_text())
                    elif self.overlay.is_active():
                        self.overlay.hide()
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
                log_debug(f"playback loop | entries={len(playlist_entries)} loop_mode={loop_mode} state={runtime_state}")
                self._sync_widget_runtime_playlist(playlist_entries, active_widget_signature=self._active_widget_signature)

                if loop_mode == "sequential":
                    playlist_paths = [str(entry.get("local_path") or "") for entry in playlist_entries if entry.get("item_type") != "widget"]
                    playlist_paths = [path for path in playlist_paths if path]
                    if self._can_use_mpv_playlist_mode(playlist_entries) and self.player.can_play_with_mpv_playlist(playlist_paths):
                        log_debug(f"mpv playlist mode selected | media_count={len(playlist_paths)}")
                        self._active_item = {"local_path": "MPV Playlist", "media_type": "playlist", "item_type": "media", "display_name": "MPV Playlist"}
                        self._active_item_started_at = time.monotonic()
                        ok = self.player.play_mpv_playlist_blocking(
                            playlist_paths,
                            clone_to_all_monitors=self._clone_to_all_monitors,
                        )
                        log_debug(f"mpv playlist play result | ok={ok}")
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
                    log_debug(f"random pick | target_path={target_path}")
                    playlist_index = playlist_entries.index(item)
                    print(
                        "🎲 Random seçim | "
                        f"pos={pos}/{len(order)} content={self._item_label(item)}"
                    )
                else:
                    index = int(runtime_state.get("index") or 0) % len(playlist_entries)
                    item = playlist_entries[index]
                    log_debug(f"sequential pick | index={index} item_type={item.get('item_type')}")
                    playlist_index = index
                    print(
                        "▶️ Sequential seçim | "
                        f"index={index}/{len(playlist_entries)} "
                        f"content={self._item_label(item)}"
                    )

                item_type = str(item.get("item_type") or "media").strip().lower()
                media_path = str(item.get("local_path") or "")

                duration_sec = item.get("duration_sec")
                resume_sec = float(runtime_state.get("resume_sec") or 0)
                self._active_item = item
                started_at = time.monotonic()
                self._active_item_started_at = started_at

                if item_type == "widget":
                    log_debug(f"widget playback start | item={self._item_label(item)} playlist_index={playlist_index}")
                    widget_duration_sec = self._resolve_widget_duration_sec(item)
                    if not (isinstance(duration_sec, int) and duration_sec > 0):
                        print(
                            f"⚠️ widget duration_sec belirtilmemiş, varsayılan uygulanıyor: {widget_duration_sec}s | widget={self._item_label(item)}"
                        )

                    widget_spec = self._build_widget_playback_spec(item, playlist_index=playlist_index)
                    direct_url_widget = False
                    if widget_spec is None:
                        ok = False
                    else:
                        widget_url, widget_config, widget_signature = widget_spec
                        log_debug(f"widget spec | direct_url_candidate={self.player.is_direct_url_widget(widget_config)} signature={widget_signature}")
                        direct_url_widget = self.player.is_direct_url_widget(widget_config)
                        has_multiple_monitors = False
                        monitor_bounds_reader = getattr(self.player, "_windows_connected_monitor_bounds", None)
                        if callable(monitor_bounds_reader):
                            try:
                                has_multiple_monitors = len(monitor_bounds_reader()) > 1
                            except Exception:
                                has_multiple_monitors = False
                        requested_target_monitor_index = self._primary_target_monitor_index()
                        primary_target_monitor_index, clone_widget_to_all_monitors = _resolve_widget_launch_options(
                            clone_requested=self._clone_to_all_monitors,
                            requested_target_monitor_index=requested_target_monitor_index,
                            has_multiple_monitors=has_multiple_monitors,
                        )
                        log_debug(
                            "widget_route_decision | "
                            f"direct_url_widget={direct_url_widget} "
                            f"clone_widget_to_all_monitors={clone_widget_to_all_monitors} "
                            f"has_multiple_monitors={has_multiple_monitors} "
                            f"primary_target_monitor_index={primary_target_monitor_index}"
                        )
                        force_process_widget_playback = False
                        if force_process_widget_playback:
                            # Çoklu monitör klonlamada widget runtime controller tek pencere
                            # yönettiği için ikincil monitörler boş kalabiliyor. Ayrıca
                            # monitöre hedefli oynatımda her monitör için ayrı süreç/komut
                            # üretilmesi gerektiğinden bu durumda süreç tabanlı oynatımı zorla.
                            self._active_widget_signature = None
                            self._prewarmed_widget_signature = None
                            ok = self.player.play_widget_blocking(
                                widget_url,
                                widget_duration_sec,
                                widget_config=widget_config,
                                target_monitor_index=primary_target_monitor_index,
                                clone_to_all_monitors=clone_widget_to_all_monitors,
                            )
                        else:
                            if widget_signature == self._prewarmed_widget_signature:
                                ok = True
                            else:
                                ok = self.player.update_widget_layout(
                                widget_url,
                                widget_config=widget_config,
                                widget_signature=widget_signature,
                                target_monitor_index=primary_target_monitor_index,
                                clone_to_all_monitors=clone_widget_to_all_monitors,
                            )
                            self._active_widget_signature = widget_signature if ok else None
                            log_debug(f"widget runtime layout update result | ok={ok} active_signature={self._active_widget_signature}")
                            self._prewarmed_widget_signature = None
                            self._sync_widget_runtime_playlist(
                                playlist_entries,
                                active_widget_signature=self._active_widget_signature,
                            )

                    if ok and not direct_url_widget:
                        self.player.clear_stop_request()
                        ok = self.player.wait_widget_duration(widget_duration_sec)
                    log_debug(f"widget playback end | ok={ok} direct_url_widget={direct_url_widget}")
                    interrupted = self.player.last_play_was_interrupted()
                    log_debug(f"widget playback interrupt flag | interrupted={interrupted}")
                else:
                    if not media_path:
                        time.sleep(0.2)
                        continue

                    self._active_widget_signature = None
                    self._sync_widget_runtime_playlist(playlist_entries, active_widget_signature=None)

                    media_duration_sec = None
                    is_video_media = self.player._is_video(media_path)
                    if self.player.is_image(media_path):
                        if isinstance(duration_sec, int) and duration_sec > 0:
                            media_duration_sec = duration_sec
                        elif len(playlist_entries) == 1:
                            media_duration_sec = self.player.static_image_duration_sec
                        else:
                            media_duration_sec = self.player.image_duration_sec
                    else:
                        if isinstance(duration_sec, int) and duration_sec > 0:
                            media_duration_sec = duration_sec
                        else:
                            media_duration_sec = None

                    effective_resume_sec = resume_sec
                    if is_video_media:
                        effective_resume_sec = self._normalize_video_resume_sec(resume_sec, media_duration_sec)
                        if effective_resume_sec != resume_sec:
                            log_debug(
                                "media resume normalized | "
                                f"path={media_path} original_resume_sec={resume_sec} "
                                f"effective_resume_sec={effective_resume_sec} media_duration_sec={media_duration_sec}"
                            )
                            resume_sec = effective_resume_sec

                    log_debug(f"media playback start | path={media_path} resume_sec={resume_sec} media_duration_sec={media_duration_sec}")
                    if is_video_media and media_duration_sec is None:
                        ok = self.player.play_blocking(
                            media_path,
                            start_position_sec=effective_resume_sec if effective_resume_sec > 0 else None,
                            target_monitor_index=self._primary_target_monitor_index(),
                            clone_to_all_monitors=self._clone_to_all_monitors,
                        )
                    else:
                        if self._clone_to_all_monitors:
                            ok = self.player.play_media_in_widget_runtime_blocking(
                                media_path,
                                media_duration_sec,
                                start_position_sec=effective_resume_sec if effective_resume_sec > 0 and is_video_media else None,
                            )
                        else:
                            ok = self.player.play_blocking(
                                media_path,
                                image_duration_sec=media_duration_sec,
                                start_position_sec=effective_resume_sec if effective_resume_sec > 0 and is_video_media else None,
                                target_monitor_index=self._primary_target_monitor_index(),
                                clone_to_all_monitors=False,
                            )
                    interrupted = self.player.last_play_was_interrupted()
                    log_debug(f"media playback end | ok={ok} interrupted={interrupted} path={media_path}")

                self._active_item = None
                self._active_item_started_at = None

                if not self._running:
                    log_debug(
                        "playback loop stop detected after item outcome | "
                        f"item_type={item_type} content={self._item_label(item)}"
                    )
                    break

                if not ok:
                    print(f"⚠️ bozuk/oynatılamayan içerik atlandı: {self._item_label(item)}")
                    if not interrupted:
                        retry_delay_sec = max(0.0, PLAYBACK_FAILURE_RETRY_SEC)
                        if retry_delay_sec > 0:
                            log_debug(
                                "playback failure retry delay | "
                                f"delay_sec={retry_delay_sec} item_type={item_type} content={self._item_label(item)}"
                            )
                            time.sleep(retry_delay_sec)
                            if not self._running:
                                log_debug(
                                    "playback retry aborted because stop requested | "
                                    f"item_type={item_type} content={self._item_label(item)}"
                                )
                                break

                played_index = None
                if loop_mode == "sequential":
                    played_index = int(runtime_state.get("index") or 0) % len(playlist_entries)

                if interrupted and item_type != "widget" and self.player._is_video(media_path):
                    elapsed = max(0.0, time.monotonic() - started_at)
                    runtime_state["resume_sec"] = self._normalize_video_resume_sec(resume_sec + elapsed, media_duration_sec)
                else:
                    runtime_state["resume_sec"] = 0
                    if loop_mode == "random":
                        runtime_state["random_pos"] = int(runtime_state.get("random_pos") or 0) + 1
                    else:
                        runtime_state["index"] = (int(runtime_state.get("index") or 0) + 1) % len(playlist_entries)

                log_debug(f"playback outcome | ok={ok} interrupted={interrupted} item_type={item_type} resume_sec={runtime_state.get('resume_sec')}")
                self._prewarm_next_widget(
                    playlist_entries,
                    loop_mode,
                    runtime_state,
                    just_played_index=played_index,
                )
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
        label = self._item_label(item)
        if label:
            return label
        media_path = str(item.get("local_path") or "").strip()
        return Path(media_path).name if media_path else ""


window_manager = _WindowManager()
if IS_WIDGET_VIEWER_PROCESS:
    gui_runtime = None
    playback = None
    idle_background = None
    work_order_alert_overlay = None
    call_request_overlay = None
else:
    gui_runtime = GuiRuntime()
    playback = PlaybackController(gui_runtime)
    idle_background = IdleBackgroundOverlay(gui_runtime)
    work_order_alert_overlay = WorkOrderAlertOverlay(gui_runtime)
    call_request_overlay = CallRequestOverlay(gui_runtime)
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


def _parse_published_at(value: str | None):
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _wait_for_rollout_slot(channel: str, version: str, published_at: str | None = None) -> None:
    if AUTO_UPDATE_ROLLOUT_WINDOW_SEC <= 0:
        return

    rollout_key = f"{channel}:{version}"
    with _rollout_wait_lock:
        if rollout_key in _rollout_waited_versions or rollout_key in _rollout_inflight_versions:
            return
        _rollout_inflight_versions.add(rollout_key)

    try:
        seed = f"{hostname}:{channel}:{version}".encode("utf-8")
        digest = hashlib.sha256(seed).digest()
        delay_sec = int.from_bytes(digest[:4], "big") % (AUTO_UPDATE_ROLLOUT_WINDOW_SEC + 1)
        published_at_dt = _parse_published_at(published_at)
        if published_at_dt:
            now_utc = datetime.now(timezone.utc)
            if published_at_dt.tzinfo is None:
                now_utc = now_utc.replace(tzinfo=None)
            elapsed_sec = max(0, int((now_utc - published_at_dt).total_seconds()))
            delay_sec = max(0, delay_sec - elapsed_sec)
        if delay_sec > 0:
            log_info(
                f"⏳ Rollout dengeleme beklemesi: kanal={channel} sürüm={version} gecikme={delay_sec}s"
            )
            shutdown_event.wait(delay_sec)
    finally:
        with _rollout_wait_lock:
            _rollout_inflight_versions.discard(rollout_key)
            _rollout_waited_versions.add(rollout_key)


def _download_release(update_info: dict) -> Path:
    url = update_info.get("url")
    file_name = update_info.get("file_name") or Path(url or "update.bin").name
    version = update_info.get("version") or "unknown"
    safe_name = f"{version}_{file_name}".replace("/", "_").replace("\\", "_")
    UPDATER_DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    target = UPDATER_DOWNLOAD_DIR / safe_name

    downloaded_size = 0
    with urllib_request.urlopen(url, timeout=90) as resp:
        with open(target, "wb") as fh:
            while True:
                chunk = resp.read(1024 * 1024)
                if not chunk:
                    break
                fh.write(chunk)
                downloaded_size += len(chunk)

    expected_size = int(update_info.get("size") or 0)
    if expected_size > 0 and downloaded_size != expected_size:
        target.unlink(missing_ok=True)
        raise RuntimeError(f"update_size_mismatch:expected={expected_size}:actual={downloaded_size}")

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
        return

    local_version = resolve_local_updater_version()
    should_force_update = _is_missing_or_unversioned_build(local_version)
    if not should_force_update and not _is_newer_version(incoming_version, local_version):
        return

    if should_force_update:
        log_info(
            f"⬆️ Updater sürümü dosyada bulunamadı, en güncel sürüm zorunlu indiriliyor: "
            f"{incoming_version} (current={local_version})"
        )
    else:
        log_info(f"⬆️ Yeni updater sürümü bulundu: {incoming_version} (current={local_version})")

    with _client_updater_update_lock:
        if incoming_version in _client_updater_completed_versions:
            log_info(f"ℹ️ Updater auto update atlandı: sürüm zaten işlendi ({incoming_version})")
            return
        if incoming_version in _client_updater_inflight_versions:
            log_info(f"ℹ️ Updater auto update atlandı: sürüm zaten başka thread tarafından işleniyor ({incoming_version})")
            return
        _client_updater_inflight_versions.add(incoming_version)

    try:
        _wait_for_rollout_slot("client_updater", incoming_version, update_info.get("published_at"))
        local_file = _download_release(update_info)
        result = _apply_client_updater_package(local_file)
        _set_update_status("client_updater", f"ok:{incoming_version}")
        log_info(f"✅ Updater auto update sonucu: {result} | file={local_file}")
        with _client_updater_update_lock:
            _client_updater_completed_versions.add(incoming_version)
    except Exception as exc:
        _set_update_status("client_updater", f"failed:{incoming_version}:{exc}")
        log_info(f"❌ Updater auto update başarısız: {exc}")
    finally:
        with _client_updater_update_lock:
            _client_updater_inflight_versions.discard(incoming_version)



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

    with _client_update_lock:
        if incoming_version in _client_update_completed_versions:
            log_info(f"ℹ️ Auto update atlandı: sürüm zaten işlendi ({incoming_version})")
            return
        if incoming_version in _client_update_inflight_versions:
            log_info(f"ℹ️ Auto update atlandı: sürüm zaten başka thread tarafından işleniyor ({incoming_version})")
            return
        _client_update_inflight_versions.add(incoming_version)

    try:
        _wait_for_rollout_slot("client", incoming_version, update_info.get("published_at"))
        local_file = _download_release(update_info)
        result = _apply_update_package(local_file)
        _set_update_status("client", f"ok:{incoming_version}")
        log_info(f"✅ Auto update sonucu: {result} | file={local_file}")
        with _client_update_lock:
            _client_update_completed_versions.add(incoming_version)
    except Exception as exc:
        _set_update_status("client", f"failed:{incoming_version}:{exc}")
        log_info(f"❌ Auto update başarısız: {exc}")
    finally:
        with _client_update_lock:
            _client_update_inflight_versions.discard(incoming_version)

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


def _capture_screen_jpeg_base64() -> str:
    from PIL import ImageGrab

    image = ImageGrab.grab(all_screens=True)
    image = image.convert("RGB")
    buffer = BytesIO()
    image.save(buffer, format="JPEG", quality=85)
    return base64.b64encode(buffer.getvalue()).decode("ascii")


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
    if cmd_type == "CAPTURE_SCREEN":
        image_base64 = _capture_screen_jpeg_base64()
        sio.emit(
            "device_screenshot",
            {
                "hostname": hostname,
                "command_id": command.get("command_id"),
                "captured_at": datetime.now(timezone.utc).isoformat(),
                "state": current_state.value,
                "content_name": playback.current_content_name() or "",
                "image_base64": image_base64,
            },
        )
        return "screen_captured"
    return "ignored"


def log_state_transition(from_state: ClientState, to_state: ClientState, reason: str):
    line = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "hostname": hostname,
        "from": from_state.value,
        "to": to_state.value,
        "reason": reason,
    }
    try:
        STATE_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with STATE_LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(line, ensure_ascii=False) + "\n")
    except OSError as exc:
        logging.debug(
            "Unable to append state transition log path=%s error=%s",
            STATE_LOG_PATH,
            exc,
            exc_info=True,
        )


def _refresh_call_overlay():
    if call_feature_enabled and current_state == ClientState.ACTIVE:
        call_request_overlay.update(
            True,
            bool(active_call),
            str((active_call or {}).get("requested_role") or ""),
        )
    else:
        call_request_overlay.hide()


def set_state(next_state: ClientState, reason: str):
    global current_state
    global playing_started_at

    if next_state == current_state:
        log_debug(f"set_state no-op | state={current_state.value} reason={reason}")
        return

    prev = current_state
    current_state = next_state
    if next_state == ClientState.PLAYING:
        playing_started_at = time.monotonic()
    log_state_transition(prev, next_state, reason)
    print(f"🔁 STATE {prev.value} -> {next_state.value} | {reason}")
    log_debug(f"state transition committed | from={prev.value} to={next_state.value} reason={reason}")
    _refresh_call_overlay()


def return_to_erp_window():
    if window_manager.bring_to_front(ERP_WINDOW_TITLE):
        print(f"🪟 ERP window brought to front: {ERP_WINDOW_TITLE}")
    else:
        print(f"⚠️ ERP window not found: {ERP_WINDOW_TITLE}")


@sio.event
def connect():
    global connection_outage_active
    if connection_outage_active:
        log_info("✅ Server bağlantısı geri geldi")
    connection_outage_active = False
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
            "updater_version": get_runtime_updater_version(),
            "content_name": playback.current_content_name(),
            **_get_update_status_payload(),
        },
    )
    sio.emit("pull_config", {"hostname": hostname})


@sio.event
def disconnect():
    global next_connect_attempt_at, connection_outage_active
    next_connect_attempt_at = 0.0
    if not connection_outage_active:
        log_warning("❌ Server bağlantısı koptu")
    connection_outage_active = True


@sio.event
def connect_error(data):
    global connection_outage_active
    if not connection_outage_active:
        log_warning("❌ Server bağlantısı koptu")
        log_debug(f"socket connect_error payload={data}")
    connection_outage_active = True


@sio.on("hello")
def on_hello(data):
    print("Server hello:", data)


@sio.on("config")
def on_config(data):
    global idle_timeout_sec, idle_mode_enabled, content_enabled
    global work_order_alert_active, work_order_alert_message
    global announcement_active, announcement_message, announcement_display_mode
    global call_feature_enabled, active_call

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
    if isinstance(data, dict):
        work_order_alert_active = bool(data.get("work_order_alert_active", False))
        work_order_alert_message = str(data.get("work_order_alert_message") or "İŞEMRİ BAŞLATILMAMIŞ")
        announcement_active = bool(data.get("announcement_active", False))
        announcement_message = str(data.get("announcement_message") or "").strip()
        announcement_display_mode = str(data.get("announcement_display_mode") or "normal").strip().lower()
        call_feature_enabled = bool(data.get("call_feature_enabled", False))
        active_call = data.get("active_call") if isinstance(data.get("active_call"), dict) else None
    else:
        work_order_alert_active = False
        work_order_alert_message = "İŞEMRİ BAŞLATILMAMIŞ"
        announcement_active = False
        announcement_message = ""
        announcement_display_mode = "normal"
        call_feature_enabled = False
        active_call = None

    effective_work_order_alert_active = work_order_alert_active
    effective_work_order_alert_message = work_order_alert_message
    effective_work_order_alert_flash = False
    if announcement_active:
        effective_work_order_alert_active = True
        effective_work_order_alert_flash = announcement_display_mode == "flash"
        if announcement_message:
            effective_work_order_alert_message = announcement_message

    if effective_work_order_alert_active:
        if work_order_alert_overlay.is_active():
            work_order_alert_overlay.update(effective_work_order_alert_message, flash=effective_work_order_alert_flash)
        else:
            work_order_alert_overlay.show(effective_work_order_alert_message, flash=effective_work_order_alert_flash)
    elif work_order_alert_overlay.is_active():
        work_order_alert_overlay.hide()

    if isinstance(config_timeout, (int, float)) and config_timeout > 0:
        idle_timeout_sec = int(config_timeout)
    else:
        idle_timeout_sec = DEFAULT_IDLE_TIMEOUT_SEC

    if isinstance(data, dict):
        playback.update_from_config(data)
        _maybe_run_client_updater_update(data)
        _maybe_run_auto_update(data)

    if call_feature_enabled and current_state == ClientState.ACTIVE:
        call_request_overlay.update(
            True,
            bool(active_call),
            str((active_call or {}).get("requested_role") or ""),
        )
    else:
        call_request_overlay.hide()

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


@sio.on("call_request_result")
def on_call_request_result(data):
    global active_call
    if not isinstance(data, dict):
        return
    if data.get("ok") and isinstance(data.get("call"), dict):
        active_call = data.get("call")
        print(f"📞 çağrı açıldı | role={active_call.get('requested_role')} id={active_call.get('id')}")
    else:
        print(f"⚠️ çağrı açma başarısız | reason={data.get('error')}")
    _refresh_call_overlay()


@sio.on("call_request_cancel_result")
def on_call_request_cancel_result(data):
    global active_call
    if isinstance(data, dict) and data.get("ok"):
        active_call = None
        print("📞 çağrı kapatıldı")
    else:
        print(f"⚠️ çağrı kapatma başarısız | reason={(data or {}).get('error')}")
    _refresh_call_overlay()


def run_state_cycle():
    global _last_observed_idle_sec, _activity_drop_streak, _low_idle_streak

    def _playback_has_selected_content() -> bool:
        if playback.current_content_name():
            return True

        active_item = getattr(playback, "_active_item", None)
        return isinstance(active_item, dict) and bool(active_item)

    def _playback_has_running_process() -> bool:
        process = getattr(playback, "_process", None)
        if process is not None:
            try:
                if process.poll() is None:
                    return True
            except Exception:
                pass

        widget_process = getattr(playback, "_widget_process", None)
        if widget_process is not None:
            try:
                if widget_process.poll() is None:
                    return True
            except Exception:
                pass

        extra_processes = getattr(playback, "_extra_processes", None)
        if isinstance(extra_processes, list):
            for extra_process in extra_processes:
                if extra_process is None:
                    continue
                try:
                    if extra_process.poll() is None:
                        return True
                except Exception:
                    continue

        return False

    if emergency_active:
        idle_background.hide()
        if current_state != ClientState.EMERGENCY:
            set_state(ClientState.EMERGENCY, "emergency_policy_enforced")
        playback.pause()
        return get_idle_seconds()

    if not content_enabled:
        idle_background.hide()
        if current_state in {ClientState.PLAYING, ClientState.IDLE_PENDING}:
            playback.stop(stop_widget_runtime=True)
            set_state(ClientState.ACTIVE, "content_disabled")
        return get_idle_seconds()

    idle_sec = get_idle_seconds()
    previous_idle_sec = _last_observed_idle_sec
    _last_observed_idle_sec = idle_sec
    activity_by_idle_drop = (
        isinstance(previous_idle_sec, (int, float))
        and (previous_idle_sec - idle_sec) >= ACTIVITY_IDLE_DROP_SEC
    )
    if activity_by_idle_drop:
        _activity_drop_streak += 1
    elif isinstance(previous_idle_sec, (int, float)) and idle_sec >= previous_idle_sec:
        _activity_drop_streak = 0

    if idle_sec <= ACTIVITY_RESUME_SEC:
        _low_idle_streak += 1
    else:
        _low_idle_streak = 0

    activity_drop_confirmed = _activity_drop_streak >= max(ACTIVITY_DROP_CONFIRM_COUNT, 1)
    low_idle_confirmed = _low_idle_streak >= 3
    user_activity_detected = activity_drop_confirmed or low_idle_confirmed
    log_debug(
        f"state_cycle sample | state={current_state.value} idle_sec={idle_sec:.3f} "
        f"prev_idle={previous_idle_sec} activity_drop={activity_by_idle_drop} "
        f"drop_streak={_activity_drop_streak} drop_confirmed={activity_drop_confirmed} "
        f"low_idle_streak={_low_idle_streak} low_idle_confirmed={low_idle_confirmed} "
        f"user_activity={user_activity_detected} idle_mode={idle_mode_enabled} content_enabled={content_enabled} emergency={emergency_active}"
    )

    if not idle_mode_enabled:
        idle_background.hide()
        if current_state in {ClientState.PLAYING, ClientState.IDLE_PENDING, ClientState.RETURNING}:
            playback.stop(stop_widget_runtime=True)
            return_to_erp_window()
            set_state(ClientState.ACTIVE, "idle_mode_disabled")
        return idle_sec

    effective_idle_timeout_sec = idle_timeout_sec
    if connection_outage_active:
        effective_idle_timeout_sec = max(1.0, min(float(idle_timeout_sec), OFFLINE_IDLE_TIMEOUT_CAP_SEC))
        log_debug(
            f"state_cycle outage mode | idle_timeout_sec={idle_timeout_sec} "
            f"effective_idle_timeout_sec={effective_idle_timeout_sec}"
        )

    if current_state == ClientState.ACTIVE and idle_sec >= effective_idle_timeout_sec:
        idle_background.show()
        set_state(ClientState.IDLE_PENDING, f"idle={idle_sec:.1f}s threshold={effective_idle_timeout_sec:.1f}s")

    if current_state == ClientState.IDLE_PENDING:
        log_debug("state_cycle IDLE_PENDING | ensuring playback.start")
        playback.start()
        if user_activity_detected:
            idle_background.hide()
            # Idle bekleme aşamasından kullanıcı etkileşimi ile çıkarken
            # widget runtime'ı kapatmayalım; bir sonraki idle geçişinde
            # viewer yeniden açılıp gri pencere flash'i oluşturmasın.
            playback.stop(stop_widget_runtime=False)
            set_state(ClientState.ACTIVE, f"activity_detected_before_playing idle={idle_sec:.1f}s")
            return idle_sec
        # Worker thread bir içerik seçmeden PLAYING durumuna geçersek
        # state machine PLAYING'de kilitli kalabilir ve tekrar start denemesi
        # yapılmadığı için ekran siyah kalabilir.
        if _playback_has_selected_content():
            log_debug("state_cycle IDLE_PENDING -> PLAYING candidate confirmed by selected content")
            # Idle overlay'i hemen kapatırsak, player içerik açmadan önce kısa bir
            # pencere oluşabiliyor ve Windows masaüstü görünür kalabiliyor.
            # Önce PLAYING durumuna geçip içerik gerçekten seçildiğinde kapatıyoruz.
            set_state(ClientState.PLAYING, "player_started")

    played_for_sec = time.monotonic() - playing_started_at
    active_item = playback._active_item if isinstance(getattr(playback, "_active_item", None), dict) else {}
    active_item_type = str(active_item.get("item_type") or active_item.get("media_type") or "").strip().lower()
    has_selected_content = _playback_has_selected_content()
    content_name = playback.current_content_name()
    if current_state == ClientState.PLAYING and has_selected_content:
        log_debug(
            "state_cycle PLAYING visible_content | "
            f"content={content_name or '<unnamed-content>'} "
            f"played_for_sec={played_for_sec:.3f} type={active_item_type}"
        )
        if active_item_type != "widget" or played_for_sec >= WIDGET_OVERLAY_HOLD_SEC:
            idle_background.hide()

    minimum_playing_before_return = WIDGET_ACTIVITY_GRACE_SEC if active_item_type == "widget" else MIN_PLAYING_SECONDS
    if active_item_type == "widget" and WIDGET_RETURN_REQUIRES_ERP_FOREGROUND and user_activity_detected:
        # Widget penceresi ön planda olduğunda dahi kullanıcı aktivitesi tespit edilirse
        # RETURNING'e geçip ERP'yi öne getirmeyi deniyoruz; aksi halde idle'dan çıkış
        # yalnızca widget hatası/bitişi ile mümkün olabiliyor.
        if not window_manager.is_window_foreground(ERP_WINDOW_TITLE):
            log_debug(
                "state_cycle PLAYING widget activity | "
                f"erp_not_foreground_detected title={ERP_WINDOW_TITLE!r} action=force_return"
            )
    if (
        current_state == ClientState.PLAYING
        and played_for_sec >= minimum_playing_before_return
        and user_activity_detected
    ):
        set_state(ClientState.RETURNING, f"activity_detected idle={idle_sec:.1f}s")

    if current_state == ClientState.RETURNING:
        log_debug("state_cycle RETURNING | returning focus to ERP and stopping playback")
        idle_background.hide()
        # ERP penceresini öne aldıktan sonra player'ı durdurmak,
        # mpv/widget kapanışında masaüstü parlamasını azaltır.
        return_to_erp_window()
        # Kullanıcı aktivitesi ile ACTIVE'e dönerken widget runtime'ı sıcak tut.
        # Böylece bir sonraki idle girişinde runtime yeniden açılıp siyah/masaüstü
        # flash'i oluşturmaz; stop() içinde gerekirse gizleme başarısızlığında
        # runtime otomatik kapatılır.
        playback.stop(stop_widget_runtime=False)
        set_state(ClientState.ACTIVE, "returned_to_erp")

    if current_state == ClientState.ACTIVE:
        if _playback_has_selected_content() or _playback_has_running_process():
            log_debug("state_cycle ACTIVE | stopping playback with warm widget runtime")
            # Startup pre-warm ile açılan widget runtime'ı ACTIVE döngüsünde kapatmayalım;
            # böylece ilk idle girişinde viewer yeniden sıfırdan başlatılmaz.
            playback.stop(stop_widget_runtime=False)
            playback._active_widget_signature = None

    return idle_sec


def main():
    global update_shutdown_requested
    global next_connect_attempt_at, connection_outage_active
    if already_running():
        return

    try:
        if RUNTIME_TMP_CLEANUP_ENABLED:
            cleanup_runtime_tmp_dir()
        hide_console_window()
        start_console_hider()
        gui_runtime.start()
        systray.start()
        print("Connecting to:", SERVER_URL)

        def force_disconnect(reason: str):
            if sio.connected:
                log_warning(f"🔌 socket reset | reason={reason}")
            try:
                sio.disconnect()
            except Exception:
                pass

        def ensure_socket_connected(now: float) -> bool:
            global next_connect_attempt_at, connection_outage_active
            if sio.connected:
                return True
            if now < next_connect_attempt_at:
                return False

            with connection_lock:
                if sio.connected:
                    return True
                if now < next_connect_attempt_at:
                    return False
                next_connect_attempt_at = now + RECONNECT_RETRY_SEC
                try:
                    log_debug(
                        f"connecting socket | url={SERVER_URL} transports={SOCKETIO_TRANSPORTS or 'default'}"
                    )
                    connect_kwargs = {"wait": True, "wait_timeout": 10}
                    if SOCKETIO_TRANSPORTS:
                        connect_kwargs["transports"] = SOCKETIO_TRANSPORTS
                    sio.connect(SERVER_URL, **connect_kwargs)
                    next_connect_attempt_at = now + HEARTBEAT_INTERVAL_SEC
                    return True
                except KeyboardInterrupt:
                    raise
                except Exception as connect_err:
                    if not connection_outage_active:
                        log_warning("❌ Server bağlantısı koptu")
                        log_debug(f"socket connect failed: {connect_err}")
                    connection_outage_active = True
                    return False

        next_heartbeat_at = time.monotonic()
        next_config_pull_at = time.monotonic()

        while not shutdown_event.is_set():
            try:
                idle_sec = run_state_cycle()
                now = time.monotonic()
                ensure_socket_connected(now)
                if now >= next_heartbeat_at:
                    if not sio.connected:
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
                                "updater_version": get_runtime_updater_version(),
                                "content_name": playback.current_content_name(),
                                **_get_update_status_payload(),
                            },
                        )
                        print(f"💓 heartbeat sent | state={current_state.value} idle={idle_sec:.1f}s")
                    except Exception as heartbeat_err:
                        log_error(f"⚠️ Heartbeat send failed: {heartbeat_err}")
                        logging.exception("Heartbeat emit failed")
                        force_disconnect("heartbeat_emit_failed")
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
                            log_error(f"⚠️ Periodic config pull failed: {pull_err}")
                            logging.exception("Periodic config pull failed")
                            force_disconnect("periodic_pull_failed")
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
        playback.stop(stop_widget_runtime=True)
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


def _parse_cli_args(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument("--widget", dest="widget_url", help="Widget URL to launch in viewer mode")
    parser.add_argument(
        "--runtime-ipc",
        action="store_true",
        help="Widget viewer runtime IPC mode (used internally by agent runtime controller)",
    )
    parser.add_argument(
        "--start-hidden",
        action="store_true",
        help="Start widget viewer hidden (used internally by agent runtime controller)",
    )
    parser.add_argument(
        "--parent-pid",
        type=int,
        default=0,
        help="Parent process PID used to stop orphaned widget runtime processes.",
    )
    parser.add_argument(
        "--monitor-bounds",
        default="",
        help="Target monitor bounds as x,y,width,height for widget viewer placement.",
    )
    parser.add_argument(
        "--monitor",
        type=int,
        default=None,
        help="Target monitor index for widget viewer placement (0-based). Ignored when --monitor-bounds is set.",
    )
    return parser.parse_known_args(argv)


def _run_widget_entrypoint(
    widget_url: str,
    runtime_ipc: bool = False,
    start_hidden: bool = False,
    parent_pid: int | None = None,
    monitor_bounds: tuple[int, int, int, int] | None = None,
    monitor: int | None = None,
) -> int:
    from widget_viewer import (
        WIDGET_ENGINE_SENTINEL,
        _build_engine_url,
        _normalize_url,
        _resolve_runtime_resource,
        _parse_monitor_bounds,
        _safe_print,
        _start_with_cef,
        _start_with_pywebview,
        _viewer_backend_order,
        _windows_connected_monitor_bounds,
    )

    _start_parent_watchdog(parent_pid)
    parsed_monitor_bounds = monitor_bounds
    if parsed_monitor_bounds is None:
        parsed_monitor_bounds = _parse_monitor_bounds(os.getenv("WIDGET_MONITOR_BOUNDS", ""))
    if parsed_monitor_bounds is None and monitor is not None:
        monitor_list = _windows_connected_monitor_bounds()
        if not monitor_list:
            _safe_print("Monitor seçimi çözümlenemedi: bağlı monitör listesi alınamadı.")
            return 2
        if monitor < 0 or monitor >= len(monitor_list):
            _safe_print(f"Geçersiz --monitor değeri: {monitor}. Geçerli aralık: 0-{len(monitor_list) - 1}")
            return 2
        parsed_monitor_bounds = monitor_list[monitor]

    normalized_input = str(widget_url or "").strip()
    if normalized_input == WIDGET_ENGINE_SENTINEL:
        normalized_input = _resolve_runtime_resource("widget_engine.html").resolve().as_uri()

    try:
        normalized_url = _build_engine_url(_normalize_url(normalized_input))
    except Exception as exc:
        _safe_print(f"Geçersiz widget URL: {exc}")
        return 2

    errors: list[str] = []
    for backend in _viewer_backend_order():
        try:
            _safe_print(f"Widget viewer backend deneniyor: {backend}")
            if backend == "cef":
                _start_with_cef(
                    normalized_url,
                    runtime_ipc=runtime_ipc,
                    start_hidden=start_hidden,
                    monitor_bounds=parsed_monitor_bounds,
                )
            else:
                _start_with_pywebview(
                    normalized_url,
                    runtime_ipc=runtime_ipc,
                    start_hidden=start_hidden,
                    monitor_bounds=parsed_monitor_bounds,
                )
            return 0
        except Exception as exc:
            errors.append(f"{backend}: {exc}")

    _safe_print(f"Widget viewer başlatılamadı: {'; '.join(errors)}")
    return 1


if __name__ == "__main__":
    args, _unknown_args = _parse_cli_args()
    if args.widget_url:
        from widget_viewer import _parse_monitor_bounds

        raise SystemExit(
            _run_widget_entrypoint(
                args.widget_url,
                runtime_ipc=args.runtime_ipc,
                start_hidden=args.start_hidden,
                parent_pid=args.parent_pid,
                monitor_bounds=_parse_monitor_bounds(args.monitor_bounds),
                monitor=args.monitor,
            )
        )
    main()
