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
from ctypes import wintypes
from pathlib import Path
from urllib.parse import quote


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

    executable_dir = Path(sys.executable).resolve().parent
    candidates.append(executable_dir.joinpath(*relative_parts))

    module_dir = Path(__file__).resolve().parent
    candidates.append(module_dir.joinpath(*relative_parts))

    for candidate in candidates:
        if candidate.is_file():
            return candidate

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

    def __init__(self):
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
        self._stop_requested = False
        self._widget_process = None
        self._widget_process_stdin_lock = threading.Lock()
        self._python_widget_viewer_supported = self._detect_python_widget_viewer_support()
        self._python_widget_viewer_runtime_enabled = True
        self._keep_widget_runtime_warm = (
            os.getenv("WIDGET_KEEP_RUNTIME_WARM", "1").strip().lower() in {"1", "true", "yes"}
        )
        self._last_interrupted = False
        _debug_log("player initialized | keep_widget_runtime_warm=%s python_viewer_supported=%s" % (self._keep_widget_runtime_warm, self._python_widget_viewer_supported))

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

    @staticmethod
    def _python_widget_viewer_enabled() -> bool:
        return os.getenv("PYTHON_WIDGET_VIEWER_ENABLED", "1").strip().lower() not in {"0", "false", "no"}

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
            if backend in {"auto", "cef"}:
                from cefpython3 import cefpython as cef

                if cef:
                    return True
        except Exception:
            pass

        try:
            if backend in {"auto", "pywebview"}:
                import webview

                if webview:
                    return True
        except Exception:
            pass

        _safe_print("⚠️ Python widget viewer pasif: ne cefpython3 ne de pywebview kullanılabilir")
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
        if getattr(sys, "frozen", False):
            return [sys.executable, "--widget", widget_source]
        if not viewer_path.is_file():
            _safe_print(f"⚠️ widget viewer script bulunamadı: {viewer_path}")
            return None
        return [sys.executable, str(viewer_path), widget_source]

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
        normalized_executable = str(Path(sys.executable).resolve())
        if (
            len(command) >= 3
            and command[0] == normalized_executable
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


    def _build_widget_command(self, widget_source: str) -> list[str]:
        command_template = os.getenv("WIDGET_PLAYER_COMMAND", "")
        if command_template.strip():
            parts = self._split_command_text(command_template)
            command = [widget_source if part == "{widget}" else part for part in parts]
            command = [self._strip_outer_quotes(part) for part in command]
            if command:
                return command

        if self._should_use_python_widget_viewer(widget_source):
            python_widget_command = self._build_python_widget_command(widget_source)
            if python_widget_command:
                return python_widget_command
            self._python_widget_viewer_runtime_enabled = False

        if self._is_widget_url(widget_source):
            windows_browser = self._resolve_windows_kiosk_browser()
            if windows_browser:
                executable, extra_flags = windows_browser
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
            return source
        if "://" not in source and not source.startswith(("/", "\\")):
            scheme = BorderlessFullscreenPlayer._default_widget_scheme(source)
            return f"{scheme}://{source}"
        return source

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
        if self._is_video(media_path) and (not isinstance(duration_sec, int) or duration_sec <= 0):
            return self.play_blocking(
                media_path,
                image_duration_sec=None,
                start_position_sec=start_position_sec,
            )

        payload = self.build_media_widget_payload(media_path, start_position_sec=start_position_sec)
        signature = hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        if not self.update_widget_layout(media_path, widget_config=payload, widget_signature=signature):
            self._last_interrupted = False
            return False
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
        return _resolve_runtime_resource("widget_engine.html").resolve().as_uri()

    def start_widget_engine_if_needed(self) -> bool:
        if not self._widget_runtime_controller_enabled():
            return False
        if self._widget_process and self._widget_process.poll() is None:
            return True

        source = self._widget_runtime_engine_source()
        command = self._build_python_widget_command(source)
        if not command:
            self._python_widget_viewer_runtime_enabled = False
            return False
        command.append("--runtime-ipc")
        command.append("--start-hidden")

        try:
            self._stop_requested = False
            popen_kwargs = self._widget_popen_kwargs(command)
            self._widget_process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                text=True,
                **popen_kwargs,
            )
            return True
        except Exception as exc:
            _safe_print(f"⚠️ widget runtime engine başlatılamadı: {exc}")
            self._widget_process = None
            return False

    def is_direct_url_widget(self, widget_config: dict | None = None) -> bool:
        if self._single_url_widget_source(widget_config):
            return True

        normalized_payload = self._normalize_widget_payload(widget_config=widget_config, fallback_source="")
        return bool(self._single_iframe_widget_url(normalized_payload))

    def _send_widget_runtime_message(self, message: dict) -> bool:
        if not self.start_widget_engine_if_needed():
            return False

        process = self._widget_process
        if not process or process.poll() is not None or not process.stdin:
            return False

        _debug_log("background_widget_engine requested")
        try:
            with self._widget_process_stdin_lock:
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
    ) -> bool:
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
        return self._send_widget_runtime_message(message)

    def stop_widget_engine(self) -> None:
        process = self._widget_process
        if not process or process.poll() is not None:
            self._widget_process = None
            return

        try:
            if process.stdin:
                with self._widget_process_stdin_lock:
                    process.stdin.write('{"type":"stop"}\n')
                    process.stdin.flush()
        except Exception:
            pass

        self._terminate_process(process, timeout_sec=2, force_tree=True)
        self._widget_process = None

    def background_widget_engine(self) -> bool:
        process = self._widget_process
        if not process or process.poll() is not None or not process.stdin:
            if process and process.poll() is not None:
                self._widget_process = None
            return False

        try:
            with self._widget_process_stdin_lock:
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
            if not self._widget_process or self._widget_process.poll() is not None:
                self._last_interrupted = False
                return False
            if time.monotonic() >= deadline:
                self._last_interrupted = False
                return True
            time.sleep(0.2)

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

    def play_widget_blocking(
        self,
        widget_source: str,
        duration_sec: int,
        widget_config: dict | None = None,
    ) -> bool:
        source = self._build_widget_source(widget_source, widget_config=widget_config)
        _debug_log(f"play_widget_blocking start | duration_sec={duration_sec} source={source[:140] if source else ''}")
        if not source:
            self._last_interrupted = False
            _safe_print("⚠️ widget kaynağı boş")
            return False

        if self._widget_runtime_controller_enabled():
            if self._process and self._process.poll() is None:
                self._terminate_process(self._process, timeout_sec=5)
                self._process = None
            self._stop_requested = False
            if self.update_widget_layout(widget_source, widget_config=widget_config):
                _debug_log("play_widget_blocking runtime-controller path active")
                return self.wait_widget_duration(duration_sec)
            if not self._widget_legacy_process_fallback_enabled():
                self._last_interrupted = False
                return False

        self.stop()

        command = self._build_widget_command(source)
        _debug_log(f"play_widget_blocking fallback process command={command}")
        if not command:
            self._last_interrupted = False
            _safe_print("⚠️ widget oynatılamıyor: browser/webview komutu bulunamadı")
            return False

        process = None
        using_python_widget_viewer = self._is_python_widget_command(command)
        try:
            self._stop_requested = False
            process = subprocess.Popen(command, **self._widget_popen_kwargs(command))
            _debug_log(f"widget process started | pid={getattr(process, 'pid', None)}")
            self._widget_process = process

            return self._wait_widget_until_stop(
                process,
                max_duration_sec=duration_sec,
            )
        except Exception as exc:
            self._last_interrupted = False
            if using_python_widget_viewer:
                self._python_widget_viewer_runtime_enabled = False
            _safe_print(f"⚠️ widget oynatma hatası: {exc}")
            return False
        finally:
            if self._widget_process is process:
                self._widget_process = None

    def _build_command(self, media_path: str, image_duration_sec: int | None = None) -> list[str]:
        template = self.video_command if self._is_video(media_path) else self.image_command
        duration = self.image_duration_sec if image_duration_sec is None else image_duration_sec
        command_text = template.format(media="{media}", duration=duration)
        parts = self._split_command_text(command_text)
        command = [media_path if part == "{media}" else part for part in parts]

        # Windows'ta shlex ile ayrıştırılan quoted executable path'ler ("C:\\...\\vlc.exe")
        # doğrudan subprocess'e gönderildiğinde bulunamıyor. Dış quote'ları temizle.
        return [self._strip_outer_quotes(part) for part in command]

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
    ) -> bool:
        _debug_log(f"play_blocking start | media_path={media_path} image_duration_sec={image_duration_sec} start_position_sec={start_position_sec}")
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
        self._process = None

        if not self._keep_widget_runtime_warm and self._widget_process and self._widget_process.poll() is None:
            self.stop_widget_engine()
        elif self._keep_widget_runtime_warm and self._widget_process and self._widget_process.poll() is None:
            self.background_widget_engine()

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

            if not self._resolve_executable(command):
                _safe_print(f"⚠️ player executable bulunamadı: {command[0] if command else 'unknown'}")
                return False

            self._stop_requested = False
            _debug_log(f"play_blocking command={command}")
            process = subprocess.Popen(command)
            self._process = process
            process.wait()

            interrupted = self._stop_requested
            self._last_interrupted = interrupted
            _debug_log(f"play_blocking finished | returncode={process.returncode} interrupted={interrupted}")
            return process.returncode == 0 or interrupted
        except Exception as exc:
            self._last_interrupted = False
            _safe_print(f"⚠️ medya oynatma hatası: {exc}")
            return False
        finally:
            if self._process is process:
                self._process = None

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

    def play_mpv_playlist_blocking(self, media_paths: list[str], image_duration_sec: int | None = None) -> bool:
        if not self.can_play_with_mpv_playlist(media_paths):
            self._last_interrupted = False
            return False

        self._stop_requested = True
        if self._process and self._process.poll() is None:
            self._terminate_process(self._process, timeout_sec=5)
        self._process = None

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
            process = subprocess.Popen(command)
            self._process = process
            process.wait()

            interrupted = self._stop_requested
            self._last_interrupted = interrupted
            return process.returncode == 0 or interrupted
        except Exception as exc:
            self._last_interrupted = False
            _safe_print(f"⚠️ mpv playlist oynatma hatası: {exc}")
            return False
        finally:
            if self._process is process:
                self._process = None
            if playlist_file:
                Path(playlist_file).unlink(missing_ok=True)

    def stop(self, stop_widget_runtime: bool | None = None):
        _debug_log(f"stop called | stop_widget_runtime={stop_widget_runtime}")
        self._stop_requested = True

        if stop_widget_runtime is None:
            stop_widget_runtime = not self._keep_widget_runtime_warm

        if self._process and self._process.poll() is None:
            self._terminate_process(self._process, timeout_sec=5)

        if stop_widget_runtime and self._widget_process and self._widget_process.poll() is None:
            self.stop_widget_engine()
        elif not stop_widget_runtime:
            self.background_widget_engine()

        self._process = None
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
                    taskkill_kwargs = BorderlessFullscreenPlayer._windows_hidden_process_kwargs()
                    subprocess.run(
                        ["taskkill", "/PID", str(pid), "/T", "/F"],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        check=False,
                        **taskkill_kwargs,
                    )
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
                    taskkill_kwargs = BorderlessFullscreenPlayer._windows_hidden_process_kwargs()
                    subprocess.run(
                        ["taskkill", "/PID", str(pid), "/T", "/F"],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        check=False,
                        **taskkill_kwargs,
                    )
                except Exception:
                    pass


    def last_play_was_interrupted(self) -> bool:
        return self._last_interrupted
