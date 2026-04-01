import base64
import hashlib
import ipaddress
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import ctypes
import time
import threading
import re
import logging
import traceback
from ctypes import wintypes
from pathlib import Path
from urllib.parse import quote
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit


WIDGET_ENGINE_SENTINEL = "__BAYLAN_WIDGET_ENGINE__"


def _safe_print(message: str) -> None:
    try:
        print(message)
    except UnicodeEncodeError:
        try:
            stream = getattr(sys, "stdout", None)
            encoding = getattr(stream, "encoding", None) or "utf-8"
            sanitized_message = message.encode(encoding, errors="replace").decode(encoding, errors="replace")
            print(sanitized_message)
        except OSError:
            pass
    except OSError:
        pass


LOGGER = logging.getLogger("baylan.client.player")
DEBUG_MODE_ENABLED = os.getenv("CLIENT_DEBUG_MODE", "0").strip().lower() in {"1", "true", "yes", "on", "debug"}


def _debug_log(message: str) -> None:
    if not DEBUG_MODE_ENABLED:
        return
    LOGGER.debug(message)
    _safe_print(f"[DEBUG][player] {message}")


def _runtime_resource_path(*relative_parts: str) -> Path:
    base_dir = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base_dir.joinpath(*relative_parts)


def _resolve_runtime_resource(*relative_parts: str) -> Path:
    candidates: list[Path] = []

    primary = _runtime_resource_path(*relative_parts)
    candidates.append(primary)

    prefixed_primary = _runtime_resource_path("client", *relative_parts)
    if prefixed_primary not in candidates:
        candidates.append(prefixed_primary)

    executable_dir = Path(sys.executable).resolve().parent
    candidates.append(executable_dir.joinpath(*relative_parts))
    prefixed_executable = executable_dir.joinpath("client", *relative_parts)
    if prefixed_executable not in candidates:
        candidates.append(prefixed_executable)

    module_dir = Path(__file__).resolve().parent
    candidates.append(module_dir.joinpath(*relative_parts))

    for candidate in candidates:
        if candidate.is_file():
            _debug_log(f"_resolve_runtime_resource hit | relative={relative_parts} candidate={candidate}")
            return candidate

    _debug_log(f"_resolve_runtime_resource miss | relative={relative_parts} fallback={primary}")
    return primary


