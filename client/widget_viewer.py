import base64
import json
import os
import sys
from pathlib import Path
from urllib.parse import quote


CHROME_KIOSK_SWITCHES = {
    "kiosk": "",
    "disable-translate": "",
    "disable-infobars": "",
    "disable-session-crashed-bubble": "",
    "disable-features": "TranslateUI",
}


def _safe_print(message: str) -> None:
    try:
        print(message)
    except OSError:
        pass


def _normalize_url(source: str) -> str:
    url = str(source or "").strip()
    if not url:
        raise ValueError("Widget URL boş")
    if url.lower().startswith(("http://", "https://", "file://")):
        return url
    return f"https://{url}"


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


def _build_engine_url(widget_url: str) -> str:
    single_engine_enabled = os.getenv("WIDGET_SINGLE_ENGINE", "0").strip().lower() in {"1", "true", "yes"}
    if not single_engine_enabled:
        return widget_url

    engine_path = Path(__file__).with_name("widget_engine.html")
    engine_uri = engine_path.resolve().as_uri()
    payload = {"widgets": [{"type": "iframe", "url": widget_url}]}
    encoded = quote(base64.urlsafe_b64encode(json.dumps(payload).encode("utf-8")).decode("ascii"))
    return f"{engine_uri}?config_b64={encoded}"


def _start_with_pywebview(widget_url: str) -> None:
    import webview

    webview.create_window(
        title="Baylan Widget",
        url=widget_url,
        fullscreen=True,
        frameless=True,
        on_top=True,
        background_color="#000000",
        text_select=False,
    )
    _start_with_fallback(webview)


def _start_with_cef(widget_url: str) -> None:
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
    cef.CreateBrowserSync(window_info=window_info, url=widget_url, window_title="Baylan Widget")
    cef.MessageLoop()
    cef.Shutdown()


def main() -> int:
    if len(sys.argv) < 2:
        _safe_print("Kullanım: widget_viewer.py <widget_url>")
        return 2

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
                _start_with_cef(widget_url)
            else:
                _start_with_pywebview(widget_url)
            return 0
        except Exception as exc:
            errors.append(f"{backend}: {exc}")

    _safe_print(f"Widget viewer başlatılamadı: {'; '.join(errors)}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
