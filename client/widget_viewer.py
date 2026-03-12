import sys


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
        webview.start(gui="edgechromium", private_mode=True)
        return 0
    except Exception as exc:
        _safe_print(f"Widget viewer başlatılamadı: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
