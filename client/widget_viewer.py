import sys
import os


def _safe_print(message: str) -> None:
    try:
        print(message)
    except OSError:
        pass


def _normalize_url(source: str) -> str:
    url = str(source or "").strip()
    if not url:
        raise ValueError("Widget URL boş")
    if url.lower().startswith(("http://", "https://")):
        return url
    return f"https://{url}"


def _gui_candidates() -> list[str | None]:
    configured = os.getenv(
        "PYWEBVIEW_GUI_PRIORITY",
        "edgechromium,cef,qt,gtk,winforms,mshtml",
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


def _start_with_fallback(webview_module, widget_url: str) -> None:
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


def main() -> int:
    if len(sys.argv) < 2:
        _safe_print("Kullanım: widget_viewer.py <widget_url>")
        return 2

    try:
        widget_url = _normalize_url(sys.argv[1])
    except Exception as exc:
        _safe_print(f"Geçersiz widget URL: {exc}")
        return 2

    try:
        import webview
    except Exception as exc:
        _safe_print(f"pywebview yüklenemedi: {exc}")
        return 1

    try:
        webview.create_window(
            title="Baylan Widget",
            url=widget_url,
            fullscreen=True,
            frameless=True,
            on_top=True,
            background_color="#000000",
            text_select=False,
        )
        _start_with_fallback(webview, widget_url)
        return 0
    except Exception as exc:
        _safe_print(f"Widget viewer başlatılamadı: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
