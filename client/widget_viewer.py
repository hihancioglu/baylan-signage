import base64
import json
import os
import sys
import threading
from pathlib import Path
from urllib.parse import quote
import re


CHROME_KIOSK_SWITCHES = {
    "kiosk": "",
    "disable-translate": "",
    "disable-infobars": "",
    "disable-session-crashed-bubble": "",
    "disable-features": "TranslateUI",
}
WINDOWS_DRIVE_PATH_PATTERN = re.compile(r"^[a-zA-Z]:[\\/]")


def _safe_print(message: str) -> None:
    try:
        print(message)
    except OSError:
        pass


def _normalize_url(source: str) -> str:
    url = str(source or "").strip()
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
        return url
    return f"https://{url}"


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


def _normalize_widget_payload(widget_config: dict, fallback_url: str | None = None) -> dict:
    payload = dict(widget_config) if isinstance(widget_config, dict) else {}
    widgets = payload.get("widgets")

    normalized_widgets: list[dict] = []
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
                    or ""
                )
                normalized_widget["type"] = "iframe"
                normalized_widget["url"] = _normalize_url(str(raw_url))
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
    elif fallback_url:
        payload["widgets"] = [{"type": "iframe", "url": fallback_url}]
    else:
        raise ValueError("Widget yapılandırması boş")

    if not isinstance(payload.get("columns"), list):
        payload.pop("columns", None)

    return payload


def _viewer_backend_order() -> list[str]:
    preferred = os.getenv("WIDGET_VIEWER_BACKEND", "auto").strip().lower()
    if preferred in {"cef", "pywebview"}:
        return [preferred]
    return ["cef", "pywebview"]


def _gui_candidates() -> list[str | None]:
    configured = os.getenv(
        "PYWEBVIEW_GUI_PRIORITY",
        "cef,edgechromium,qt,gtk,winforms,mshtml",
    )
    candidates: list[str | None] = []

    if os.getenv("PYWEBVIEW_TRY_AUTO", "1").strip().lower() in {"1", "true", "yes"}:
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
    for gui in _gui_candidates():
        try:
            kwargs = {"private_mode": True}
            if gui:
                kwargs["gui"] = gui
                _safe_print(f"Widget viewer GUI deneniyor: {gui}")
            else:
                _safe_print("Widget viewer GUI deneniyor: auto")
            webview_module.start(**kwargs)
            return
        except Exception as exc:
            gui_name = gui or "auto"
            errors.append(f"{gui_name}: {exc}")

    raise RuntimeError("; ".join(errors))


def _build_engine_url(widget_url: str | None = None, widget_config: dict | None = None) -> str:
    single_engine_enabled = os.getenv("WIDGET_SINGLE_ENGINE", "0").strip().lower() in {"1", "true", "yes"}
    source = _normalize_url(widget_url) if str(widget_url or "").strip() else ""
    if not single_engine_enabled:
        return source

    payload = _normalize_widget_payload(widget_config if isinstance(widget_config, dict) else {}, fallback_url=source)

    engine_uri = _resolve_runtime_resource("widget_engine.html").resolve().as_uri()
    encoded = quote(base64.urlsafe_b64encode(json.dumps(payload).encode("utf-8")).decode("ascii"))
    return f"{engine_uri}?config_b64={encoded}"


def _runtime_message_reader(dispatch):
    while True:
        line = sys.stdin.readline()
        if not line:
            break
        try:
            message = json.loads(line.strip())
        except Exception:
            continue

        message_type = str(message.get("type") or "").strip().lower()
        if message_type == "stop":
            dispatch({"type": "stop"})
            break
        if message_type == "layout_update":
            dispatch({"type": "layout_update", "payload": message.get("payload")})


def _build_runtime_update_script(payload: object) -> str:
    encoded_payload = json.dumps(payload, ensure_ascii=False)
    return (
        "(function(payload){"
        "function tryApply(){"
        "if(typeof window.__baylanApplyRuntimeConfig==='function'){"
        "window.__baylanApplyRuntimeConfig(payload);"
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
        + ");"
    )


def _start_with_pywebview(widget_url: str, runtime_ipc: bool = False) -> None:
    import webview

    window = webview.create_window(
        title="Baylan Widget",
        url=widget_url,
        fullscreen=True,
        frameless=True,
        on_top=True,
        background_color="#000000",
        text_select=False,
    )

    if runtime_ipc:
        def dispatch(message: dict) -> None:
            if message.get("type") == "stop":
                try:
                    webview.destroy_window()
                except Exception:
                    pass
                return
            payload = message.get("payload")
            js = _build_runtime_update_script(payload)
            try:
                window.evaluate_js(js)
            except Exception as exc:
                _safe_print(f"Widget runtime IPC pywebview hatası: {exc}")

        threading.Thread(target=_runtime_message_reader, args=(dispatch,), daemon=True).start()

    _start_with_fallback(webview)


def _start_with_cef(widget_url: str, runtime_ipc: bool = False) -> None:
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

    cef.Initialize(settings={"context_menu": {"enabled": False}}, switches=switches)
    window_info = cef.WindowInfo()
    window_info.SetAsPopup(0, "Baylan Widget")
    browser = cef.CreateBrowserSync(window_info=window_info, url=widget_url, window_title="Baylan Widget")

    if runtime_ipc:
        def dispatch(message: dict) -> None:
            if message.get("type") == "stop":
                cef.PostTask(cef.TID_UI, cef.QuitMessageLoop)
                return

            payload = message.get("payload")

            def _post_js():
                browser.GetMainFrame().ExecuteJavascript(_build_runtime_update_script(payload))

            cef.PostTask(cef.TID_UI, _post_js)

        threading.Thread(target=_runtime_message_reader, args=(dispatch,), daemon=True).start()

    cef.MessageLoop()
    cef.Shutdown()


def main() -> int:
    if len(sys.argv) < 2:
        _safe_print("Kullanım: widget_viewer.py <widget_url>")
        return 2

    runtime_ipc = "--runtime-ipc" in sys.argv[2:]

    try:
        widget_url = _build_engine_url(_normalize_url(sys.argv[1]))
    except Exception as exc:
        _safe_print(f"Geçersiz widget URL: {exc}")
        return 2

    errors: list[str] = []
    for backend in _viewer_backend_order():
        try:
            _safe_print(f"Widget viewer backend deneniyor: {backend}")
            if backend == "cef":
                _start_with_cef(widget_url, runtime_ipc=runtime_ipc)
            else:
                _start_with_pywebview(widget_url, runtime_ipc=runtime_ipc)
            return 0
        except Exception as exc:
            errors.append(f"{backend}: {exc}")

    _safe_print(f"Widget viewer başlatılamadı: {'; '.join(errors)}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
