import base64
import ctypes
import ipaddress
import json
import logging
import mimetypes
import os
import sys
import threading
import tempfile
import time
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.parse import quote, unquote, urljoin, urlsplit, urlunsplit
import re


CHROME_KIOSK_SWITCHES = {
    "kiosk": "",
    "disable-translate": "",
    "disable-infobars": "",
    "disable-session-crashed-bubble": "",
    "disable-features": "TranslateUI",
    # Video autoplay in dashboard iframes requires explicit Chromium policy override.
    "autoplay-policy": "no-user-gesture-required",
}
CEF_BLACK_BACKGROUND = 0xFF000000
WINDOWS_DRIVE_PATH_PATTERN = re.compile(r"^[a-zA-Z]:[\\/]")
WEATHER_WIDGET_HREF_PATTERN = re.compile(
    r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>',
    re.IGNORECASE,
)
HTML_HEAD_PATTERN = re.compile(r"<head[^>]*>", re.IGNORECASE)
HTML_DOCTYPE_PATTERN = re.compile(r"<!doctype[^>]*>", re.IGNORECASE)
MEDIA_EXTENSION_PATTERN = re.compile(
    r"\.(mp4|m4v|webm|mov|mkv|m3u8|mpd|jpg|jpeg|png|gif|webp|bmp|svg)(?:$|[?#])",
    re.IGNORECASE,
)

DEBUG_MODE_ENABLED = os.getenv("CLIENT_DEBUG_MODE", "0").strip().lower() in {"1", "true", "yes", "on", "debug"}
WIDGET_ENGINE_SENTINEL = "__BAYLAN_WIDGET_ENGINE__"
WIDGET_VIEWER_LOG_NAME = "widget_viewer.log"


def _resolve_widget_viewer_log_path() -> Path:
    explicit = str(os.getenv("WIDGET_VIEWER_LOG_PATH", "") or "").strip()
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit))

    executable_dir = Path(sys.executable).resolve().parent
    candidates.append(executable_dir / "client" / "logs" / WIDGET_VIEWER_LOG_NAME)
    candidates.append(executable_dir / "logs" / WIDGET_VIEWER_LOG_NAME)
    candidates.append(Path(tempfile.gettempdir()) / "baylan-client" / WIDGET_VIEWER_LOG_NAME)

    for candidate in candidates:
        try:
            candidate.parent.mkdir(parents=True, exist_ok=True)
            with open(candidate, "a", encoding="utf-8"):
                pass
            return candidate
        except OSError:
            continue

    return Path(tempfile.gettempdir()) / WIDGET_VIEWER_LOG_NAME