class BorderlessFullscreenPlayer:
    VIDEO_EXTENSIONS = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v"}
    IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp", ".svg"}
    VLC_VIDEO_TEMPLATE = (
        "{player} --intf dummy --dummy-quiet --fullscreen --play-and-exit "
        "--no-video-title-show --no-osd --no-mouse-events --no-keyboard-events "
        "--video-on-top --no-interact --quiet {media}"
    )
    VLC_IMAGE_TEMPLATE = (
        "{player} --intf dummy --dummy-quiet --fullscreen "
        "--no-video-title-show --no-osd --no-mouse-events --no-keyboard-events "
        "--no-interact --quiet --loop --image-duration={duration} "
        "--avcodec-hw=none {media}"
    )
    MPV_COMMON_FLAGS = (
        "--fs --border=no --force-window=immediate --ontop --quiet "
        "--background-color=0/0/0 --osc=no --osd-level=0 --input-cursor=no"
    )
    MPV_VIDEO_TEMPLATE = "{player} " + MPV_COMMON_FLAGS + " {media}"
    MPV_IMAGE_TEMPLATE = (
        "{player} " + MPV_COMMON_FLAGS + " "
        "--image-display-duration={duration} {media}"
    )
    _WINDOWS_BROWSER_KIOSK_FLAGS = [
        "--kiosk",
        "--edge-kiosk-type=fullscreen",
        "--app={widget}",
        "--start-fullscreen",
        "--start-maximized",
        "--window-position=0,0",
        "--disable-features=Translate,TranslateUI,msUndersideButton",
        "--disable-translate",
        "--translate=0",
        "--disable-infobars",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    _WINDOWS_BROWSER_KIOSK_FLAGS_CHROME = [
        "--kiosk",
        "--app={widget}",
        "--start-fullscreen",
        "--start-maximized",
        "--window-position=0,0",
        "--disable-features=Translate,TranslateUI",
        "--disable-translate",
        "--translate=0",
        "--disable-infobars",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    _WINDOWS_DRIVE_PATH_PATTERN = re.compile(r"^[a-zA-Z]:[\\/]")

    @staticmethod
    def _windows_active_monitor_bounds() -> tuple[int, int, int, int] | None:
        if os.name != "nt" or not hasattr(ctypes, "windll"):
            return None

        user32 = ctypes.windll.user32
        monitor_from_window = getattr(user32, "MonitorFromWindow", None)
        get_monitor_info = getattr(user32, "GetMonitorInfoW", None)
        get_foreground_window = getattr(user32, "GetForegroundWindow", None)
        if not monitor_from_window or not get_monitor_info or not get_foreground_window:
            return None

        hwnd = get_foreground_window()
        if not hwnd:
            return None

        MONITOR_DEFAULTTONEAREST = 2
        monitor_handle = monitor_from_window(hwnd, MONITOR_DEFAULTTONEAREST)
        if not monitor_handle:
            return None

        class RECT(ctypes.Structure):
            _fields_ = [
                ("left", ctypes.c_long),
                ("top", ctypes.c_long),
                ("right", ctypes.c_long),
                ("bottom", ctypes.c_long),
            ]

        class MONITORINFO(ctypes.Structure):
            _fields_ = [
                ("cbSize", ctypes.c_ulong),
                ("rcMonitor", RECT),
                ("rcWork", RECT),
                ("dwFlags", ctypes.c_ulong),
            ]

        monitor_info = MONITORINFO()
        monitor_info.cbSize = ctypes.sizeof(MONITORINFO)
        if not get_monitor_info(monitor_handle, ctypes.byref(monitor_info)):
            return None

        left = int(monitor_info.rcMonitor.left)
        top = int(monitor_info.rcMonitor.top)
        right = int(monitor_info.rcMonitor.right)
        bottom = int(monitor_info.rcMonitor.bottom)
        width = max(1, right - left)
        height = max(1, bottom - top)
        return left, top, width, height

    @classmethod
    def _windows_active_monitor_origin(cls) -> tuple[int, int] | None:
        monitor_bounds = cls._windows_active_monitor_bounds()
        if monitor_bounds is None:
            return None
        x, y, _, _ = monitor_bounds
        return x, y

    @classmethod
    def _apply_windows_monitor_position(
        cls,
        flags: list[str],
        monitor_bounds: tuple[int, int, int, int] | None = None,
    ) -> list[str]:
        if os.name != "nt":
            return list(flags)

        monitor_bounds = monitor_bounds or cls._windows_active_monitor_bounds()
        if monitor_bounds is None:
            return list(flags)

        x, y, width, height = monitor_bounds
        positioned_flags: list[str] = []
        position_replaced = False
        size_replaced = False
        for flag in flags:
            if isinstance(flag, str) and flag.startswith("--window-position="):
                positioned_flags.append(f"--window-position={x},{y}")
                position_replaced = True
            elif isinstance(flag, str) and flag.startswith("--window-size="):
                positioned_flags.append(f"--window-size={width},{height}")
                size_replaced = True
            elif flag in {"--start-fullscreen", "--start-maximized"}:
                continue
            else:
                positioned_flags.append(flag)

        has_window_position_flag = any(
            isinstance(flag, str) and flag.startswith("--window-position=")
            for flag in flags
        )
        has_window_size_flag = any(
            isinstance(flag, str) and flag.startswith("--window-size=")
            for flag in flags
        )

        if has_window_size_flag and not position_replaced:
            positioned_flags.append(f"--window-position={x},{y}")
        if has_window_position_flag and not size_replaced:
            positioned_flags.append(f"--window-size={width},{height}")

        return positioned_flags

    def __init__(self, keep_widget_runtime_warm: bool | None = None):
        self.image_duration_sec = int(os.getenv("IMAGE_DURATION_SEC", "8"))
        self.static_image_duration_sec = int(os.getenv("STATIC_IMAGE_DURATION_SEC", "86400"))
        default_video_command, default_image_command = self._pick_default_player_commands()

        self.video_command = os.getenv(
            "PLAYER_VIDEO_COMMAND",
            default_video_command,
        )
        self.image_command = os.getenv(
            "PLAYER_IMAGE_COMMAND",
            default_image_command,
        )
        self._process = None
        self._extra_processes: list[subprocess.Popen] = []
        self._process_lock = threading.RLock()
        self._widget_process_lock = threading.RLock()
        self._stop_requested = False
        self._widget_process = None
        self._widget_runtime_processes: list[subprocess.Popen] = []
        self._widget_process_stdin_lock = threading.Lock()
        self._widget_runtime_restart_count = 0
        self._python_widget_viewer_supported = self._detect_python_widget_viewer_support()
        self._python_widget_viewer_runtime_enabled = True
        env_keep_widget_runtime_warm = (
            os.getenv("WIDGET_KEEP_RUNTIME_WARM", "1").strip().lower() in {"1", "true", "yes"}
        )
        if keep_widget_runtime_warm is None:
            self._keep_widget_runtime_warm = env_keep_widget_runtime_warm
        else:
            self._keep_widget_runtime_warm = bool(keep_widget_runtime_warm)
        self._last_interrupted = False
        self._last_widget_source = ""
        self._last_runtime_signature: tuple[tuple[tuple[int | None, tuple[int, int, int, int] | None], ...], bool, int | None] | None = None
        self._last_widget_signature: str | None = None
        self._active_widget_signature: str | None = None
        _debug_log("player initialized | keep_widget_runtime_warm=%s python_viewer_supported=%s" % (self._keep_widget_runtime_warm, self._python_widget_viewer_supported))

    def _apply_pending_stop_to_media_processes(self, processes: list[subprocess.Popen]) -> bool:
        """
        Close freshly started media processes if a stop request arrived concurrently.

        Playback runs in a worker thread while state transitions call ``stop`` from
        another thread. If the stop signal lands between process spawn and process
        reference assignment, the new mpv process can miss the termination request.
        """
        if not self._stop_requested:
            return False

        for process in processes:
            if process and process.poll() is None:
                self._terminate_process(process, timeout_sec=5)
        return True

    @staticmethod
    def _windows_hidden_process_kwargs() -> dict:
        if os.name != "nt":
            return {}

        kwargs: dict = {}
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        if creation_flags:
            kwargs["creationflags"] = creation_flags

        startupinfo_cls = getattr(subprocess, "STARTUPINFO", None)
        startupinfo = startupinfo_cls() if startupinfo_cls else None
        startf_use_show_window = getattr(subprocess, "STARTF_USESHOWWINDOW", 0)
        if startupinfo and startf_use_show_window:
            startupinfo.dwFlags |= startf_use_show_window
            startupinfo.wShowWindow = 0
            kwargs["startupinfo"] = startupinfo

        return kwargs

    @classmethod
    def _run_taskkill_tree(cls, pid: int) -> None:
        kwargs = {
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "check": False,
        }
        kwargs.update(cls._windows_hidden_process_kwargs())
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], **kwargs)

    @staticmethod
    def _python_widget_viewer_enabled() -> bool:
        return os.getenv("PYTHON_WIDGET_VIEWER_ENABLED", "1").strip().lower() not in {"0", "false", "no"}

    @staticmethod
    def _should_clone_to_all_monitors() -> bool:
        return os.getenv("MIRROR_PLAYLIST_TO_ALL_MONITORS", "1").strip().lower() in {"1", "true", "yes", "on"}

    @staticmethod
    def _windows_connected_monitor_bounds() -> list[tuple[int, int, int, int]]:
        if os.name != "nt" or not hasattr(ctypes, "windll"):
            return []

        enum_display_monitors = getattr(ctypes.windll.user32, "EnumDisplayMonitors", None)
        get_monitor_info = getattr(ctypes.windll.user32, "GetMonitorInfoW", None)
        if not enum_display_monitors or not get_monitor_info:
            return []

        class RECT(ctypes.Structure):
            _fields_ = [
                ("left", ctypes.c_long),
                ("top", ctypes.c_long),
                ("right", ctypes.c_long),
                ("bottom", ctypes.c_long),
            ]

        class MONITORINFO(ctypes.Structure):
            _fields_ = [
                ("cbSize", ctypes.c_ulong),
                ("rcMonitor", RECT),
                ("rcWork", RECT),
                ("dwFlags", ctypes.c_ulong),
            ]

        MONITORINFOF_PRIMARY = 1
        monitor_rects: list[tuple[int, int, int, int, bool]] = []
        callback_type = ctypes.WINFUNCTYPE(
            ctypes.c_int,
            wintypes.HMONITOR,
            wintypes.HDC,
            ctypes.POINTER(RECT),
            wintypes.LPARAM,
        )

        def _collect_monitor(monitor_handle, _hdc, rect_ptr, _data):
            if not rect_ptr:
                return 1
            rect = rect_ptr.contents
            width = max(1, int(rect.right - rect.left))
            height = max(1, int(rect.bottom - rect.top))
            is_primary = False
            try:
                monitor_info = MONITORINFO()
                monitor_info.cbSize = ctypes.sizeof(MONITORINFO)
                if get_monitor_info(monitor_handle, ctypes.byref(monitor_info)):
                    is_primary = bool(int(monitor_info.dwFlags) & MONITORINFOF_PRIMARY)
            except Exception:
                is_primary = False

            monitor_rects.append((int(rect.left), int(rect.top), width, height, is_primary))
            return 1

        try:
            enum_display_monitors(0, 0, callback_type(_collect_monitor), 0)
        except Exception:
            return []

        # Windows ekran yerleşimi ile aynı görsel sırayı korumak için
        # monitörleri önce sol (x), sonra üst (y) koordinata göre sırala.
        # Primary monitörü başa zorlamak, farklı dikey hizalamalarda
        # Windows'taki ID yerleşimi ile uyumsuz index üretimine yol açabiliyor.
        monitor_rects.sort(key=lambda item: (item[0], item[1], 0 if item[4] else 1))

        unique_monitor_rects: list[tuple[int, int, int, int]] = []
        seen_rects: set[tuple[int, int, int, int]] = set()
        for left, top, width, height, _is_primary in monitor_rects:
            rect = (left, top, width, height)
            if rect in seen_rects:
                continue
            seen_rects.add(rect)
            unique_monitor_rects.append(rect)

        return unique_monitor_rects

    @staticmethod
    def _windows_monitor_id_to_index_map() -> dict[int, int]:
        # Windows Display Settings kimlikleri (DISPLAY1, DISPLAY2, ...)
        # MONITORINFOEXW::szDevice alanından okunur. Ancak Windows "Ekranı
        # tanımla" numaraları bazı kurulumlarda DISPLAYx ile birebir
        # olmayabildiği için önce QueryDisplayConfig source id eşlemesini
        # deneriz. Bu kimlikleri
        # uygulama tarafındaki monitor_no ile eşleyerek seçim yapıyoruz.
        # Eşleme sırasında tek bir enumerasyon sonucunu kullanarak index
        # üretelim; böylece farklı API çağrıları arasında sıralama kayması
        # olduğunda (ör. DISPLAY2/DISPLAY3 swap) yanlış map oluşmaz.
        if os.name != "nt" or not hasattr(ctypes, "windll"):
            return {}

        gdi_name_to_display_id = BorderlessFullscreenPlayer._windows_gdi_device_name_to_display_id_map()
        enum_display_monitors = getattr(ctypes.windll.user32, "EnumDisplayMonitors", None)
        get_monitor_info = getattr(ctypes.windll.user32, "GetMonitorInfoW", None)
        if not enum_display_monitors or not get_monitor_info:
            return {}

        class RECT(ctypes.Structure):
            _fields_ = [
                ("left", ctypes.c_long),
                ("top", ctypes.c_long),
                ("right", ctypes.c_long),
                ("bottom", ctypes.c_long),
            ]

        class MONITORINFOEXW(ctypes.Structure):
            _fields_ = [
                ("cbSize", ctypes.c_ulong),
                ("rcMonitor", RECT),
                ("rcWork", RECT),
                ("dwFlags", ctypes.c_ulong),
                ("szDevice", ctypes.c_wchar * 32),
            ]

        callback_type = ctypes.WINFUNCTYPE(
            ctypes.c_int,
            wintypes.HMONITOR,
            wintypes.HDC,
            ctypes.POINTER(RECT),
            wintypes.LPARAM,
        )

        MONITORINFOF_PRIMARY = 1
        monitor_entries: list[tuple[int, int, int, int, int, bool]] = []

        def _collect_monitor(monitor_handle, _hdc, rect_ptr, _data):
            if not rect_ptr:
                return 1
            rect = rect_ptr.contents
            width = max(1, int(rect.right - rect.left))
            height = max(1, int(rect.bottom - rect.top))
            monitor_info = MONITORINFOEXW()
            monitor_info.cbSize = ctypes.sizeof(MONITORINFOEXW)
            try:
                if get_monitor_info(monitor_handle, ctypes.byref(monitor_info)):
                    device_name = str(monitor_info.szDevice or "")
                    normalized_device_name = device_name.strip().upper()
                    display_id = gdi_name_to_display_id.get(normalized_device_name)
                    windows_id: int | None = None
                    if display_id is not None:
                        windows_id = int(display_id)
                    else:
                        match = re.search(r"DISPLAY(\d+)", device_name, re.IGNORECASE)
                        if match:
                            windows_id = int(match.group(1))
                    if windows_id is not None:
                        is_primary = bool(int(monitor_info.dwFlags) & MONITORINFOF_PRIMARY)
                        monitor_entries.append((
                            int(rect.left),
                            int(rect.top),
                            width,
                            height,
                            windows_id,
                            is_primary,
                        ))
            except Exception:
                return 1
            return 1

        try:
            enum_display_monitors(0, 0, callback_type(_collect_monitor), 0)
        except Exception:
            return {}

        if not monitor_entries:
            return {}

        # Eşleme, _windows_connected_monitor_bounds ile aynı geometri bazlı
        # sırayı kullanmalı; aksi halde DISPLAY2/DISPLAY3 gibi ID'ler
        # dikey hizası farklı monitörlerde yer değiştirmiş görünebiliyor.
        monitor_entries.sort(key=lambda item: (item[0], item[1], 0 if item[5] else 1))

        id_to_index: dict[int, int] = {}
        seen_rects: set[tuple[int, int, int, int]] = set()
        for left, top, width, height, windows_id, _is_primary in monitor_entries:
            rect = (left, top, width, height)
            if rect in seen_rects:
                continue
            seen_rects.add(rect)
            id_to_index[windows_id] = len(seen_rects) - 1
        return id_to_index

    @staticmethod
    def _windows_gdi_device_name_to_display_id_map() -> dict[str, int]:
        """Map \\\\.\\DISPLAYx => Windows Display Settings "Identify" ID."""
        if os.name != "nt" or not hasattr(ctypes, "windll"):
            return {}

        user32 = ctypes.windll.user32
        get_buffer_sizes = getattr(user32, "GetDisplayConfigBufferSizes", None)
        query_display_config = getattr(user32, "QueryDisplayConfig", None)
        display_config_get_device_info = getattr(user32, "DisplayConfigGetDeviceInfo", None)
        if not get_buffer_sizes or not query_display_config or not display_config_get_device_info:
            return {}

        QDC_ONLY_ACTIVE_PATHS = 0x00000002
        DISPLAYCONFIG_DEVICE_INFO_GET_SOURCE_NAME = 1
        CCHDEVICENAME = 32

        class LUID(ctypes.Structure):
            _fields_ = [
                ("LowPart", wintypes.DWORD),
                ("HighPart", wintypes.LONG),
            ]

        class DISPLAYCONFIG_PATH_SOURCE_INFO(ctypes.Structure):
            _fields_ = [
                ("adapterId", LUID),
                ("id", wintypes.UINT),
                ("modeInfoIdx", wintypes.UINT),
                ("statusFlags", wintypes.UINT),
            ]

        class DISPLAYCONFIG_PATH_TARGET_INFO(ctypes.Structure):
            _fields_ = [
                ("adapterId", LUID),
                ("id", wintypes.UINT),
                ("modeInfoIdx", wintypes.UINT),
                ("outputTechnology", wintypes.UINT),
                ("rotation", wintypes.UINT),
                ("scaling", wintypes.UINT),
                ("refreshRateNumerator", wintypes.UINT),
                ("refreshRateDenominator", wintypes.UINT),
                ("scanLineOrdering", wintypes.UINT),
                ("targetAvailable", wintypes.BOOL),
                ("statusFlags", wintypes.UINT),
            ]

        class DISPLAYCONFIG_PATH_INFO(ctypes.Structure):
            _fields_ = [
                ("sourceInfo", DISPLAYCONFIG_PATH_SOURCE_INFO),
                ("targetInfo", DISPLAYCONFIG_PATH_TARGET_INFO),
                ("flags", wintypes.UINT),
            ]

        class DISPLAYCONFIG_DEVICE_INFO_HEADER(ctypes.Structure):
            _fields_ = [
                ("type", wintypes.UINT),
                ("size", wintypes.UINT),
                ("adapterId", LUID),
                ("id", wintypes.UINT),
            ]

        class DISPLAYCONFIG_SOURCE_DEVICE_NAME(ctypes.Structure):
            _fields_ = [
                ("header", DISPLAYCONFIG_DEVICE_INFO_HEADER),
                ("viewGdiDeviceName", ctypes.c_wchar * CCHDEVICENAME),
            ]

        path_count = wintypes.UINT(0)
        mode_count = wintypes.UINT(0)
        if int(get_buffer_sizes(QDC_ONLY_ACTIVE_PATHS, ctypes.byref(path_count), ctypes.byref(mode_count))) != 0:
            return {}
        if int(path_count.value) <= 0:
            return {}

        paths = (DISPLAYCONFIG_PATH_INFO * int(path_count.value))()
        # Mode array zorunlu parametre; boyutunu API'nin verdiği kadar ayırıyoruz.
        mode_info_raw = (ctypes.c_ubyte * max(1, int(mode_count.value) * 64))()
        if int(query_display_config(
            QDC_ONLY_ACTIVE_PATHS,
            ctypes.byref(path_count),
            paths,
            ctypes.byref(mode_count),
            mode_info_raw,
            None,
        )) != 0:
            return {}

        gdi_to_source_id: dict[str, int] = {}
        gdi_to_target_id: dict[str, int] = {}
        seen_sources: set[tuple[int, int, int]] = set()
        for idx in range(int(path_count.value)):
            source_info = paths[idx].sourceInfo
            target_info = paths[idx].targetInfo
            source_key = (
                int(source_info.adapterId.HighPart),
                int(source_info.adapterId.LowPart),
                int(source_info.id),
            )
            if source_key in seen_sources:
                continue
            seen_sources.add(source_key)

            source_name = DISPLAYCONFIG_SOURCE_DEVICE_NAME()
            source_name.header.type = DISPLAYCONFIG_DEVICE_INFO_GET_SOURCE_NAME
            source_name.header.size = ctypes.sizeof(DISPLAYCONFIG_SOURCE_DEVICE_NAME)
            source_name.header.adapterId = source_info.adapterId
            source_name.header.id = source_info.id
            if int(display_config_get_device_info(ctypes.byref(source_name.header))) != 0:
                continue

            gdi_name = str(source_name.viewGdiDeviceName or "").strip().upper()
            if not gdi_name:
                continue
            # Bazı sistemlerde Windows "Ekranı tanımla" numarası source id + 1,
            # bazılarında target id + 1 ile daha uyumlu olabiliyor.
            gdi_to_target_id[gdi_name] = int(target_info.id) + 1
            gdi_to_source_id[gdi_name] = int(source_info.id) + 1

        sanitized_target_map = BorderlessFullscreenPlayer._sanitize_windows_display_id_map(gdi_to_target_id)
        if sanitized_target_map:
            return sanitized_target_map
        return BorderlessFullscreenPlayer._sanitize_windows_display_id_map(gdi_to_source_id)

    @staticmethod
    def _sanitize_windows_display_id_map(raw_map: dict[str, int]) -> dict[str, int]:
        """Keep only plausible Windows display IDs.

        QueryDisplayConfig source/target IDs can be large adapter-scoped integers
        on some systems and are not always the same as "Identify" numbers.
        Accept only a compact positive integer range (1..N) to avoid exposing
        bogus IDs such as 8388689.
        """
        if not isinstance(raw_map, dict) or not raw_map:
            return {}

        normalized: dict[str, int] = {}
        for gdi_name, display_id in raw_map.items():
            key = str(gdi_name or "").strip().upper()
            if not key:
                continue
            try:
                parsed_id = int(display_id)
            except (TypeError, ValueError):
                continue
            if parsed_id < 1:
                continue
            normalized[key] = parsed_id

        if not normalized:
            return {}

        unique_ids = sorted(set(normalized.values()))
        max_id = unique_ids[-1]
        # Accept only dense/compact IDs in a reasonable range.
        # Valid examples: [1], [1,2], [1,2,3,4].
        if max_id > 64 or unique_ids != list(range(1, max_id + 1)):
            return {}
        return normalized

    @staticmethod
    def _prefer_python_widget_viewer() -> bool:
        return os.getenv("WIDGET_USE_PYTHON_VIEWER", "1").strip().lower() in {"1", "true", "yes"}

    def _detect_python_widget_viewer_support(self) -> bool:
        if not self._python_widget_viewer_enabled():
            return False

        if os.name != "nt":
            _safe_print("⚠️ Python widget viewer pasif: sadece Windows'ta destekleniyor")
            return False

        backend = os.getenv("WIDGET_VIEWER_BACKEND", "auto").strip().lower()
        try:
            if backend in {"auto", "pywebview"}:
                import webview

                if webview:
                    return True
        except Exception:
            pass

        _safe_print("⚠️ Python widget viewer pasif: pywebview kullanılamıyor")
        return False

    def _should_use_python_widget_viewer(self, widget_source: str) -> bool:
        if not self._is_widget_url(widget_source):
            return False
        if not self._prefer_python_widget_viewer():
            return False
        if not self._python_widget_viewer_supported:
            return False
        if not self._python_widget_viewer_runtime_enabled:
            return False
        return True

    def _build_python_widget_command(self, widget_source: str) -> list[str] | None:
        viewer_path = _resolve_runtime_resource("widget_viewer.py")
        parent_pid = str(os.getpid())
        _debug_log(
            "_build_python_widget_command | "
            f"frozen={getattr(sys, 'frozen', False)} viewer_path={viewer_path} "
            f"viewer_exists={viewer_path.is_file()} widget_source={widget_source[:120]}"
        )
        if getattr(sys, "frozen", False):
            return [sys.executable, "--widget", widget_source, "--parent-pid", parent_pid]
        if not viewer_path.is_file():
            _safe_print(f"⚠️ widget viewer script bulunamadı: {viewer_path}")
            return None
        return [sys.executable, str(viewer_path), widget_source, "--parent-pid", parent_pid]

    @staticmethod
    def _widget_runtime_controller_enabled() -> bool:
        return os.getenv("WIDGET_RUNTIME_CONTROLLER_ENABLED", "1").strip().lower() in {"1", "true", "yes"}

    @staticmethod
    def _widget_legacy_process_fallback_enabled() -> bool:
        return os.getenv("WIDGET_LEGACY_PROCESS_FALLBACK", "1").strip().lower() in {"1", "true", "yes"}

    def _is_python_widget_command(self, command: list[str]) -> bool:
        if not command:
            return False
        script_path = str(_runtime_resource_path("widget_viewer.py"))
        normalized_executable = str(Path(sys.executable))
        normalized_command_executable = str(Path(command[0]))
        if (
            len(command) >= 3
            and normalized_command_executable == normalized_executable
            and command[1] == "--widget"
        ):
            return True
        if len(command) >= 2 and command[1] == script_path:
            return True
        return Path(command[0]).name.lower() in {"widget_viewer.exe", "widget_viewer"}

    def _widget_popen_kwargs(self, command: list[str]) -> dict:
        kwargs: dict = {}
        if os.name == "nt" and self._is_python_widget_command(command):
            creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            if creation_flags:
                kwargs["creationflags"] = creation_flags
            env = os.environ.copy()
            env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
            if not str(env.get("WIDGET_VIEWER_LOG_PATH", "") or "").strip():
                executable_dir = os.path.dirname(os.path.abspath(sys.executable))
                log_file_name = "widget_viewer.log"
                if "--monitor" in command:
                    try:
                        monitor_arg = command[command.index("--monitor") + 1]
                        monitor_index = int(str(monitor_arg).strip())
                    except (ValueError, IndexError):
                        monitor_index = -1
                    if monitor_index >= 0:
                        log_file_name = f"widget_viewer_m{monitor_index}.log"
                env["WIDGET_VIEWER_LOG_PATH"] = os.path.join(
                    executable_dir,
                    "client",
                    "logs",
                    log_file_name,
                )
            kwargs["env"] = env
        return kwargs


    @staticmethod
    def _is_vlc_command(command: list[str]) -> bool:
        if not command:
            return False
        executable_name = Path(BorderlessFullscreenPlayer._strip_outer_quotes(command[0])).name.lower()
        return "vlc" in executable_name

    def _build_mpv_image_command(self, media_path: str, image_duration_sec: int | None = None) -> list[str]:
        duration = self.image_duration_sec if image_duration_sec is None else image_duration_sec
        command_text = self.MPV_IMAGE_TEMPLATE.format(player="mpv", duration=duration, media="{media}")
        parts = self._split_command_text(command_text)
        command = [media_path if part == "{media}" else part for part in parts]
        return [self._strip_outer_quotes(part) for part in command]

    @staticmethod
    def _split_windows_command(command_text: str) -> list[str]:
        if not isinstance(command_text, str):
            command_text = str(command_text)

        if not command_text:
            return []

        command_line_to_argv = ctypes.windll.shell32.CommandLineToArgvW
        command_line_to_argv.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
        command_line_to_argv.restype = ctypes.POINTER(ctypes.c_wchar_p)

        local_free = ctypes.windll.kernel32.LocalFree
        local_free.argtypes = [wintypes.HLOCAL]
        local_free.restype = wintypes.HLOCAL

        argc = ctypes.c_int(0)
        argv = command_line_to_argv(command_text, ctypes.byref(argc))
        if not argv:
            return []

        try:
            return [argv[index] for index in range(argc.value)]
        finally:
            local_free(argv)

    def _split_command_text(self, command_text: str) -> list[str]:
        try:
            if os.name == "nt":
                return self._split_windows_command(command_text)
            return shlex.split(command_text, posix=True)
        except Exception as exc:
            _safe_print(f"⚠️ komut ayrıştırılamadı, basit ayrıştırma kullanılacak ({exc})")
            return command_text.split()

    @staticmethod
    def _is_mpv_command(command: list[str]) -> bool:
        if not command:
            return False
        executable_name = Path(BorderlessFullscreenPlayer._strip_outer_quotes(command[0])).name.lower()
        return "mpv" in executable_name

    @staticmethod
    def _with_mpv_screen_target(command: list[str], screen_index: int) -> list[str]:
        normalized_screen_index = max(0, int(screen_index))
        updated_command = [part for part in command if not str(part).startswith("--screen=") and not str(part).startswith("--fs-screen=")]
        insert_at = 1 if updated_command else 0
        updated_command.insert(insert_at, f"--screen={normalized_screen_index}")
        updated_command.insert(insert_at + 1, f"--fs-screen={normalized_screen_index}")
        return updated_command

    @staticmethod
    def _is_vlc_image_fallback_allowed() -> bool:
        return os.getenv("ALLOW_VLC_IMAGE_FALLBACK", "0").strip().lower() in {
            "1",
            "true",
            "yes",
        }

    def _prefer_non_vlc_image_command(
        self,
        media_path: str,
        command: list[str],
        image_duration_sec: int | None = None,
    ) -> list[str] | None:
        if not self.is_image(media_path):
            return command

        if not self._is_vlc_command(command):
            return command

        if self._is_vlc_image_fallback_allowed():
            return command

        mpv_fallback = self._build_mpv_image_command(
            media_path,
            image_duration_sec=image_duration_sec,
        )
        if self._resolve_executable(mpv_fallback):
            _safe_print("ℹ️ image oynatımında VLC yerine mpv kullanılacak")
            return mpv_fallback

        _safe_print("⚠️ VLC image fallback devre dışı ve mpv bulunamadı, medya atlandı")
        return None

    def _pick_default_player_commands(self) -> tuple[str, str]:
        # Windows'ta VLC her içerik geçişinde pencereyi kapatıp yeniden açarken
        # masaüstünü kısa süre görünür bırakabiliyor. mpv bu geçişi daha stabil
        # yönettiği için varsayılan seçimde önceliği mpv'ye veriyoruz.
        player_candidates = ["mpv"]
        if os.name == "nt":
            player_candidates.extend(
                [
                    r"C:\Program Files\mpv\mpv.exe",
                    "mpv.exe",
                    r"C:\Program Files\VideoLAN\VLC\vlc.exe",
                    "vlc",
                    "vlc.exe",
                ]
            )
        else:
            player_candidates.append("vlc")

        for player in player_candidates:
            resolved = shutil.which(player)
            if not resolved:
                continue

            player_quoted = shlex.quote(resolved)
            if "vlc" in Path(resolved).name.lower():
                return (
                    self.VLC_VIDEO_TEMPLATE.format(player=player_quoted, media="{media}"),
                    self.VLC_IMAGE_TEMPLATE.format(
                        player=player_quoted,
                        duration="{duration}",
                        media="{media}",
                    ),
                )

            return (
                self.MPV_VIDEO_TEMPLATE.format(player=player_quoted, media="{media}"),
                self.MPV_IMAGE_TEMPLATE.format(
                    player=player_quoted,
                    duration="{duration}",
                    media="{media}",
                ),
            )

        # Keep player unresolved when nothing exists in PATH.
        return (
            self.VLC_VIDEO_TEMPLATE.format(player="vlc", media="{media}"),
            self.VLC_IMAGE_TEMPLATE.format(player="vlc", duration="{duration}", media="{media}"),
        )

    def _is_video(self, media_path: str) -> bool:
        return Path(media_path).suffix.lower() in self.VIDEO_EXTENSIONS

    def is_image(self, media_path: str) -> bool:
        return Path(media_path).suffix.lower() in self.IMAGE_EXTENSIONS

    def supports_media(self, media_path: str) -> bool:
        ext = Path(media_path).suffix.lower()
        return ext in self.VIDEO_EXTENSIONS or ext in self.IMAGE_EXTENSIONS


    def _build_widget_command(self, widget_source: str, *, allow_python_viewer: bool = True) -> list[str]:
        command_template = os.getenv("WIDGET_PLAYER_COMMAND", "")
        if command_template.strip():
            parts = self._split_command_text(command_template)
            command = [widget_source if part == "{widget}" else part for part in parts]
            command = [self._strip_outer_quotes(part) for part in command]
            if command:
                return command

        python_widget_command: list[str] | None = None
        if allow_python_viewer and self._should_use_python_widget_viewer(widget_source):
            python_widget_command = self._build_python_widget_command(widget_source)
            if not python_widget_command:
                self._python_widget_viewer_runtime_enabled = False

        if python_widget_command:
            return python_widget_command

        if self._is_widget_url(widget_source):
            windows_browser = self._resolve_windows_kiosk_browser()
            if windows_browser:
                executable, extra_flags = windows_browser
                extra_flags = self._apply_windows_monitor_position(extra_flags)
                command = [executable]
                widget_arg_included = False
                for flag in extra_flags:
                    if "{widget}" in flag:
                        widget_arg_included = True
                        command.append(flag.replace("{widget}", widget_source))
                    else:
                        command.append(flag)

                if not widget_arg_included:
                    command.append(widget_source)
                return command

        if not str(widget_source or "").strip():
            return []

        if os.name == "nt":
            return ["cmd", "/c", "start", "", widget_source]

        opener = shutil.which("xdg-open") or shutil.which("open")
        if opener:
            return [opener, widget_source]

        return []

    @staticmethod
    def _is_widget_url(widget_source: str) -> bool:
        normalized = str(widget_source or "").strip().lower()
        return (
            normalized.startswith("http://")
            or normalized.startswith("https://")
            or normalized.startswith("file://")
        )

    @staticmethod
    def _normalize_widget_source(widget_source: str) -> str:
        source = str(widget_source or "").strip()
        if not source:
            return ""

        source_path = Path(source).expanduser()
        if source_path.exists():
            try:
                return source_path.resolve().as_uri()
            except OSError:
                pass

        if BorderlessFullscreenPlayer._WINDOWS_DRIVE_PATH_PATTERN.match(source):
            try:
                return Path(source).expanduser().resolve().as_uri()
            except OSError:
                normalized_source = source.replace("\\", "/")
                return f"file:///{normalized_source}"

        if source.startswith(("/", "\\")):
            try:
                return Path(source).expanduser().resolve().as_uri()
            except OSError:
                pass

        if BorderlessFullscreenPlayer._is_widget_url(source):
            return BorderlessFullscreenPlayer._maybe_remap_stale_engine_uri(source)
        if "://" not in source and not source.startswith(("/", "\\")):
            scheme = BorderlessFullscreenPlayer._default_widget_scheme(source)
            return f"{scheme}://{source}"
        return source

    @staticmethod
    def _normalize_widget_media_source(widget_source: str) -> str:
        source = str(widget_source or "").strip()
        if not source:
            return ""

        if source.startswith(("/", "\\")):
            base_url = str(os.getenv("MEDIA_SOURCE_BASE_URL") or os.getenv("SERVER_URL") or "").strip()
            if base_url:
                return urljoin(f"{base_url.rstrip('/')}/", source.lstrip("/\\"))

        return BorderlessFullscreenPlayer._normalize_widget_source(source)

    @staticmethod
    def _maybe_remap_stale_engine_uri(widget_source: str) -> str:
        """Remap stale _MEI widget_engine file:// URLs to current runtime path."""
        try:
            parsed = urlsplit(widget_source)
        except Exception:
            return widget_source

        if parsed.scheme.lower() != "file":
            return widget_source

        decoded_path = unquote(parsed.path or "")
        if parsed.netloc and not decoded_path.startswith("/"):
            decoded_path = f"/{parsed.netloc}{decoded_path}"
        normalized_candidate_path = decoded_path.rstrip("/\\")
        candidate_path_for_name = normalized_candidate_path or decoded_path
        candidate_name = re.split(r"[\\/]+", candidate_path_for_name)[-1].lower() if candidate_path_for_name else ""
        candidate = Path(normalized_candidate_path or decoded_path)
        if os.name == "nt" and decoded_path.startswith("/") and len(decoded_path) > 3 and decoded_path[2] == ":":
            candidate = Path(decoded_path.lstrip("/"))

        if candidate_name != "widget_engine.html":
            return widget_source

        local_engine = _resolve_runtime_resource("widget_engine.html")
        if not local_engine.is_file():
            return widget_source
        local_resolved = local_engine.resolve()

        candidate_is_current_runtime = False
        try:
            if candidate.is_file():
                candidate_is_current_runtime = candidate.resolve() == local_resolved
        except OSError:
            candidate_is_current_runtime = False

        if candidate_is_current_runtime:
            return widget_source

        remapped = local_resolved.as_uri()
        local_parsed = urlsplit(remapped)
        return urlunsplit((local_parsed.scheme, local_parsed.netloc, local_parsed.path, parsed.query, parsed.fragment))

    @staticmethod
    def _default_widget_scheme(raw_source: str) -> str:
        host_candidate = str(raw_source or "").split("/", 1)[0].strip().strip("[]")
        if not host_candidate:
            return "https"

        hostname = host_candidate.rsplit("@", 1)[-1].split(":", 1)[0].lower()
        if hostname == "localhost" or hostname.endswith(".local"):
            return "http"

        try:
            ip = ipaddress.ip_address(hostname)
        except ValueError:
            return "https"

        if ip.is_loopback or ip.is_private or ip.is_link_local:
            return "http"
        return "https"

    def _normalize_widget_payload(self, widget_config: dict | None, fallback_source: str = "") -> dict | None:
        payload: dict[str, object] = {}

        if isinstance(widget_config, dict):
            widgets = widget_config.get("widgets")
            if isinstance(widgets, list) and widgets:
                normalized_widgets: list[dict] = []
                def _looks_like_embed_html(value: object) -> bool:
                    text = str(value or "").strip().lower()
                    return bool(text) and text.startswith("<") and ">" in text

                for widget in widgets:
                    if not isinstance(widget, dict):
                        continue
                    normalized_widget = dict(widget)
                    widget_type = str(normalized_widget.get("type") or "").strip().lower()
                    if widget_type in {"iframe", "url"}:
                        raw_url = (
                            normalized_widget.get("url")
                            or normalized_widget.get("content")
                            or normalized_widget.get("source")
                            or normalized_widget.get("path")
                            or ""
                        )
                        if _looks_like_embed_html(raw_url):
                            normalized_widget["type"] = "embed"
                            normalized_widget["html"] = str(raw_url)
                            normalized_widget.pop("url", None)
                        elif str(raw_url).strip():
                            normalized_widget["type"] = "iframe"
                            normalized_widget["url"] = self._normalize_widget_source(str(raw_url))
                        else:
                            normalized_widget["type"] = "empty"
                            normalized_widget.pop("url", None)
                    elif widget_type == "html":
                        normalized_widget["type"] = "card"
                        normalized_widget["html"] = str(
                            normalized_widget.get("html")
                            or normalized_widget.get("content")
                            or ""
                        )
                    elif widget_type in {"video", "image"}:
                        raw_source = (
                            normalized_widget.get("url")
                            or normalized_widget.get("content")
                            or normalized_widget.get("source")
                            or normalized_widget.get("path")
                            or ""
                        )
                        normalized_media_source = self._normalize_widget_media_source(str(raw_source))
                        if normalized_media_source:
                            normalized_widget["url"] = normalized_media_source
                        else:
                            normalized_widget["type"] = "empty"
                            normalized_widget.pop("url", None)
                    elif widget_type == "card" and "html" not in normalized_widget:
                        normalized_widget["html"] = str(normalized_widget.get("content") or "")
                    normalized_widgets.append(normalized_widget)
                if normalized_widgets:
                    payload["widgets"] = normalized_widgets

            columns = widget_config.get("columns")
            if isinstance(columns, list) and columns:
                payload["columns"] = columns
            elif isinstance(columns, int) and columns > 0:
                payload["columns"] = columns

            rows = widget_config.get("rows")
            if isinstance(rows, int) and rows > 0:
                payload["rows"] = rows

        normalized_fallback = self._normalize_widget_source(fallback_source)
        if "widgets" not in payload and normalized_fallback:
            payload["widgets"] = [{"type": "iframe", "url": normalized_fallback}]

        widgets = payload.get("widgets")
        if not isinstance(widgets, list) or not widgets:
            return None
        return payload

    @staticmethod
    def _resolve_windows_kiosk_browser() -> tuple[str, list[str]] | None:
        if os.name != "nt":
            return None

        candidates: list[tuple[str, list[str]]] = [
            (
                "msedge",
                BorderlessFullscreenPlayer._WINDOWS_BROWSER_KIOSK_FLAGS,
            ),
            (
                "chrome",
                BorderlessFullscreenPlayer._WINDOWS_BROWSER_KIOSK_FLAGS_CHROME,
            ),
        ]
        absolute_candidates = [
            (
                r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                BorderlessFullscreenPlayer._WINDOWS_BROWSER_KIOSK_FLAGS,
            ),
            (
                r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
                BorderlessFullscreenPlayer._WINDOWS_BROWSER_KIOSK_FLAGS,
            ),
            (
                r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                BorderlessFullscreenPlayer._WINDOWS_BROWSER_KIOSK_FLAGS_CHROME,
            ),
        ]

        for candidate, flags in candidates:
            resolved = shutil.which(candidate)
            if resolved:
                return resolved, flags

        for candidate, flags in absolute_candidates:
            if Path(candidate).is_file():
                return candidate, flags

        return None

    def _build_widget_source(self, widget_source: str, widget_config: dict | None = None) -> str:
        source = self._normalize_widget_source(widget_source)
        direct_url_source = self._single_url_widget_source(widget_config)
        if not direct_url_source:
            direct_url_source = self._single_iframe_widget_url(
                self._normalize_widget_payload(widget_config=widget_config, fallback_source="")
            )
        if direct_url_source:
            return direct_url_source

        payload = self._normalize_widget_payload(widget_config=widget_config, fallback_source=source)
        if payload is None:
            return source

        engine_path = _resolve_runtime_resource("widget_engine.html")
        engine_uri = engine_path.resolve().as_uri()
        encoded = quote(base64.urlsafe_b64encode(json.dumps(payload).encode("utf-8")).decode("ascii"))
        return f"{engine_uri}?config_b64={encoded}"

    def _build_widget_layout_payload(self, widget_source: str, widget_config: dict | None = None) -> dict | None:
        source = self._normalize_widget_source(widget_source)
        return self._normalize_widget_payload(widget_config=widget_config, fallback_source=source)

    def build_media_widget_payload(
        self,
        media_path: str,
        start_position_sec: float | None = None,
    ) -> dict:
        normalized_media = self._normalize_widget_source(media_path)
        media_widget: dict[str, object]
        if self.is_image(media_path):
            media_widget = {
                "type": "image",
                "url": normalized_media,
            }
        else:
            media_widget = {
                "type": "video",
                "url": normalized_media,
                "autoplay": True,
                "controls": False,
            }
            if isinstance(start_position_sec, (int, float)) and start_position_sec > 0:
                media_widget["start_position_sec"] = float(start_position_sec)

        return {
            "widgets": [media_widget],
            "columns": 1,
            "rows": 1,
        }

    def play_media_in_widget_runtime_blocking(
        self,
        media_path: str,
        duration_sec: int | None,
        start_position_sec: float | None = None,
    ) -> bool:
        payload = self.build_media_widget_payload(media_path, start_position_sec=start_position_sec)
        signature = hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        if not self.update_widget_layout(media_path, widget_config=payload, widget_signature=signature):
            self._last_interrupted = False
            return False

        if self._is_video(media_path) and (not isinstance(duration_sec, int) or duration_sec <= 0):
            while True:
                if self._stop_requested:
                    self._last_interrupted = True
                    return True
                if not self._widget_process or self._widget_process.poll() is not None:
                    self._last_interrupted = False
                    return False
                time.sleep(0.2)

        if not isinstance(duration_sec, int) or duration_sec <= 0:
            self._last_interrupted = False
            return False
        return self.wait_widget_duration(duration_sec)

    def _single_url_widget_source(self, widget_config: dict | None) -> str | None:
        if not isinstance(widget_config, dict):
            return None

        widgets = widget_config.get("widgets")
        if not isinstance(widgets, list) or len(widgets) != 1:
            return None

        widget = widgets[0]
        if not isinstance(widget, dict):
            return None

        widget_type = str(widget.get("type") or "").strip().lower()
        if widget_type != "url":
            return None

        raw_source = str(widget.get("url") or widget.get("content") or widget.get("source") or "").strip()
        if not raw_source:
            return None

        return self._normalize_widget_source(raw_source)

    @staticmethod
    def _single_iframe_widget_url(widget_payload: dict | None) -> str | None:
        if not isinstance(widget_payload, dict):
            return None
        if widget_payload.get("columns"):
            return None

        widgets = widget_payload.get("widgets")
        if not isinstance(widgets, list) or len(widgets) != 1:
            return None

        widget = widgets[0]
        if not isinstance(widget, dict):
            return None
        if str(widget.get("type") or "").strip().lower() != "iframe":
            return None

        widget_url = str(widget.get("url") or "").strip()
        return widget_url or None

    def _widget_runtime_engine_source(self) -> str:
        # PyInstaller onefile süreçlerinde parent process'in _MEI yolu child process
        # başlatılırken temizlenebiliyor. Engine kaynağını parent'ın mutlak file URI'si
        # yerine sentinel olarak iletip, child process'in kendi runtime'ında çözümlüyoruz.
        return WIDGET_ENGINE_SENTINEL

    def _resolve_widget_runtime_monitor_targets(
        self,
        target_monitor_index: int | None = None,
        clone_to_all_monitors: bool | None = None,
    ) -> list[tuple[int | None, tuple[int, int, int, int] | None]]:
        if os.name != "nt":
            return [(None, None)]

        monitor_bounds_list = self._windows_connected_monitor_bounds()
        if not monitor_bounds_list:
            return [(None, None)]

        clone_enabled = bool(clone_to_all_monitors)
        if clone_enabled and target_monitor_index is None and len(monitor_bounds_list) > 1:
            return [(monitor_index, bounds) for monitor_index, bounds in enumerate(monitor_bounds_list)]

        if isinstance(target_monitor_index, int) and 0 <= target_monitor_index < len(monitor_bounds_list):
            return [(target_monitor_index, monitor_bounds_list[target_monitor_index])]

        return [(0, monitor_bounds_list[0])]

    @staticmethod
    def _monitor_indexes_from_runtime_signature(
        runtime_signature: tuple | None,
    ) -> set[int]:
        if not isinstance(runtime_signature, tuple) or len(runtime_signature) != 3:
            return set()
        targets = runtime_signature[0]
        if not isinstance(targets, tuple):
            return set()

        indexes: set[int] = set()
        for target in targets:
            if not isinstance(target, tuple) or not target:
                continue
            monitor_index = target[0]
            if isinstance(monitor_index, int) and monitor_index >= 0:
                indexes.add(monitor_index)
        return indexes

    def _runtime_processes_snapshot(self) -> list[subprocess.Popen]:
        processes = [process for process in self._widget_runtime_processes if process is not None]
        if processes:
            return processes
        fallback_processes: list[subprocess.Popen] = []
        if self._widget_process is not None:
            fallback_processes.append(self._widget_process)
        for process in self._extra_processes:
            if process is not None and process not in fallback_processes:
                fallback_processes.append(process)
        return fallback_processes

    def _build_widget_runtime_command(
        self,
        monitor_index: int | None = None,
        monitor_bounds: tuple[int, int, int, int] | None = None,
    ) -> list[str] | None:
        source = self._widget_runtime_engine_source()
        command = self._build_python_widget_command(source)
        if not command:
            return None
        command.append("--runtime-ipc")
        command.append("--start-hidden")
        if isinstance(monitor_index, int) and monitor_index >= 0:
            command.extend(["--monitor", str(monitor_index)])
        if monitor_bounds is not None:
            x, y, width, height = monitor_bounds
            command.append(f"--monitor-bounds={x},{y},{width},{height}")
        return command

    def start_widget_engine_if_needed(
        self,
        *,
        target_monitor_index: int | None = None,
        clone_to_all_monitors: bool | None = None,
    ) -> bool:
        if not self._widget_runtime_controller_enabled():
            return False
        with self._widget_process_lock:
            runtime_targets = self._resolve_widget_runtime_monitor_targets(
                target_monitor_index=target_monitor_index,
                clone_to_all_monitors=clone_to_all_monitors,
            )
            desired_count = len(runtime_targets)
            runtime_signature = (
                tuple(
                    (
                        monitor_index if isinstance(monitor_index, int) and monitor_index >= 0 else None,
                        bounds,
                    )
                    for monitor_index, bounds in runtime_targets
                ),
                bool(clone_to_all_monitors),
                target_monitor_index if isinstance(target_monitor_index, int) and target_monitor_index >= 0 else None,
            )
            running_processes = [
                process for process in self._widget_runtime_processes
                if process and process.poll() is None
            ]
            _debug_log(
                "widget runtime ensure begin | "
                f"target_monitor_index={target_monitor_index} clone_to_all_monitors={clone_to_all_monitors} "
                f"desired_targets={runtime_targets} desired_count={desired_count} "
                f"running_pids={[getattr(process, 'pid', None) for process in running_processes]}"
            )
            last_signature = getattr(self, "_last_runtime_signature", None)
            signature_matches = last_signature == runtime_signature
            if (
                not signature_matches
                and isinstance(last_signature, tuple)
                and len(last_signature) == 3
                and isinstance(last_signature[0], tuple)
                and last_signature[1:] == runtime_signature[1:]
                and len(last_signature[0]) == len(runtime_signature[0])
            ):
                signature_matches = all(
                    (
                        last_target_index == current_target_index
                        and (last_bounds is None or last_bounds == current_bounds)
                    )
                    for (last_target_index, last_bounds), (current_target_index, current_bounds) in zip(
                        last_signature[0], runtime_signature[0]
                    )
                )

            if running_processes and signature_matches:
                _debug_log(
                    "widget runtime ensure reuse | "
                    "reason=runtime_signature_matched "
                    f"running_count={len(running_processes)} signature={runtime_signature}"
                )
                self._widget_runtime_processes = running_processes
                self._widget_process = running_processes[0]
                self._extra_processes = running_processes[1:]
                return True
            # keep_widget_runtime_warm aktifken, başlangıçta tüm monitörler için
            # prewarm edilen süreçleri hedef-monitor çağrılarında küçültmeyelim.
            # Aksi halde her ACTIVE/IDLE geçişinde runtime shrink->spawn döngüsü
            # oluşup flicker/respawn problemi üretebilir.
            if running_processes and self._keep_widget_runtime_warm:
                requested_indexes = self._monitor_indexes_from_runtime_signature(runtime_signature)
                last_indexes = self._monitor_indexes_from_runtime_signature(last_signature)
                if requested_indexes and requested_indexes.issubset(last_indexes):
                    _debug_log(
                        "widget runtime ensure reuse | "
                        "reason=keep_runtime_warm_monitor_subset "
                        f"running_count={len(running_processes)} requested_indexes={sorted(requested_indexes)} "
                        f"last_indexes={sorted(last_indexes)}"
                    )
                    self._widget_runtime_processes = running_processes
                    self._widget_process = running_processes[0]
                    self._extra_processes = running_processes[1:]
                    return True
            if running_processes and len(running_processes) == desired_count:
                _debug_log(
                    "widget runtime ensure restart | "
                    "reason=running_count_matches_but_signature_differs "
                    f"running_count={len(running_processes)} desired_count={desired_count} "
                    f"last_signature={last_signature} runtime_signature={runtime_signature}"
                )

            if running_processes and desired_count > 0 and len(running_processes) > desired_count:
                keep_all_running = (
                    self._keep_widget_runtime_warm
                    and target_monitor_index is None
                    and clone_to_all_monitors is None
                )
                if keep_all_running:
                    _debug_log(
                        "widget runtime ensure reuse | "
                        "reason=keep_runtime_warm_avoid_shrink "
                        f"running_count={len(running_processes)} desired_count={desired_count} "
                        f"target_monitor_index={target_monitor_index} clone_to_all_monitors={clone_to_all_monitors}"
                    )
                    self._widget_runtime_processes = running_processes
                    self._widget_process = running_processes[0]
                    self._extra_processes = running_processes[1:]
                    return True
                preferred_index = 0
                if desired_count == 1 and isinstance(target_monitor_index, int) and target_monitor_index >= 0:
                    preferred_index = min(target_monitor_index, len(running_processes) - 1)
                kept_processes = [running_processes[preferred_index]]
                removed_processes = [
                    process
                    for process in running_processes
                    if process not in kept_processes and process.poll() is None
                ]
                _debug_log(
                    "widget runtime ensure shrink | "
                    "reason=running_count_above_desired_count "
                    f"running_count={len(running_processes)} desired_count={desired_count} "
                    f"kept_pid={getattr(kept_processes[0], 'pid', None)} "
                    f"removed_pids={[getattr(process, 'pid', None) for process in removed_processes]}"
                )
                for process in removed_processes:
                    self._terminate_process(process, timeout_sec=2, force_tree=True)
                self._widget_runtime_processes = kept_processes
                self._widget_process = kept_processes[0]
                self._extra_processes = []
                return True

            if running_processes:
                _debug_log(
                    "widget runtime ensure restart | "
                    "reason=running_processes_need_reset "
                    f"running_count={len(running_processes)} desired_count={desired_count}"
                )
            for process in list(self._widget_runtime_processes):
                if process and process.poll() is None:
                    self._terminate_process(process, timeout_sec=2, force_tree=True)
            self._widget_runtime_processes = []
            self._widget_process = None
            self._extra_processes = []

            commands: list[list[str]] = []
            for monitor_index, monitor_bounds in runtime_targets:
                command = self._build_widget_runtime_command(
                    monitor_index=monitor_index,
                    monitor_bounds=monitor_bounds,
                )
                if command:
                    commands.append(command)
            if not commands:
                self._python_widget_viewer_runtime_enabled = False
                return False

            try:
                self._stop_requested = False
                spawned_processes: list[subprocess.Popen] = []
                for command in commands:
                    popen_kwargs = self._widget_popen_kwargs(command)
                    _debug_log(
                        "widget runtime spawn attempt | "
                        f"cwd={Path.cwd()} command={command} popen_kwargs_keys={list(popen_kwargs.keys())}"
                    )
                    process = subprocess.Popen(
                        command,
                        stdin=subprocess.PIPE,
                        text=True,
                        **popen_kwargs,
                    )
                    spawned_processes.append(process)
                self._widget_runtime_restart_count += 1
                self._widget_runtime_processes = spawned_processes
                self._widget_process = spawned_processes[0]
                self._extra_processes = spawned_processes[1:]
                _debug_log(
                    "widget runtime started | "
                    f"pids={[getattr(process, 'pid', None) for process in spawned_processes]} "
                    f"restart_count={self._widget_runtime_restart_count} "
                    f"monitor_targets={runtime_targets}"
                )
                self._last_runtime_signature = runtime_signature
                return True
            except FileNotFoundError as exc:
                _debug_log(
                    "widget runtime spawn FileNotFoundError | "
                    f"filename={exc.filename} strerror={exc.strerror} cwd={Path.cwd()} command={command}"
                )
                _safe_print(f"⚠️ widget runtime engine başlatılamadı: {exc}")
                self._widget_process = None
                self._widget_runtime_processes = []
                return False
            except Exception as exc:
                _safe_print(f"⚠️ widget runtime engine başlatılamadı: {exc}")
                self._widget_process = None
                self._widget_runtime_processes = []
                return False

    def is_direct_url_widget(self, widget_config: dict | None = None) -> bool:
        if self._single_url_widget_source(widget_config):
            return True

        normalized_payload = self._normalize_widget_payload(widget_config=widget_config, fallback_source="")
        return bool(self._single_iframe_widget_url(normalized_payload))

    def _send_widget_runtime_message(
        self,
        message: dict,
        *,
        target_monitor_index: int | None = None,
        clone_to_all_monitors: bool | None = None,
    ) -> bool:
        message_type = str(message.get("type") or "").strip().lower()
        _debug_log(
            "widget runtime message request | "
            f"type={message_type or '<unknown>'} target_monitor_index={target_monitor_index} "
            f"clone_to_all_monitors={clone_to_all_monitors}"
        )
        if not self.start_widget_engine_if_needed(
            target_monitor_index=target_monitor_index,
            clone_to_all_monitors=clone_to_all_monitors,
        ):
            _debug_log(f"widget runtime message dropped | type={message_type} reason=start_failed")
            return False

        processes = [
            process for process in self._runtime_processes_snapshot()
            if process and process.poll() is None and process.stdin
        ]
        if not processes:
            _debug_log(
                "widget runtime message dropped | "
                f"type={message_type} reason=process_not_running"
            )
            return False

        _debug_log(
            "widget runtime message send | "
            f"type={message_type} pids={[getattr(process, 'pid', None) for process in processes]}"
        )
        try:
            with self._widget_process_stdin_lock:
                for process in processes:
                    process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
                    process.stdin.flush()
            return True
        except Exception as exc:
            _safe_print(f"⚠️ widget runtime mesajı gönderilemedi: {exc}")
            return False

    def sync_widget_runtime_playlist(self, widget_items: list[dict], active_signature: str | None = None) -> bool:
        normalized_items: list[dict] = []
        for item in widget_items or []:
            if not isinstance(item, dict):
                continue
            signature = str(item.get("signature") or "").strip()
            source = str(item.get("widget_source") or "").strip()
            widget_config = item.get("widget_config") if isinstance(item.get("widget_config"), dict) else None
            if not signature:
                continue
            payload = self._build_widget_layout_payload(source, widget_config=widget_config)
            if payload is None:
                continue
            normalized_items.append({"signature": signature, "payload": payload})

        _debug_log(f"sync_widget_runtime_playlist | items={len(normalized_items)} active_signature={active_signature}")
        message = {
            "type": "playlist_sync",
            "payload": {
                "items": normalized_items,
                "active_signature": str(active_signature or "").strip() or None,
            },
        }
        return self._send_widget_runtime_message(message)

    def update_widget_layout(
        self,
        widget_source: str,
        widget_config: dict | None = None,
        widget_signature: str | None = None,
        target_monitor_index: int | None = None,
        clone_to_all_monitors: bool | None = None,
    ) -> bool:
        if widget_signature and widget_signature == getattr(self, "_active_widget_signature", None):
            _debug_log(f"update_widget_layout skipped | signature_unchanged={widget_signature}")
            return True
        if widget_signature == self._last_widget_signature:
            return True
        payload = self._build_widget_layout_payload(widget_source, widget_config=widget_config)
        if payload is None:
            _safe_print("⚠️ widget layout payload geçersiz")
            return False

        _debug_log(f"update_widget_layout | signature={widget_signature} source={widget_source[:120]}")
        # Yeni bir layout oynatımı başlarken önceki stop isteği (ör. state geçişinden
        # kalan bayrak) temizlenmeli; aksi halde wait_widget_duration hemen kesiliyor
        # ve playback döngüsü çok hızlı tekrar ederek flicker üretiyor.
        self._stop_requested = False
        message = {
            "type": "layout_update",
            "payload": {
                "signature": str(widget_signature or "").strip() or None,
                "config": payload,
            },
        }
        sent = self._send_widget_runtime_message(
            message,
            target_monitor_index=target_monitor_index,
            clone_to_all_monitors=clone_to_all_monitors,
        )
        if sent:
            self._last_widget_signature = widget_signature
            self._active_widget_signature = widget_signature
        return sent

    def stop_widget_engine(self) -> None:
        with self._widget_process_lock:
            processes = [
                process for process in self._runtime_processes_snapshot()
                if process and process.poll() is None
            ]
            if not processes:
                self._widget_process = None
                self._widget_runtime_processes = []
                return

            try:
                with self._widget_process_stdin_lock:
                    for process in processes:
                        if process.stdin:
                            process.stdin.write('{"type":"stop"}\n')
                            process.stdin.flush()
            except Exception:
                pass

            for process in processes:
                self._terminate_process(process, timeout_sec=2, force_tree=True)
            self._widget_process = None
            self._widget_runtime_processes = []

    def _stop_detached_widget_browser_processes(self) -> None:
        if os.name != "nt":
            return

        marker = str(self._last_widget_source or "").strip()
        if not marker:
            return

        normalized_marker = marker.lower()
        marker_mode = "engine" if "widget_engine.html" in normalized_marker else "source"
        if marker_mode == "source" and not self._is_widget_url(marker):
            return

        script = (
            "param([string]$Mode,[string]$Marker);"
            "$ErrorActionPreference='SilentlyContinue';"
            "$markerSafe=[Regex]::Escape($Marker);"
            "$procs=Get-CimInstance Win32_Process | Where-Object {"
            "$_.Name -in @('msedge.exe','chrome.exe') -and $_.CommandLine -and "
            "$_.CommandLine -match '--kiosk'"
            "};"
            "foreach($p in $procs){"
            "$cmd=$p.CommandLine;"
            "if($Mode -eq 'engine'){"
            "if(($cmd -match 'widget_engine\\.html') -and ($cmd -match 'config_b64=')){"
            "Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue"
            "}"
            "} else {"
            "if($cmd -match $markerSafe){"
            "Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue"
            "}"
            "}"
            "}"
        )
        kwargs = {
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "check": False,
        }
        kwargs.update(self._windows_hidden_process_kwargs())
        try:
            subprocess.run(
                ["powershell", "-NoProfile", "-Command", script, marker_mode, marker],
                **kwargs,
            )
            _debug_log(
                "stop detached widget browser processes requested | "
                f"mode={marker_mode} marker={marker[:160]}"
            )
        except Exception:
            pass

    def background_widget_engine(self) -> bool:
        with self._widget_process_lock:
            processes = [
                process for process in self._runtime_processes_snapshot()
                if process and process.poll() is None and process.stdin
            ]
            if not processes:
                self._widget_process = None
                self._widget_runtime_processes = []
                return False

            try:
                with self._widget_process_stdin_lock:
                    for process in processes:
                        process.stdin.write('{"type":"background"}\n')
                        process.stdin.flush()
                return True
            except Exception as exc:
                _safe_print(f"⚠️ widget runtime background moduna alınamadı: {exc}")
                return False

    def wait_widget_duration(self, duration_sec: int) -> bool:
        deadline = time.monotonic() + max(1, int(duration_sec))
        _debug_log(f"wait_widget_duration start | duration_sec={duration_sec} has_process={self._widget_process is not None}")
        while True:
            if self._stop_requested:
                self._last_interrupted = True
                return True
            processes = [process for process in self._runtime_processes_snapshot() if process is not None]
            if not processes:
                _debug_log(
                    "wait_widget_duration aborted | "
                    "reason=widget_process_stopped returncode=None"
                )
                self._last_interrupted = False
                return False
            return_codes = [process.poll() for process in processes]
            if any(return_code is not None for return_code in return_codes):
                _debug_log(
                    "wait_widget_duration aborted | "
                    f"reason=widget_process_stopped returncodes={return_codes}"
                )
                self._last_interrupted = False
                return False
            if time.monotonic() >= deadline:
                self._last_interrupted = False
                return True
            time.sleep(0.2)

    def clear_stop_request(self) -> None:
        # stop(stop_widget_runtime=False) ile runtime sıcak tutulduğunda kalan stop
        # bayrağı, sonraki widget turunda wait_widget_duration'ın anında kesilmesine
        # neden olabilir.
        self._stop_requested = False

    def _wait_widget_until_stop(
        self,
        process: subprocess.Popen,
        max_duration_sec: int | None = None,
    ) -> bool:
        deadline = None
        if max_duration_sec is not None:
            deadline = time.monotonic() + max(1, int(max_duration_sec))

        while True:
            if self._stop_requested:
                break
            if process.poll() is not None:
                break
            if deadline is not None and time.monotonic() >= deadline:
                break
            time.sleep(0.2)

        if process.poll() is None:
            _debug_log("_wait_widget_until_stop deadline/stop reached, terminating widget process")
            self._terminate_process(process, timeout_sec=2, force_tree=True)

        interrupted = self._stop_requested
        _debug_log(f"_wait_widget_until_stop finished | interrupted={interrupted} returncode={process.returncode}")
        self._last_interrupted = interrupted
        return (process.returncode in (0, None)) or interrupted

    def _wait_widget_processes_until_stop(self, processes: list[subprocess.Popen], max_duration_sec: int | None = None) -> bool:
        if not processes:
            self._last_interrupted = False
            return False
        unique_processes = list(dict.fromkeys(processes))
        pid_list = [getattr(process, "pid", None) for process in unique_processes]

        deadline = None
        if max_duration_sec is not None:
            deadline = time.monotonic() + max(1, int(max_duration_sec))

        wait_exit_reason = "unknown"
        while True:
            if self._stop_requested:
                wait_exit_reason = "stop_requested"
                break
            if all(process.poll() is not None for process in unique_processes):
                wait_exit_reason = "all_processes_exited"
                break
            if deadline is not None and time.monotonic() >= deadline:
                wait_exit_reason = "duration_deadline_reached"
                break
            time.sleep(0.2)

        for process in unique_processes:
            if process.poll() is None:
                self._terminate_process(process, timeout_sec=2, force_tree=True)

        interrupted = self._stop_requested
        self._last_interrupted = interrupted
        return_codes = [process.returncode for process in unique_processes]
        ok = all(process.returncode in (0, None) for process in unique_processes) or interrupted
        _debug_log(
            "_wait_widget_processes_until_stop finished | "
            f"reason={wait_exit_reason} interrupted={interrupted} max_duration_sec={max_duration_sec} "
            f"pids={pid_list} returncodes={return_codes} ok={ok}"
        )
        return ok

    def _hold_widget_slot_for_duration(self, duration_sec: int, already_elapsed_sec: float = 0.0) -> bool:
        remaining_sec = max(0.0, max(1, int(duration_sec)) - max(0.0, float(already_elapsed_sec)))
        if remaining_sec <= 0:
            self._last_interrupted = False
            return True

        deadline = time.monotonic() + remaining_sec
        while time.monotonic() < deadline:
            if self._stop_requested:
                self._last_interrupted = True
                return True
            time.sleep(0.2)

        self._last_interrupted = False
        return True

    def _build_widget_commands_for_monitors(
        self,
        source: str,
        clone_to_all_monitors: bool | None = None,
    ) -> list[list[str]]:
        command = self._build_widget_command(source)
        if not command:
            _debug_log(
                "widget_monitor_command_build | "
                "scope=for_monitors clone_enabled=False connected_monitors=0 generated_commands=0"
            )
            return []

        clone_enabled = self._should_clone_to_all_monitors() if clone_to_all_monitors is None else bool(clone_to_all_monitors)
        if not clone_enabled or os.name != "nt":
            _debug_log(
                "widget_monitor_command_build | "
                f"scope=for_monitors clone_enabled={clone_enabled} connected_monitors=0 generated_commands=1"
            )
            return [command]

        monitor_bounds_list = self._windows_connected_monitor_bounds()
        if len(monitor_bounds_list) <= 1:
            _debug_log(
                "widget_monitor_command_build | "
                f"scope=for_monitors clone_enabled={clone_enabled} "
                f"connected_monitors={len(monitor_bounds_list)} generated_commands=1"
            )
            return [command]

        commands: list[list[str]] = []
        for monitor_index, monitor_bounds in enumerate(monitor_bounds_list):
            monitor_command = [command[0], *self._apply_windows_monitor_position(command[1:], monitor_bounds=monitor_bounds)]
            if self._is_python_widget_command(command):
                x, y, width, height = monitor_bounds
                monitor_command.extend(["--monitor", str(monitor_index)])
                monitor_command.append(f"--monitor-bounds={x},{y},{width},{height}")
            commands.append(monitor_command)

        _debug_log(
            "widget_monitor_command_build | "
            f"scope=for_monitors clone_enabled={clone_enabled} "
            f"connected_monitors={len(monitor_bounds_list)} generated_commands={len(commands)}"
        )
        return commands

    def _build_widget_commands(
        self,
        source: str,
        *,
        target_monitor_index: int | None = None,
        clone_to_all_monitors: bool | None = None,
    ) -> list[list[str]]:
        clone_enabled = False if clone_to_all_monitors is None else bool(clone_to_all_monitors)
        connected_monitor_count = 0
        allow_python_viewer = True
        if os.name == "nt":
            connected_monitor_count = len(self._windows_connected_monitor_bounds())
        if os.name == "nt" and clone_enabled and target_monitor_index is None:
            # Python widget viewer tek pencere başlattığı için çoklu monitör
            # klonlamada sadece tek ekranda açılabilir ve döngüde tekrar tekrar
            # başlatılıyormuş gibi görünür. Klon modunda browser komutunu zorla.
            allow_python_viewer = False
        command = self._build_widget_command(source, allow_python_viewer=allow_python_viewer)
        if not command:
            _debug_log(
                "widget_monitor_command_build | "
                f"scope=build clone_enabled={clone_enabled} "
                f"monitor_count={connected_monitor_count} command_count=0"
            )
            return []

        if (
            os.name == "nt"
            and isinstance(target_monitor_index, int)
            and target_monitor_index >= 0
        ):
            monitor_bounds_list = self._windows_connected_monitor_bounds()
            connected_monitor_count = len(monitor_bounds_list)
            selected_monitor_bounds: tuple[int, int, int, int] | None = None
            selected_monitor_index: int | None = None

            use_active_monitor_hint = target_monitor_index == 0 and clone_to_all_monitors is None
            if use_active_monitor_hint:
                active_monitor_bounds = self._windows_active_monitor_bounds()
                if active_monitor_bounds is not None:
                    active_x, active_y, active_width, active_height = active_monitor_bounds
                    active_center_x = active_x + (active_width // 2)
                    active_center_y = active_y + (active_height // 2)
                    for monitor_index, bounds in enumerate(monitor_bounds_list):
                        x, y, width, height = bounds
                        if x <= active_center_x < (x + width) and y <= active_center_y < (y + height):
                            selected_monitor_bounds = bounds
                            selected_monitor_index = monitor_index
                            break
                    if selected_monitor_bounds is None:
                        selected_monitor_bounds = active_monitor_bounds

            if selected_monitor_bounds is None and target_monitor_index < len(monitor_bounds_list):
                selected_monitor_bounds = monitor_bounds_list[target_monitor_index]
                selected_monitor_index = target_monitor_index
            if (
                selected_monitor_bounds is None
                and monitor_bounds_list
                and target_monitor_index >= len(monitor_bounds_list)
            ):
                selected_monitor_index = len(monitor_bounds_list) - 1
                selected_monitor_bounds = monitor_bounds_list[selected_monitor_index]

            if selected_monitor_bounds is not None:
                monitor_bounds = selected_monitor_bounds
                monitor_command = [command[0], *self._apply_windows_monitor_position(command[1:], monitor_bounds=monitor_bounds)]
                if self._is_python_widget_command(command):
                    x, y, width, height = monitor_bounds
                    if selected_monitor_index is None:
                        selected_monitor_index = target_monitor_index
                    if isinstance(selected_monitor_index, int) and selected_monitor_index >= 0:
                        monitor_command.extend(["--monitor", str(selected_monitor_index)])
                    monitor_command.append(f"--monitor-bounds={x},{y},{width},{height}")
                _debug_log(
                    "widget_monitor_command_build | "
                    f"scope=build clone_enabled={clone_enabled} "
                    f"monitor_count={connected_monitor_count} command_count=1"
                )
                return [monitor_command]

        if not clone_enabled:
            _debug_log(
                "widget_monitor_command_build | "
                f"scope=build clone_enabled={clone_enabled} "
                f"monitor_count={connected_monitor_count} command_count=1"
            )
            return [command]
        commands = self._build_widget_commands_for_monitors(source, clone_to_all_monitors=clone_enabled)
        if os.name == "nt":
            connected_monitor_count = len(self._windows_connected_monitor_bounds())
        _debug_log(
            "widget_monitor_command_build | "
            f"scope=build clone_enabled={clone_enabled} "
            f"monitor_count={connected_monitor_count} command_count={len(commands)}"
        )
        return commands

    def _launch_media_processes(
        self,
        command: list[str],
        *,
        target_monitor_index: int | None = None,
        clone_to_all_monitors: bool | None = None,
    ) -> list[subprocess.Popen]:
        normalized_command = list(command)
        monitor_bounds_list: list[tuple[int, int, int, int]] = []
        connected_monitor_count = 0
        if os.name == "nt":
            monitor_bounds_list = self._windows_connected_monitor_bounds()
            connected_monitor_count = len(monitor_bounds_list)
        _debug_log(
            "media_launch_prepare | "
            f"target_monitor_index={target_monitor_index} clone_to_all_monitors={clone_to_all_monitors} "
            f"connected_monitor_count={connected_monitor_count} monitor_bounds={monitor_bounds_list} "
            f"base_command={normalized_command}"
        )
        if (
            isinstance(target_monitor_index, int)
            and target_monitor_index >= 0
            and os.name == "nt"
            and self._is_mpv_command(normalized_command)
        ):
            resolved_target_monitor_index = target_monitor_index
            use_active_monitor_hint = target_monitor_index == 0 and clone_to_all_monitors is None
            if use_active_monitor_hint and connected_monitor_count > 0:
                active_monitor_bounds = self._windows_active_monitor_bounds()
                if active_monitor_bounds is not None:
                    active_x, active_y, active_width, active_height = active_monitor_bounds
                    active_center_x = active_x + (active_width // 2)
                    active_center_y = active_y + (active_height // 2)
                    for monitor_index, bounds in enumerate(monitor_bounds_list):
                        x, y, width, height = bounds
                        if x <= active_center_x < (x + width) and y <= active_center_y < (y + height):
                            resolved_target_monitor_index = monitor_index
                            break
            if connected_monitor_count > 0:
                resolved_target_monitor_index = min(resolved_target_monitor_index, connected_monitor_count - 1)
            _debug_log(
                "media_launch_mpv_target | "
                f"requested_target_monitor_index={target_monitor_index} "
                f"resolved_target_monitor_index={resolved_target_monitor_index} "
                f"used_active_monitor_hint={use_active_monitor_hint}"
            )
            normalized_command = self._with_mpv_screen_target(
                normalized_command,
                resolved_target_monitor_index,
            )

        processes = [subprocess.Popen(normalized_command)]
        _debug_log(f"media_launch_primary_started | command={normalized_command}")
        clone_enabled = self._should_clone_to_all_monitors() if clone_to_all_monitors is None else bool(clone_to_all_monitors)
        if not clone_enabled or target_monitor_index is not None or os.name != "nt" or not self._is_mpv_command(normalized_command):
            return processes

        if len(monitor_bounds_list) <= 1:
            return processes

        for monitor_index in range(1, len(monitor_bounds_list)):
            monitor_command = self._with_mpv_screen_target(normalized_command, monitor_index)
            processes.append(subprocess.Popen(monitor_command))
            _debug_log(f"media_launch_clone_started | monitor_index={monitor_index} command={monitor_command}")
        return processes

    @staticmethod
    def _probe_media_file(media_path: str) -> str:
        try:
            media_file = Path(media_path)
            stats = media_file.stat()
            size_bytes = int(stats.st_size)
            modified_at = int(stats.st_mtime)
            signature = "n/a"
            if size_bytes > 0:
                hasher = hashlib.sha256()
                with media_file.open("rb") as handle:
                    hasher.update(handle.read(64 * 1024))
                signature = hasher.hexdigest()[:16]
            return (
                "media_probe | "
                f"path={media_path} size_bytes={size_bytes} mtime_epoch={modified_at} "
                f"head_sha256={signature}"
            )
        except Exception as exc:
            return f"media_probe_failed | path={media_path} error={exc}"

    @staticmethod
    def _inject_mpv_debug_log(command: list[str], media_path: str) -> tuple[list[str], str | None]:
        if not command or not BorderlessFullscreenPlayer._is_mpv_command(command):
            return list(command), None

        timestamp = int(time.time() * 1000)
        media_stem = Path(media_path).stem or "media"
        safe_media_stem = re.sub(r"[^a-zA-Z0-9_.-]", "_", media_stem)[:48]
        log_file = Path(tempfile.gettempdir()) / f"baylan-mpv-{safe_media_stem}-{timestamp}.log"
        debug_command = list(command)
        debug_command.insert(1, f"--log-file={log_file}")
        debug_command.insert(2, "--msg-level=all=v")
        return debug_command, str(log_file)

    def play_widget_blocking(
        self,
        widget_source: str,
        duration_sec: int,
        widget_config: dict | None = None,
        target_monitor_index: int | None = None,
        clone_to_all_monitors: bool | None = None,
    ) -> bool:
        source = self._build_widget_source(widget_source, widget_config=widget_config)
        _debug_log(f"play_widget_blocking start | duration_sec={duration_sec} source={source[:140] if source else ''}")
        if not source:
            self._last_interrupted = False
            _safe_print("⚠️ widget kaynağı boş")
            return False

        clone_enabled = False if clone_to_all_monitors is None else bool(clone_to_all_monitors)
        monitor_count = 0
        if os.name == "nt":
            monitor_count = len(self._windows_connected_monitor_bounds())

        runtime_controller_allowed = (
            os.name == "nt"
            and self._python_widget_viewer_supported
        )
        runtime_controller_skipped = not runtime_controller_allowed
        runtime_controller_requested = target_monitor_index is not None
        if (
            runtime_controller_requested
            and self._widget_runtime_controller_enabled()
            and runtime_controller_allowed
        ):
            if self._process and self._process.poll() is None:
                self._terminate_process(self._process, timeout_sec=5)
                self._process = None
            self._stop_requested = False
            if self.update_widget_layout(
                source,
                widget_config=widget_config,
                target_monitor_index=target_monitor_index,
                clone_to_all_monitors=clone_to_all_monitors,
            ):
                _debug_log("play_widget_blocking runtime-controller path active")
                return self.wait_widget_duration(duration_sec)
            if not self._widget_legacy_process_fallback_enabled():
                self._last_interrupted = False
                return False
            _debug_log(
                "play_widget_blocking runtime-controller update failed; "
                "falling back to legacy widget process launcher"
            )

        with self._widget_process_lock:
            if self._process and self._process.poll() is None:
                self._terminate_process(self._process, timeout_sec=5)
            self._process = None
            for extra_process in list(self._extra_processes):
                if extra_process and extra_process.poll() is None:
                    self._terminate_process(extra_process, timeout_sec=5)
            self._extra_processes = []

            commands = self._build_widget_commands(
                source,
                target_monitor_index=target_monitor_index,
                clone_to_all_monitors=clone_to_all_monitors,
            )
        _debug_log(
            "widget_launch_telemetry | "
            f"clone_enabled={clone_enabled} monitor_count={monitor_count} "
            f"command_count={len(commands)} runtime_controller_skipped={runtime_controller_skipped}"
        )
        _debug_log(f"play_widget_blocking fallback process commands={commands}")
        if not commands:
            self._last_interrupted = False
            _safe_print("⚠️ widget oynatılamıyor: browser/webview komutu bulunamadı")
            return False

        processes: list[subprocess.Popen] = []
        using_python_widget_viewer = self._is_python_widget_command(commands[0])
        try:
            self._stop_requested = False
            self._last_widget_source = source
            with self._widget_process_lock:
                for command in commands:
                    process = subprocess.Popen(command, **self._widget_popen_kwargs(command))
                    processes.append(process)
                    _debug_log(
                        "widget process started | "
                        f"pid={getattr(process, 'pid', None)} command={command}"
                    )
                self._widget_process = processes[0]
                self._extra_processes = processes[1:]
            wait_started_at = time.monotonic()
            result = self._wait_widget_processes_until_stop(processes, max_duration_sec=duration_sec)
            wait_elapsed_sec = max(0.0, time.monotonic() - wait_started_at)
            _debug_log(
                "play_widget_blocking wait result | "
                f"duration_sec={duration_sec} elapsed_sec={wait_elapsed_sec:.3f} "
                f"stop_requested={self._stop_requested} result={result} "
                f"returncodes={[process.returncode for process in processes]}"
            )
            if (
                result
                and not self._stop_requested
                and int(duration_sec) > 1
                and wait_elapsed_sec < float(duration_sec)
            ):
                _debug_log(
                    "widget launcher detached quickly; holding playback slot | "
                    f"duration_sec={duration_sec} elapsed_sec={wait_elapsed_sec:.3f} "
                    f"python_viewer={using_python_widget_viewer}"
                )
                return self._hold_widget_slot_for_duration(duration_sec, already_elapsed_sec=wait_elapsed_sec)
            if (
                not result
                and not self._stop_requested
                and int(duration_sec) > 1
                and wait_elapsed_sec < float(duration_sec)
            ):
                _debug_log(
                    "widget launcher exited early with failure; holding playback slot | "
                    f"duration_sec={duration_sec} elapsed_sec={wait_elapsed_sec:.3f} "
                    f"python_viewer={using_python_widget_viewer}"
                )
                self._hold_widget_slot_for_duration(duration_sec, already_elapsed_sec=wait_elapsed_sec)
            return result
        except Exception as exc:
            self._last_interrupted = False
            if using_python_widget_viewer:
                self._python_widget_viewer_runtime_enabled = False
            _safe_print(f"⚠️ widget oynatma hatası: {exc}")
            return False
        finally:
            with self._widget_process_lock:
                if self._widget_process and self._widget_process in processes:
                    self._widget_process = None
            self._extra_processes = []

    def _build_command(self, media_path: str, image_duration_sec: int | None = None) -> list[str]:
        template = self.video_command if self._is_video(media_path) else self.image_command
        duration = self.image_duration_sec if image_duration_sec is None else image_duration_sec
        command_text = template.format(media="{media}", duration=duration)
        parts = self._split_command_text(command_text)
        command = [media_path if part == "{media}" else part for part in parts]

        # Windows'ta shlex ile ayrıştırılan quoted executable path'ler ("C:\\...\\vlc.exe")
        # doğrudan subprocess'e gönderildiğinde bulunamıyor. Dış quote'ları temizle.
        return [self._strip_outer_quotes(part) for part in command]

    def _build_alternate_command(
        self,
        media_path: str,
        image_duration_sec: int | None,
        start_position_sec: float | None,
        failed_command: list[str],
    ) -> list[str] | None:
        if not failed_command:
            return None

        failed_executable = Path(self._strip_outer_quotes(failed_command[0])).name.lower()
        if "mpv" in failed_executable:
            alternate_template = self.VLC_VIDEO_TEMPLATE if self._is_video(media_path) else self.VLC_IMAGE_TEMPLATE
            alternate_command = self._split_command_text(
                alternate_template.format(player="vlc", media="{media}", duration=image_duration_sec or self.image_duration_sec)
            )
        elif "vlc" in failed_executable:
            alternate_template = self.MPV_VIDEO_TEMPLATE if self._is_video(media_path) else self.MPV_IMAGE_TEMPLATE
            alternate_command = self._split_command_text(
                alternate_template.format(player="mpv", media="{media}", duration=image_duration_sec or self.image_duration_sec)
            )
        else:
            return None

        alternate_command = [media_path if part == "{media}" else part for part in alternate_command]
        alternate_command = [self._strip_outer_quotes(part) for part in alternate_command]
        if start_position_sec and self._is_video(media_path):
            exe_name = Path(self._strip_outer_quotes(alternate_command[0])).name.lower()
            if "vlc" in exe_name:
                alternate_command.insert(1, f"--start-time={max(0, float(start_position_sec)):.3f}")
            elif "mpv" in exe_name:
                alternate_command.insert(1, f"--start={max(0, float(start_position_sec)):.3f}")

        if not self._resolve_executable(alternate_command):
            return None

        return self._prefer_non_vlc_image_command(
            media_path,
            alternate_command,
            image_duration_sec=image_duration_sec,
        )

    @staticmethod
    def _strip_outer_quotes(value: str) -> str:
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            return value[1:-1]
        return value

    @staticmethod
    def _resolve_executable(command: list[str]) -> bool:
        if not command:
            return False

        executable = BorderlessFullscreenPlayer._strip_outer_quotes(command[0])
        if Path(executable).is_file():
            return True

        return shutil.which(executable) is not None

    def play_blocking(
        self,
        media_path: str,
        image_duration_sec: int | None = None,
        start_position_sec: float | None = None,
        target_monitor_index: int | None = None,
        clone_to_all_monitors: bool | None = None,
    ) -> bool:
        _debug_log(
            "play_blocking start | "
            f"media_path={media_path} image_duration_sec={image_duration_sec} start_position_sec={start_position_sec} "
            f"target_monitor_index={target_monitor_index} clone_to_all_monitors={clone_to_all_monitors}"
        )
        if DEBUG_MODE_ENABLED:
            _debug_log(self._probe_media_file(media_path))
        if not Path(media_path).exists():
            self._last_interrupted = False
            _safe_print(f"⚠️ medya bulunamadı: {media_path}")
            return False

        if not self.supports_media(media_path):
            self._last_interrupted = False
            _safe_print(f"⚠️ desteklenmeyen medya formatı atlandı: {media_path}")
            return False

        self._stop_requested = True
        if self._process and self._process.poll() is None:
            self._terminate_process(self._process, timeout_sec=5)
        for process in self._extra_processes:
            if process and process.poll() is None:
                self._terminate_process(process, timeout_sec=5)
        self._process = None
        self._extra_processes = []

        if self._widget_process and self._widget_process.poll() is None:
            # Harici medya oynatıcıları (mpv/vlc) ile oynatımda widget runtime'ın
            # sıcak tutulması bazı ortamlarda siyah ekran/başlatılamama
            # davranışına neden olabiliyor. Bu yüzden doğrudan oynatım öncesi
            # widget runtime temizce kapatılır.
            self.stop_widget_engine()

        process = None

        try:
            command = self._build_command(media_path, image_duration_sec=image_duration_sec)
            if start_position_sec and self._is_video(media_path):
                exe_name = Path(self._strip_outer_quotes(command[0])).name.lower() if command else ""
                if "vlc" in exe_name:
                    command.insert(1, f"--start-time={max(0, float(start_position_sec)):.3f}")
                elif "mpv" in exe_name:
                    command.insert(1, f"--start={max(0, float(start_position_sec)):.3f}")
            command = self._prefer_non_vlc_image_command(
                media_path,
                command,
                image_duration_sec=image_duration_sec,
            )
            if not command:
                return False

            if DEBUG_MODE_ENABLED:
                command, mpv_debug_log_path = self._inject_mpv_debug_log(command, media_path)
                if mpv_debug_log_path:
                    _debug_log(f"play_blocking mpv_debug_log | path={mpv_debug_log_path}")

            if not self._resolve_executable(command):
                _safe_print(f"⚠️ player executable bulunamadı: {command[0] if command else 'unknown'}")
                return False

            self._stop_requested = False
            _debug_log(f"play_blocking command={command}")
            processes = self._launch_media_processes(
                command,
                target_monitor_index=target_monitor_index,
                clone_to_all_monitors=(False if clone_to_all_monitors is None else clone_to_all_monitors),
            )
            process = processes[0]
            with self._process_lock:
                self._process = process
                self._extra_processes = processes[1:]
            if self._apply_pending_stop_to_media_processes(processes):
                self._last_interrupted = True
                return True
            process.wait()

            interrupted = self._stop_requested
            if process.returncode == 0 or interrupted:
                self._last_interrupted = interrupted
                _debug_log(f"play_blocking finished | returncode={process.returncode} interrupted={interrupted}")
                return True

            _debug_log(
                "play_blocking primary failed | "
                f"returncode={process.returncode} interrupted={interrupted} media_path={media_path} command={command}"
            )

            alternate_command = self._build_alternate_command(
                media_path,
                image_duration_sec=image_duration_sec,
                start_position_sec=start_position_sec,
                failed_command=command,
            )
            if not alternate_command:
                self._last_interrupted = False
                _debug_log(f"play_blocking finished | returncode={process.returncode} interrupted=False no_alternate=true")
                return False

            _safe_print(
                "⚠️ birincil player başarısız oldu, alternatif player ile yeniden denenecek: "
                f"{Path(self._strip_outer_quotes(alternate_command[0])).name}"
            )
            _debug_log(
                "play_blocking alternate command | "
                f"failed_returncode={process.returncode} alternate={alternate_command}"
            )

            if DEBUG_MODE_ENABLED:
                alternate_command, mpv_debug_log_path = self._inject_mpv_debug_log(alternate_command, media_path)
                if mpv_debug_log_path:
                    _debug_log(f"play_blocking alternate mpv_debug_log | path={mpv_debug_log_path}")

            self._stop_requested = False
            processes = self._launch_media_processes(
                alternate_command,
                target_monitor_index=target_monitor_index,
                clone_to_all_monitors=(False if clone_to_all_monitors is None else clone_to_all_monitors),
            )
            process = processes[0]
            with self._process_lock:
                self._process = process
                self._extra_processes = processes[1:]
            if self._apply_pending_stop_to_media_processes(processes):
                self._last_interrupted = True
                return True
            process.wait()
            interrupted = self._stop_requested
            self._last_interrupted = interrupted
            _debug_log(f"play_blocking alternate finished | returncode={process.returncode} interrupted={interrupted}")
            return process.returncode == 0 or interrupted
        except Exception as exc:
            self._last_interrupted = False
            _safe_print(f"⚠️ medya oynatma hatası: {exc}")
            return False
        finally:
            with self._process_lock:
                if self._process is process:
                    self._process = None
                self._extra_processes = []

    def can_play_with_mpv_playlist(self, media_paths: list[str]) -> bool:
        if len(media_paths) < 2:
            return False

        for media_path in media_paths:
            if not Path(media_path).exists() or not self.supports_media(media_path):
                return False

        probe_path = media_paths[0]
        probe_command = self._build_command(probe_path)
        probe_command = self._prefer_non_vlc_image_command(probe_path, probe_command)
        if not probe_command:
            return False
        return self._is_mpv_command(probe_command)

    def play_mpv_playlist_blocking(
        self,
        media_paths: list[str],
        image_duration_sec: int | None = None,
        target_monitor_index: int | None = None,
        clone_to_all_monitors: bool | None = None,
    ) -> bool:
        if not self.can_play_with_mpv_playlist(media_paths):
            self._last_interrupted = False
            return False

        self._stop_requested = True
        if self._process and self._process.poll() is None:
            self._terminate_process(self._process, timeout_sec=5)
        for extra_process in self._extra_processes:
            if extra_process and extra_process.poll() is None:
                self._terminate_process(extra_process, timeout_sec=5)
        self._process = None
        self._extra_processes = []

        if not self._keep_widget_runtime_warm and self._widget_process and self._widget_process.poll() is None:
            self.stop_widget_engine()
        elif self._keep_widget_runtime_warm and self._widget_process and self._widget_process.poll() is None:
            self.background_widget_engine()

        duration = self.image_duration_sec if image_duration_sec is None else image_duration_sec
        playlist_file = None
        process = None

        try:
            first_command = self._build_command(media_paths[0])
            executable = self._strip_outer_quotes(first_command[0])
            if not self._resolve_executable([executable]):
                self._last_interrupted = False
                _safe_print(f"⚠️ player executable bulunamadı: {executable}")
                return False

            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", delete=False, suffix=".m3u") as handle:
                for media_path in media_paths:
                    handle.write(f"{media_path}\n")
                playlist_file = handle.name

            command = [
                executable,
                "--fs",
                "--border=no",
                "--force-window=immediate",
                "--ontop",
                "--quiet",
                "--background-color=0/0/0",
                "--osc=no",
                "--osd-level=0",
                "--input-cursor=no",
                f"--image-display-duration={duration}",
                "--loop-playlist=inf",
                f"--playlist={playlist_file}",
            ]

            self._stop_requested = False
            processes = self._launch_media_processes(
                command,
                target_monitor_index=target_monitor_index,
                clone_to_all_monitors=clone_to_all_monitors,
            )
            process = processes[0]
            with self._process_lock:
                self._process = process
                self._extra_processes = processes[1:]
            if self._apply_pending_stop_to_media_processes(processes):
                self._last_interrupted = True
                return True
            process.wait()

            interrupted = self._stop_requested
            self._last_interrupted = interrupted
            return process.returncode == 0 or interrupted
        except Exception as exc:
            self._last_interrupted = False
            _safe_print(f"⚠️ mpv playlist oynatma hatası: {exc}")
            return False
        finally:
            with self._process_lock:
                if self._process is process:
                    self._process = None
                self._extra_processes = []
            if playlist_file:
                Path(playlist_file).unlink(missing_ok=True)

    def stop(self, stop_widget_runtime: bool | None = None):
        caller_frame = traceback.extract_stack(limit=3)[-2] if DEBUG_MODE_ENABLED else None
        caller_hint = ""
        if caller_frame:
            caller_hint = (
                f" caller={Path(caller_frame.filename).name}:{caller_frame.lineno}:{caller_frame.name}"
            )
        _debug_log(
            f"stop called | stop_widget_runtime={stop_widget_runtime} "
            f"thread={threading.current_thread().name}{caller_hint}"
        )
        self._stop_requested = True

        if stop_widget_runtime is None:
            stop_widget_runtime = not self._keep_widget_runtime_warm

        with self._process_lock:
            process = self._process
            extra_processes = list(self._extra_processes)
        if process and process.poll() is None:
            self._terminate_process(process, timeout_sec=5)
        for process in extra_processes:
            if process and process.poll() is None:
                self._terminate_process(process, timeout_sec=5)

        with self._widget_process_lock:
            running_widget_runtime_processes = [
                process
                for process in self._runtime_processes_snapshot()
                if process and process.poll() is None
            ]
            if stop_widget_runtime and running_widget_runtime_processes:
                self.stop_widget_engine()
            elif stop_widget_runtime:
                self._stop_detached_widget_browser_processes()
            elif not stop_widget_runtime:
                backgrounded = self.background_widget_engine()
                if not backgrounded:
                    _debug_log("stop keep-warm requested but background failed | runtime_kept_alive=True")
                    self.stop_widget_engine()

        with self._process_lock:
            self._process = None
            self._extra_processes = []
        with self._widget_process_lock:
            if stop_widget_runtime:
                self._widget_process = None
            elif self._widget_process and self._widget_process.poll() is not None:
                self._widget_process = None

    @staticmethod
    def _terminate_process(process: subprocess.Popen, timeout_sec: float, force_tree: bool = False) -> None:
        if force_tree and os.name == "nt":
            pid = getattr(process, "pid", None)
            if isinstance(pid, int) and pid > 0:
                try:
                    BorderlessFullscreenPlayer._run_taskkill_tree(pid)
                    process.wait(timeout=1)
                    return
                except Exception:
                    pass

        try:
            process.terminate()
            process.wait(timeout=timeout_sec)
            return
        except subprocess.TimeoutExpired:
            pass
        except Exception:
            pass

        try:
            process.kill()
            process.wait(timeout=1)
        except Exception:
            pass

        if force_tree and os.name == "nt":
            pid = getattr(process, "pid", None)
            if isinstance(pid, int) and pid > 0:
                try:
                    BorderlessFullscreenPlayer._run_taskkill_tree(pid)
                except Exception:
                    pass


    def last_play_was_interrupted(self) -> bool:
        return self._last_interrupted