def _setup_widget_viewer_logger() -> logging.Logger:
    logger = logging.getLogger("baylan.client.widget_viewer")
    logger.setLevel(logging.DEBUG if DEBUG_MODE_ENABLED else logging.INFO)
    logger.propagate = False
    if logger.handlers:
        return logger

    log_path = _resolve_widget_viewer_log_path()
    try:
        handler = logging.FileHandler(log_path, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(threadName)s | %(message)s"))
        logger.addHandler(handler)
        logger.info("widget_viewer log path: %s", log_path)
    except OSError:
        pass
    return logger


WIDGET_VIEWER_LOGGER = _setup_widget_viewer_logger()


def _debug_log(message: str) -> None:
    if not DEBUG_MODE_ENABLED:
        return
    _safe_print(f"[DEBUG][widget_viewer] {message}")


class _WidgetEngineDebugBridge:
    def debug_log(self, message: str, extra_json: str = "") -> None:
        message_text = str(message or "").strip()
        extra_text = str(extra_json or "").strip()
        if extra_text:
            _debug_log(f"widget_engine {message_text} | extra={extra_text}")
        else:
            _debug_log(f"widget_engine {message_text}")

    # Compatibility alias for camelCase calls.
    def debugLog(self, message: str, extra_json: str = "") -> None:
        self.debug_log(message, extra_json)



def _safe_print(message: str) -> None:
    text = str(message)
    if text:
        try:
            WIDGET_VIEWER_LOGGER.info(text)
        except Exception:
            pass
    try:
        print(text)
    except OSError:
        pass


def _normalize_url(source: str) -> str:
    url = str(source or "").strip()
    _debug_log(f"_normalize_url input={url}")
    if not url:
        raise ValueError("Widget URL boş")

    local_path = Path(url).expanduser()
    if local_path.exists():
        try:
            return local_path.resolve().as_uri()
        except OSError:
            pass

    if WINDOWS_DRIVE_PATH_PATTERN.match(url):
        try:
            return Path(url).expanduser().resolve().as_uri()
        except OSError:
            normalized_windows_path = url.replace("\\", "/")
            return f"file:///{normalized_windows_path}"

    if url.startswith(("/", "\\")):
        try:
            return Path(url).expanduser().resolve().as_uri()
        except OSError:
            pass

    if url.lower().startswith(("http://", "https://", "file://")):
        remapped = _maybe_remap_stale_engine_uri(url)
        if remapped != url:
            _debug_log(f"_normalize_url remapped_stale_engine_uri from={url} to={remapped}")
        if remapped.lower().startswith("file://"):
            _debug_log(f"_normalize_url file_uri={remapped}")
        return remapped

    scheme = _default_widget_scheme(url)
    normalized = f"{scheme}://{url}"
    _debug_log(f"_normalize_url normalized_with_scheme={normalized}")
    return normalized




def _maybe_remap_stale_engine_uri(url: str) -> str:
    """Remap stale _MEI widget_engine file:// URLs to this process runtime path."""
    try:
        parsed = urlsplit(url)
    except Exception:
        return url

    if parsed.scheme.lower() != "file":
        return url

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
        return url

    local_engine = _resolve_runtime_resource("widget_engine.html")
    if not local_engine.is_file():
        return url
    local_resolved = local_engine.resolve()

    candidate_is_current_runtime = False
    try:
        if candidate.is_file():
            candidate_is_current_runtime = candidate.resolve() == local_resolved
    except OSError:
        candidate_is_current_runtime = False

    if candidate_is_current_runtime:
        return url

    remapped = local_resolved.as_uri()
    local_parsed = urlsplit(remapped)
    return urlunsplit((local_parsed.scheme, local_parsed.netloc, local_parsed.path, parsed.query, parsed.fragment))

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


def _normalize_widget_payload(widget_config: dict, fallback_url: str | None = None) -> dict:
    payload = dict(widget_config) if isinstance(widget_config, dict) else {}
    widgets = payload.get("widgets")

    normalized_widgets: list[dict] = []

    def _looks_like_embed_html(value: object) -> bool:
        text = str(value or "").strip().lower()
        return bool(text) and text.startswith("<") and ">" in text

    def _extract_weather_widget_url(value: object) -> str | None:
        html = str(value or "")
        if "weatherwidget.io/js/widget.min.js" not in html.lower() and "weatherwidget-io" not in html.lower():
            return None
        match = WEATHER_WIDGET_HREF_PATTERN.search(html)
        if not match:
            return None
        href = match.group(1).strip()
        if "forecast7.com" not in href.lower():
            return None
        return href

    def _normalize_media_source(value: object) -> str:
        source = str(value or "").strip()
        if not source:
            return ""
        if source.lower().startswith(("http://", "https://", "file://")):
            return source
        if source.startswith("/"):
            base_url = str(os.getenv("MEDIA_SOURCE_BASE_URL") or os.getenv("SERVER_URL") or "").strip()
            if base_url:
                return urljoin(f"{base_url.rstrip('/')}/", source.lstrip("/"))
            return source
        local_path = Path(source).expanduser()
        if local_path.exists():
            try:
                return local_path.resolve().as_uri()
            except OSError:
                return source
        return _normalize_url(source)

    def _should_inline_iframe_source(source: str) -> bool:
        if os.getenv("WIDGET_INLINE_HTML_FROM_IFRAME", "1").strip().lower() not in {"1", "true", "yes", "on"}:
            return False
        parsed = urlsplit(source)
        scheme = str(parsed.scheme or "").lower()
        if scheme in {"http", "https", "file"}:
            path_part = str(parsed.path or "").lower()
            return path_part.endswith(".html") or path_part.endswith(".htm")
        if not scheme:
            lowered = source.lower()
            return lowered.endswith(".html") or lowered.endswith(".htm")
        return False

    def _inject_base_href(html: str, base_href: str) -> str:
        if "<base" in html.lower():
            return html
        base_tag = f'<base href="{base_href}">'
        match = HTML_HEAD_PATTERN.search(html)
        if match:
            insert_at = match.end()
            return f"{html[:insert_at]}{base_tag}{html[insert_at:]}"
        doctype_match = HTML_DOCTYPE_PATTERN.search(html)
        if doctype_match:
            insert_at = doctype_match.end()
            return f"{html[:insert_at]}<head>{base_tag}</head>{html[insert_at:]}"
        return f"<head>{base_tag}</head>{html}"

    def _read_embed_html_from_url(source: str) -> str | None:
        try:
            normalized_source = _normalize_url(source)
        except Exception:
            normalized_source = str(source or "").strip()
        if not normalized_source:
            return None
        parsed = urlsplit(normalized_source)
        scheme = str(parsed.scheme or "").lower()
        content = ""
        try:
            if scheme in {"http", "https", "file"}:
                with urlopen(normalized_source, timeout=8) as response:
                    charset = getattr(response.headers, "get_content_charset", lambda _d=None: None)(None) or "utf-8"
                    body = response.read(1024 * 1024)
                    content = body.decode(charset, errors="replace")
            else:
                candidate = Path(source).expanduser()
                if not candidate.exists():
                    return None
                content = candidate.read_text(encoding="utf-8")
        except Exception as exc:
            _debug_log(f"_normalize_widget_payload inline iframe read failed | source={source} error={exc}")
            return None

        if not str(content).strip():
            return None
        base_path = parsed.path or ""
        base_url = urlunsplit((parsed.scheme, parsed.netloc, base_path.rsplit("/", 1)[0] + "/", "", ""))
        return _inject_base_href(content, base_url)

    def _detect_media_type_from_url(source: object) -> str | None:
        text = str(source or "").strip()
        if not text:
            return None
        parsed = urlsplit(text)
        candidate = str(parsed.path or text).lower()
        if not MEDIA_EXTENSION_PATTERN.search(candidate):
            guessed_type = None
            parsed_scheme = str(parsed.scheme or "").lower()

            if parsed_scheme in {"http", "https"}:
                try:
                    request = Request(text, method="HEAD")
                    with urlopen(request, timeout=5) as response:
                        guessed_type = str(response.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
                except Exception as exc:
                    _debug_log(f"_detect_media_type_from_url head_failed | source={text} error={exc}")
            elif parsed_scheme == "file":
                try:
                    file_guess, _encoding = mimetypes.guess_type(unquote(parsed.path or ""))
                    guessed_type = str(file_guess or "").strip().lower()
                except Exception:
                    guessed_type = None
            else:
                try:
                    local_guess, _encoding = mimetypes.guess_type(text)
                    guessed_type = str(local_guess or "").strip().lower()
                except Exception:
                    guessed_type = None

            guessed_type = str(guessed_type or "").strip().lower()
            if guessed_type.startswith("video/"):
                return "video"
            if guessed_type.startswith("image/"):
                return "image"
            return None
        if re.search(r"\.(jpg|jpeg|png|gif|webp|bmp|svg)(?:$|[?#])", candidate):
            return "image"
        return "video"

    if isinstance(widgets, list):
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
                    weather_url = _extract_weather_widget_url(raw_url)
                    if weather_url:
                        normalized_widget["type"] = "iframe"
                        normalized_widget["url"] = _normalize_url(weather_url)
                    else:
                        normalized_widget["type"] = "embed"
                        normalized_widget["html"] = str(raw_url)
                        normalized_widget.pop("url", None)
                elif str(raw_url).strip():
                    normalized_url = _normalize_url(str(raw_url))
                    inferred_media_type = _detect_media_type_from_url(normalized_url)
                    if inferred_media_type:
                        normalized_widget["type"] = inferred_media_type
                        normalized_widget["url"] = normalized_url
                        normalized_widgets.append(normalized_widget)
                        continue
                    if _should_inline_iframe_source(normalized_url):
                        embed_html = _read_embed_html_from_url(normalized_url)
                        if embed_html:
                            normalized_widget["type"] = "embed"
                            normalized_widget["html"] = embed_html
                            normalized_widget.pop("url", None)
                            normalized_widget["source_url"] = normalized_url
                            normalized_widgets.append(normalized_widget)
                            continue
                    normalized_widget["type"] = "iframe"
                    normalized_widget["url"] = normalized_url
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
                normalized_url = _normalize_media_source(raw_source)
                if normalized_url:
                    normalized_widget["url"] = normalized_url
                    _debug_log(
                        f"_normalize_widget_payload media widget normalized | "
                        f"type={widget_type} source={str(raw_source)[:180]} url={normalized_url}"
                    )
                else:
                    normalized_widget["type"] = "empty"
                    normalized_widget.pop("url", None)
                    _debug_log(
                        f"_normalize_widget_payload media widget empty | type={widget_type} source={str(raw_source)[:180]}"
                    )
            elif widget_type == "card" and "html" not in normalized_widget:
                normalized_widget["html"] = str(normalized_widget.get("content") or "")
            normalized_widgets.append(normalized_widget)

    if normalized_widgets:
        payload["widgets"] = normalized_widgets
    elif fallback_url:
        payload["widgets"] = [{"type": "iframe", "url": fallback_url}]
    else:
        raise ValueError("Widget yapılandırması boş")

    columns = payload.get("columns")
    if isinstance(columns, list):
        pass
    else:
        parsed_columns = int(columns) if isinstance(columns, int) else None
        if parsed_columns and parsed_columns > 0:
            payload["columns"] = parsed_columns
        else:
            payload.pop("columns", None)

    rows = payload.get("rows")
    parsed_rows = int(rows) if isinstance(rows, int) else None
    if parsed_rows and parsed_rows > 0:
        payload["rows"] = parsed_rows
    else:
        payload.pop("rows", None)

    return payload


def _viewer_backend_order() -> list[str]:
    preferred = os.getenv("WIDGET_VIEWER_BACKEND", "auto").strip().lower()
    if preferred in {"cef", "pywebview"}:
        return [preferred]
    # Windows'ta ikinci ekran/harici GPU kombinasyonlarında CEF siyah ekran
    # davranışı gösterebildiği için varsayılan otomatik sırada pywebview'i
    # öne alıyoruz. CEF yine fallback olarak denenir.
    return ["pywebview", "cef"]


def _gui_candidates() -> list[str | None]:
    configured = os.getenv(
        "PYWEBVIEW_GUI_PRIORITY",
        "edgechromium,qt,gtk,winforms,mshtml,cef",
    )
    candidates: list[str | None] = []

    # Windows cihazlarda pywebview "auto" modu önce farklı GUI denemesi
    # yapabildiği için kısa süreli siyah ekran/yeniden açılma etkisi yaratabiliyor.
    # Bu yüzden varsayılan olarak auto yerine doğrudan GUI sırasını deniyoruz.
    auto_default = "0" if os.name == "nt" else "1"
    if os.getenv("PYWEBVIEW_TRY_AUTO", auto_default).strip().lower() in {"1", "true", "yes"}:
        candidates.append(None)

    for gui in configured.split(","):
        normalized = gui.strip().lower()
        if not normalized:
            continue
        if normalized not in candidates:
            candidates.append(normalized)

    return candidates or [None]


def _start_with_fallback(webview_module) -> None:
    errors: list[str] = []
    launch_started_at = time.perf_counter()
    for gui in _gui_candidates():
        try:
            kwargs = {"private_mode": True}
            if gui:
                kwargs["gui"] = gui
                _safe_print(f"Widget viewer GUI deneniyor: {gui}")
            else:
                _safe_print("Widget viewer GUI deneniyor: auto")
            _debug_log(f"pywebview.start begin | gui={gui or 'auto'} kwargs={kwargs}")
            webview_module.start(**kwargs)
            elapsed_ms = int((time.perf_counter() - launch_started_at) * 1000)
            _debug_log(f"pywebview.start success | gui={gui or 'auto'} elapsed_ms={elapsed_ms}")
            return
        except Exception as exc:
            gui_name = gui or "auto"
            _debug_log(f"pywebview.start failed | gui={gui_name} error={exc}")
            errors.append(f"{gui_name}: {exc}")

    raise RuntimeError("; ".join(errors))


def _build_engine_url(widget_url: str | None = None, widget_config: dict | None = None) -> str:
    single_engine_enabled = os.getenv("WIDGET_SINGLE_ENGINE", "0").strip().lower() in {"1", "true", "yes"}
    source = _normalize_url(widget_url) if str(widget_url or "").strip() else ""
    has_layout_config = isinstance(widget_config, dict) and isinstance(widget_config.get("widgets"), list)
    if not single_engine_enabled:
        _debug_log(f"_build_engine_url single_engine_disabled source={source}")
        return source

    if not has_layout_config:
        _debug_log(f"_build_engine_url no_layout_config source={source}")
        return source

    payload = _normalize_widget_payload(widget_config if isinstance(widget_config, dict) else {}, fallback_url=source)

    engine_uri = _resolve_runtime_resource("widget_engine.html").resolve().as_uri()
    _debug_log(f"_build_engine_url engine_uri={engine_uri} widget_count={len(payload.get('widgets') or [])}")
    encoded = quote(base64.urlsafe_b64encode(json.dumps(payload).encode("utf-8")).decode("ascii"))
    debug_suffix = "&debug=1" if DEBUG_MODE_ENABLED else ""
    return f"{engine_uri}?config_b64={encoded}{debug_suffix}"


def _runtime_message_reader(dispatch):
    _debug_log("runtime reader started")
    while True:
        line = sys.stdin.readline()
        if not line:
            _debug_log("runtime reader exiting | reason=stdin_eof")
            break
        try:
            message = json.loads(line.strip())
            _debug_log(f"runtime message received | type={message.get('type')}")
        except Exception:
            continue

        message_type = str(message.get("type") or "").strip().lower()
        if message_type == "stop":
            dispatch({"type": "stop"})
            break
        if message_type == "layout_update":
            dispatch({"type": "layout_update", "payload": message.get("payload")})
        if message_type == "playlist_sync":
            dispatch({"type": "playlist_sync", "payload": message.get("payload")})
        if message_type == "background":
            dispatch({"type": "background"})


def _build_runtime_update_script(payload: object, signature: str | None = None) -> str:
    encoded_payload = json.dumps(payload, ensure_ascii=False)
    encoded_signature = json.dumps(signature if str(signature or "").strip() else None, ensure_ascii=False)
    return (
        "(function(payload,signature){"
        "function tryApply(){"
        "if(typeof window.__baylanApplyRuntimeConfig==='function'){"
        "window.__baylanApplyRuntimeConfig(payload,signature);"
        "return true;"
        "}"
        "window.__baylanPendingConfig=payload;"
        "return false;"
        "}"
        "if(tryApply()){return;}"
        "let attempts=0;"
        "const timer=setInterval(function(){"
        "attempts+=1;"
        "if(tryApply()||attempts>=40){clearInterval(timer);}"
        "},100);"
        "})("
        + encoded_payload
        + ","
        + encoded_signature
        + ");"
    )




def _set_cef_window_visible(browser, visible: bool) -> None:
    try:
        handle = int(browser.GetOuterWindowHandle())
    except Exception:
        return

    if handle <= 0:
        return

    try:
        import ctypes

        SW_HIDE = 0
        SW_SHOW = 5
        ctypes.windll.user32.ShowWindow(handle, SW_SHOW if visible else SW_HIDE)
    except Exception:
        pass


def _parse_monitor_bounds(raw_value: str | None) -> tuple[int, int, int, int] | None:
    text = str(raw_value or "").strip()
    if not text:
        return None
    parts = [part.strip() for part in text.split(",")]
    if len(parts) != 4:
        return None
    try:
        x, y, width, height = (int(part) for part in parts)
    except ValueError:
        return None
    if width <= 0 or height <= 0:
        return None
    return x, y, width, height


def _windows_connected_monitor_bounds() -> list[tuple[int, int, int, int]]:
    if os.name != "nt":
        return []

    class RECT(ctypes.Structure):
        _fields_ = [
            ("left", ctypes.c_long),
            ("top", ctypes.c_long),
            ("right", ctypes.c_long),
            ("bottom", ctypes.c_long),
        ]

    monitor_bounds: list[tuple[int, int, int, int]] = []
    enum_proc = ctypes.WINFUNCTYPE(
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.POINTER(RECT),
        ctypes.c_long,
    )

    def _enum_callback(_monitor, _hdc, lprc_monitor, _data) -> int:
        rect = lprc_monitor.contents
        width = int(rect.right - rect.left)
        height = int(rect.bottom - rect.top)
        if width > 0 and height > 0:
            monitor_bounds.append((int(rect.left), int(rect.top), width, height))
        return 1

    try:
        ctypes.windll.user32.EnumDisplayMonitors(0, 0, enum_proc(_enum_callback), 0)
    except Exception:
        return []

    # Oyuncu tarafındaki sıralamayla uyumlu kalarak monitör indexlerini
    # Windows yerleşimine göre soldan-sağa (x), sonra yukarıdan-aşağı (y)
    # sıralıyoruz. Böylece --monitor index'i ile hedef ekran eşleşir.
    monitor_bounds.sort(key=lambda item: (item[0], item[1]))

    unique_bounds: list[tuple[int, int, int, int]] = []
    seen_bounds: set[tuple[int, int, int, int]] = set()
    for bounds in monitor_bounds:
        if bounds in seen_bounds:
            continue
        seen_bounds.add(bounds)
        unique_bounds.append(bounds)
    return unique_bounds


def _apply_cef_monitor_bounds(
    browser,
    monitor_bounds: tuple[int, int, int, int] | None,
    *,
    show_window: bool = True,
) -> None:
    if monitor_bounds is None:
        return
    try:
        handle = int(browser.GetOuterWindowHandle())
    except Exception:
        return
    if handle <= 0:
        return

    x, y, width, height = monitor_bounds
    try:
        SWP_NOZORDER = 0x0004
        SWP_SHOWWINDOW = 0x0040
        flags = SWP_NOZORDER
        if show_window:
            flags |= SWP_SHOWWINDOW
        ctypes.windll.user32.SetWindowPos(handle, 0, x, y, width, height, flags)
    except Exception:
        pass


def _start_with_pywebview(
    widget_url: str,
    runtime_ipc: bool = False,
    start_hidden: bool = False,
    monitor_bounds: tuple[int, int, int, int] | None = None,
) -> None:
    import webview
    debug_bridge = _WidgetEngineDebugBridge()
    _debug_log(
        "pywebview create_window request | "
        f"url={widget_url} runtime_ipc={runtime_ipc} start_hidden={start_hidden} monitor_bounds={monitor_bounds}"
    )

    target_fullscreen = True
    use_hidden_launch_workaround = bool(runtime_ipc and start_hidden and os.name == "nt")

    window_kwargs: dict = {
        "title": "Baylan Widget",
        "url": widget_url,
        "frameless": True,
        "on_top": True,
        "hidden": start_hidden,
        "background_color": "#000000",
        "text_select": False,
    }
    if monitor_bounds is None:
        window_kwargs["fullscreen"] = True
    else:
        x, y, width, height = monitor_bounds
        window_kwargs["x"] = x
        window_kwargs["y"] = y
        window_kwargs["width"] = width
        window_kwargs["height"] = height
        # Keep true fullscreen even on explicitly targeted monitors.
        # Windowed mode leaves OS chrome (e.g. Windows taskbar) visible.
        window_kwargs["fullscreen"] = True

    # Bazı Windows/pywebview kombinasyonlarında hidden+fullscreen pencere kısa süreli
    # görünür olup siyah ekran flicker üretebiliyor. Runtime IPC ile hidden başlangıçta
    # pencereyi geçici olarak off-screen küçük başlatıp, ilk layout mesajında hedef
    # geometri + fullscreen'e taşıyoruz.
    if use_hidden_launch_workaround:
        target_fullscreen = False
        window_kwargs["hidden"] = False
        window_kwargs["fullscreen"] = False
        window_kwargs["x"] = -32000
        window_kwargs["y"] = -32000
        if monitor_bounds is None:
            window_kwargs["width"] = 1920
            window_kwargs["height"] = 1080
        else:
            _x, _y, width, height = monitor_bounds
            window_kwargs["width"] = width
            window_kwargs["height"] = height
        _debug_log(
            "pywebview hidden launch workaround active | "
            f"target_monitor_bounds={monitor_bounds} target_fullscreen={target_fullscreen}"
        )

    create_started_at = time.perf_counter()
    window = webview.create_window(**window_kwargs, js_api=debug_bridge)
    _debug_log(
        "pywebview create_window success | "
        f"elapsed_ms={int((time.perf_counter() - create_started_at) * 1000)} "
        f"start_hidden={start_hidden} fullscreen={window_kwargs.get('fullscreen')}"
    )

    if runtime_ipc:
        _debug_log(f"pywebview runtime ipc enabled | start_hidden={start_hidden}")
        shown_once = not start_hidden

        def _prepare_window_for_show() -> None:
            if not use_hidden_launch_workaround:
                return
            try:
                if monitor_bounds is not None:
                    x, y, width, height = monitor_bounds
                    window.move(x, y)
                    window.resize(width, height)
            except Exception as exc:
                _debug_log(f"pywebview hidden launch workaround failed | error={exc}")

        def dispatch(message: dict) -> None:
            nonlocal shown_once
            _debug_log(f"pywebview dispatch message={message.get('type')}")
            if message.get("type") == "stop":
                try:
                    webview.destroy_window()
                except Exception:
                    pass
                return
            if message.get("type") == "background":
                try:
                    window.hide()
                except Exception:
                    pass
                shown_once = False
                return
            message_type = str(message.get("type") or "").strip().lower()
            raw_payload = message.get("payload")
            signature = None
            payload = None
            if message_type == "layout_update":
                if isinstance(raw_payload, dict):
                    signature = raw_payload.get("signature")
                    payload = raw_payload.get("config")
            elif message_type == "playlist_sync":
                payload = {"__playlist_sync": raw_payload}

            if payload is None:
                return

            js = _build_runtime_update_script(payload, signature=signature)
            try:
                _debug_log(f"pywebview evaluate_js | message_type={message_type} signature={signature}")
                window.evaluate_js(js)
            except Exception as exc:
                _safe_print(f"Widget runtime IPC pywebview hatası: {exc}")
            if not shown_once:
                shown_once = True
                try:
                    _prepare_window_for_show()
                    window.show()
                except Exception:
                    pass

        threading.Thread(target=_runtime_message_reader, args=(dispatch,), daemon=True).start()

    _start_with_fallback(webview)


def _start_with_cef(
    widget_url: str,
    runtime_ipc: bool = False,
    start_hidden: bool = False,
    monitor_bounds: tuple[int, int, int, int] | None = None,
) -> None:
    from cefpython3 import cefpython as cef

    switches = dict(CHROME_KIOSK_SWITCHES)
    custom_switches = os.getenv("CEF_EXTRA_SWITCHES", "").strip()
    if custom_switches:
        for raw_switch in custom_switches.split(","):
            cleaned = raw_switch.strip().lstrip("-")
            if not cleaned:
                continue
            if "=" in cleaned:
                key, value = cleaned.split("=", 1)
                switches[key] = value
            else:
                switches[cleaned] = ""

    switches.setdefault("force-device-scale-factor", "1")
    switches.setdefault("high-dpi-support", "1")

    cef.Initialize(settings={"context_menu": {"enabled": False}}, switches=switches)
    window_info = cef.WindowInfo()
    window_info.SetAsPopup(0, "Baylan Widget")
    browser = cef.CreateBrowserSync(
        window_info=window_info,
        url=widget_url,
        window_title="Baylan Widget",
        settings={"background_color": CEF_BLACK_BACKGROUND},
    )
    try:
        browser.SetZoomLevel(0)
    except Exception:
        _debug_log("cef zoom level reset skipped")
    _apply_cef_monitor_bounds(browser, monitor_bounds, show_window=not start_hidden)
    if start_hidden:
        _set_cef_window_visible(browser, False)

    if runtime_ipc:
        _debug_log(f"cef runtime ipc enabled | start_hidden={start_hidden}")
        shown_once = not start_hidden

        def dispatch(message: dict) -> None:
            nonlocal shown_once
            _debug_log(f"cef dispatch message={message.get('type')}")
            if message.get("type") == "stop":
                cef.PostTask(cef.TID_UI, cef.QuitMessageLoop)
                return
            if message.get("type") == "background":
                shown_once = False

                def _hide_window():
                    _set_cef_window_visible(browser, False)

                cef.PostTask(cef.TID_UI, _hide_window)
                return

            message_type = str(message.get("type") or "").strip().lower()
            raw_payload = message.get("payload")
            signature = None
            payload = None
            if message_type == "layout_update":
                if isinstance(raw_payload, dict):
                    signature = raw_payload.get("signature")
                    payload = raw_payload.get("config")
            elif message_type == "playlist_sync":
                payload = {"__playlist_sync": raw_payload}

            if payload is None:
                return

            def _post_js():
                nonlocal shown_once
                _debug_log(f"cef ExecuteJavascript | message_type={message_type} signature={signature}")
                browser.GetMainFrame().ExecuteJavascript(_build_runtime_update_script(payload, signature=signature))
                if not shown_once:
                    shown_once = True
                    _set_cef_window_visible(browser, True)

            cef.PostTask(cef.TID_UI, _post_js)

        threading.Thread(target=_runtime_message_reader, args=(dispatch,), daemon=True).start()

    cef.MessageLoop()
    cef.Shutdown()


def main() -> int:
    if len(sys.argv) < 2:
        _safe_print("Kullanım: widget_viewer.py <widget_url>")
        return 2

    runtime_ipc = "--runtime-ipc" in sys.argv[2:]
    _debug_log(f"main start | argv={sys.argv} runtime_ipc={runtime_ipc}")
    start_hidden = "--start-hidden" in sys.argv[2:]
    _debug_log(f"main flags | start_hidden={start_hidden}")
    monitor_bounds = None
    if "--monitor-bounds" in sys.argv[2:]:
        try:
            monitor_bounds_arg = sys.argv[sys.argv.index("--monitor-bounds") + 1]
        except (ValueError, IndexError):
            monitor_bounds_arg = ""
        monitor_bounds = _parse_monitor_bounds(monitor_bounds_arg)
    elif "--monitor" in sys.argv[2:]:
        try:
            monitor_arg = sys.argv[sys.argv.index("--monitor") + 1]
            monitor_index = int(str(monitor_arg).strip())
        except (ValueError, IndexError):
            monitor_index = -1
        monitor_list = _windows_connected_monitor_bounds()
        if not monitor_list:
            _safe_print("Monitor seçimi çözümlenemedi: bağlı monitör listesi alınamadı.")
            return 2
        if monitor_index < 0 or monitor_index >= len(monitor_list):
            _safe_print(f"Geçersiz --monitor değeri: {monitor_index}. Geçerli aralık: 0-{len(monitor_list) - 1}")
            return 2
        monitor_bounds = monitor_list[monitor_index]
        _debug_log(f"main monitor selected | index={monitor_index} bounds={monitor_bounds}")

    try:
        widget_url = _build_engine_url(_normalize_url(sys.argv[1]))
        if widget_url.lower().startswith("file://"):
            parsed_widget_url = urlsplit(widget_url)
            decoded_widget_path = unquote(parsed_widget_url.path or "")
            _debug_log(
                f"main normalized file widget path={decoded_widget_path} exists={Path(decoded_widget_path).exists()}"
            )
    except Exception as exc:
        _safe_print(f"Geçersiz widget URL: {exc}")
        return 2

    errors: list[str] = []
    launch_started_at = time.perf_counter()
    for backend in _viewer_backend_order():
        try:
            _safe_print(f"Widget viewer backend deneniyor: {backend}")
            _debug_log(f"backend try={backend} widget_url={widget_url}")
            if backend == "cef":
                _start_with_cef(
                    widget_url,
                    runtime_ipc=runtime_ipc,
                    start_hidden=start_hidden,
                    monitor_bounds=monitor_bounds,
                )
            else:
                _start_with_pywebview(
                    widget_url,
                    runtime_ipc=runtime_ipc,
                    start_hidden=start_hidden,
                    monitor_bounds=monitor_bounds,
                )
            _debug_log(f"main backend success | backend={backend} total_elapsed_ms={int((time.perf_counter() - launch_started_at) * 1000)}")
            return 0
        except Exception as exc:
            _debug_log(f"backend failed | backend={backend} error={exc}")
            errors.append(f"{backend}: {exc}")

    _safe_print(f"Widget viewer başlatılamadı: {'; '.join(errors)}")
    _debug_log(f"main exit failure | errors={errors}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
