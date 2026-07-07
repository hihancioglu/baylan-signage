import base64
import json
import tempfile
from urllib.parse import urlparse, parse_qs, unquote
import unittest
from unittest.mock import patch
from pathlib import Path

from client import widget_viewer


class TestWidgetViewer(unittest.TestCase):
    def test_gui_candidates_default_includes_auto_and_edge(self):
        with patch.dict("os.environ", {}, clear=False):
            candidates = widget_viewer._gui_candidates()

        self.assertEqual(candidates[0], None)
        self.assertIn("edgechromium", candidates)

    def test_gui_candidates_honors_priority_env(self):
        with patch.dict(
            "os.environ",
            {"PYWEBVIEW_GUI_PRIORITY": "qt,edgechromium", "PYWEBVIEW_TRY_AUTO": "0"},
            clear=False,
        ):
            candidates = widget_viewer._gui_candidates()

        self.assertEqual(candidates, ["qt", "edgechromium"])

    def test_start_with_fallback_tries_next_gui_when_first_fails(self):
        fake_webview = unittest.mock.Mock()
        fake_webview.start.side_effect = [RuntimeError("edge failed"), None]

        with patch("client.widget_viewer._gui_candidates", return_value=["edgechromium", "qt"]):
            widget_viewer._start_with_fallback(fake_webview)

        self.assertEqual(fake_webview.start.call_count, 2)
        self.assertEqual(fake_webview.start.call_args_list[0].kwargs["gui"], "edgechromium")
        self.assertEqual(fake_webview.start.call_args_list[1].kwargs["gui"], "qt")

    def test_backend_order_prefers_configured_backend(self):
        with patch.dict("os.environ", {"WIDGET_VIEWER_BACKEND": "pywebview"}, clear=False):
            self.assertEqual(widget_viewer._viewer_backend_order(), ["pywebview"])

        with patch.dict("os.environ", {"WIDGET_VIEWER_BACKEND": "auto"}, clear=False):
            self.assertEqual(widget_viewer._viewer_backend_order(), ["pywebview"])


    def test_parse_runtime_options_reads_flags_in_single_pass(self):
        options = widget_viewer._parse_runtime_options(
            ["widget_viewer.py", "https://example.com", "--runtime-ipc", "--start-hidden", "--monitor-bounds", "10,20,1920,1080"]
        )

        self.assertTrue(options.runtime_ipc)
        self.assertTrue(options.start_hidden)
        self.assertEqual(options.monitor_bounds, (10, 20, 1920, 1080))

    def test_parse_runtime_options_supports_equals_syntax(self):
        options = widget_viewer._parse_runtime_options(
            [
                "widget_viewer.py",
                "https://example.com",
                "--runtime-ipc",
                "--start-hidden",
                "--monitor=1",
                "--monitor-bounds=30,40,1280,720",
            ]
        )

        self.assertTrue(options.runtime_ipc)
        self.assertTrue(options.start_hidden)
        self.assertEqual(options.monitor_bounds, (30, 40, 1280, 720))

    def test_parse_runtime_options_resolves_monitor_index(self):
        with patch("client.widget_viewer._windows_connected_monitor_bounds", return_value=[(0, 0, 1920, 1080), (1920, 0, 1920, 1080)]):
            options = widget_viewer._parse_runtime_options(
                ["widget_viewer.py", "https://example.com", "--monitor", "1"]
            )

        self.assertEqual(options.monitor_bounds, (1920, 0, 1920, 1080))

    def test_start_with_pywebview_starts_windowed_without_monitor_bounds(self):
        fake_webview = unittest.mock.Mock()
        fake_webview.create_window.return_value = unittest.mock.Mock()

        with patch.dict("sys.modules", {"webview": fake_webview}):
            widget_viewer._start_with_pywebview("https://example.com")

        self.assertFalse(fake_webview.create_window.call_args.kwargs["fullscreen"])
        self.assertEqual(fake_webview.create_window.call_args.kwargs["width"], 16)
        self.assertEqual(fake_webview.create_window.call_args.kwargs["height"], 16)

    def test_start_with_pywebview_starts_windowed_with_monitor_bounds(self):
        fake_webview = unittest.mock.Mock()
        fake_webview.create_window.return_value = unittest.mock.Mock()

        with patch.dict("sys.modules", {"webview": fake_webview}):
            widget_viewer._start_with_pywebview("https://example.com", monitor_bounds=(1920, 0, 1920, 1080))

        self.assertEqual(fake_webview.create_window.call_args.kwargs["x"], 1920)
        self.assertEqual(fake_webview.create_window.call_args.kwargs["y"], 0)
        self.assertEqual(fake_webview.create_window.call_args.kwargs["width"], 1920)
        self.assertEqual(fake_webview.create_window.call_args.kwargs["height"], 1080)
        self.assertFalse(fake_webview.create_window.call_args.kwargs["fullscreen"])

    def test_start_with_pywebview_background_hides_window_and_promotes_fullscreen_on_foreground(self):
        fake_window = unittest.mock.Mock()
        fake_webview = unittest.mock.Mock()
        fake_webview.create_window.return_value = fake_window

        def _fake_runtime_reader(dispatch):
            dispatch({"type": "background"})
            dispatch(
                {
                    "type": "layout_update",
                    "payload": {
                        "signature": "sig-1",
                        "config": {"widgets": [{"type": "iframe", "url": "https://example.com"}]},
                    },
                }
            )

        class _ImmediateThread:
            def __init__(self, target=None, args=(), daemon=False):
                self._target = target
                self._args = args

            def start(self):
                if self._target:
                    self._target(*self._args)

        with patch.dict("sys.modules", {"webview": fake_webview}), patch(
            "client.widget_viewer._runtime_message_reader", side_effect=_fake_runtime_reader
        ), patch("client.widget_viewer.threading.Thread", _ImmediateThread), patch(
            "client.widget_viewer._start_with_fallback"
        ), patch("client.widget_viewer.os.name", "nt"):
            widget_viewer._start_with_pywebview("https://example.com", runtime_ipc=True, monitor_bounds=(0, 0, 1920, 1080))

        fake_window.hide.assert_called()
        fake_window.minimize.assert_not_called()
        fake_window.toggle_fullscreen.assert_called_once()
        fake_window.move.assert_not_called()
        fake_window.resize.assert_not_called()
        fake_window.show.assert_called_once()
        show_index = fake_window.method_calls.index(unittest.mock.call.show())
        evaluate_index = fake_window.method_calls.index(unittest.mock.call.evaluate_js(unittest.mock.ANY))
        self.assertLess(show_index, evaluate_index)


    def test_widget_engine_preserves_iframe_url_for_server_driven_live_updates(self):
        engine = Path("client/widget_engine.html").read_text(encoding="utf-8")

        self.assertIn("const source = normalizeIframeUrl(url);", engine)
        self.assertIn("frame.src = source;", engine)
        self.assertNotIn("_baylan_widget_instance", engine)
        self.assertNotIn("refreshIntervalSeconds", engine)
        self.assertNotIn("window.setInterval(loadFreshSource", engine)

    def test_widget_engine_hides_iframe_scrollbars_and_blocks_stuck_blank_frames(self):
        engine = Path("client/widget_engine.html").read_text(encoding="utf-8")

        self.assertIn("scrollbar-width: none;", engine)
        self.assertIn("frame.scrolling = \"no\";", engine)
        self.assertIn("block();\n          return;", engine)

    def test_build_engine_url_keeps_direct_url_when_layout_missing(self):
        with patch.dict("os.environ", {"WIDGET_SINGLE_ENGINE": "1"}, clear=False):
            result = widget_viewer._build_engine_url("https://example.com")

        self.assertEqual(result, "https://example.com")

    def test_build_engine_url_wraps_source_when_layout_exists(self):
        with patch.dict("os.environ", {"WIDGET_SINGLE_ENGINE": "1"}, clear=False):
            result = widget_viewer._build_engine_url(
                "https://example.com",
                widget_config={"widgets": [{"type": "iframe", "url": "https://example.com"}]},
            )

        self.assertIn("widget_engine.html", result)
        self.assertIn("config_b64=", result)


    def test_build_engine_url_falls_back_when_meipass_resource_missing(self):
        with patch.dict('os.environ', {'WIDGET_SINGLE_ENGINE': '1'}, clear=False), patch(
            'client.widget_viewer.sys._MEIPASS', '/tmp/nonexistent-meipass', create=True
        ):
            result = widget_viewer._build_engine_url(
                'https://example.com',
                widget_config={'widgets': [{'type': 'iframe', 'url': 'https://example.com'}]},
            )

        self.assertIn('widget_engine.html', result)
        self.assertNotIn('/tmp/nonexistent-meipass', result)

    def test_build_engine_url_encodes_widgets_and_columns(self):
        config = {
            "widgets": [
                {"type": "iframe", "url": "https://widget-a.example.com"},
                {"type": "iframe", "url": "https://widget-b.example.com"},
            ],
            "columns": [{"width": 6}, {"width": 6}],
        }

        with patch.dict("os.environ", {"WIDGET_SINGLE_ENGINE": "1"}, clear=False):
            result = widget_viewer._build_engine_url(widget_url=None, widget_config=config)

        parsed = urlparse(result)
        encoded = parse_qs(parsed.query)["config_b64"][0]
        payload = json.loads(base64.urlsafe_b64decode(unquote(encoded)).decode("utf-8"))
        self.assertEqual(payload["widgets"], config["widgets"])
        self.assertEqual(payload["columns"], config["columns"])


    def test_build_engine_url_preserves_numeric_columns(self):
        config = {
            "widgets": [
                {"type": "iframe", "url": "https://widget-a.example.com"},
                {"type": "iframe", "url": "https://widget-b.example.com"},
            ],
            "columns": 2,
        }

        with patch.dict("os.environ", {"WIDGET_SINGLE_ENGINE": "1"}, clear=False):
            result = widget_viewer._build_engine_url(widget_url=None, widget_config=config)

        parsed = urlparse(result)
        encoded = parse_qs(parsed.query)["config_b64"][0]
        payload = json.loads(base64.urlsafe_b64decode(unquote(encoded)).decode("utf-8"))
        self.assertEqual(payload["columns"], 2)


    def test_build_engine_url_preserves_empty_cells_and_rows(self):
        config = {
            "widgets": [
                {"type": "iframe", "url": "https://widget-a.example.com"},
                {"type": "empty"},
                {"type": "iframe", "url": "https://widget-b.example.com"},
                {"type": "empty"},
            ],
            "columns": 2,
            "rows": 2,
        }

        with patch.dict("os.environ", {"WIDGET_SINGLE_ENGINE": "1"}, clear=False):
            result = widget_viewer._build_engine_url(widget_url=None, widget_config=config)

        parsed = urlparse(result)
        encoded = parse_qs(parsed.query)["config_b64"][0]
        payload = json.loads(base64.urlsafe_b64decode(unquote(encoded)).decode("utf-8"))
        self.assertEqual(payload["rows"], 2)
        self.assertEqual(payload["widgets"][1], {"type": "empty"})
        self.assertEqual(payload["widgets"][3], {"type": "empty"})

    def test_normalize_url_uses_file_uri_for_existing_local_file(self):
        with tempfile.NamedTemporaryFile(suffix=".html", delete=False) as handle:
            handle.write(b"<html></html>")
            local_path = handle.name

        try:
            self.assertEqual(widget_viewer._normalize_url(local_path), Path(local_path).resolve().as_uri())
        finally:
            Path(local_path).unlink(missing_ok=True)


    def test_normalize_url_remaps_missing_widget_engine_file_uri(self):
        stale = "file:///C:/ProgramData/BaylanSignage/RuntimeTmp/_MEI12345/client/widget_engine.html?config_b64=abc"
        with tempfile.NamedTemporaryFile(suffix=".html", delete=False) as handle:
            engine_path = Path(handle.name)
            handle.write(b"<html></html>")
        remapped_engine = engine_path.with_name("widget_engine.html")
        engine_path.rename(remapped_engine)

        try:
            with patch('client.widget_viewer._resolve_runtime_resource', return_value=remapped_engine):
                result = widget_viewer._normalize_url(stale)
        finally:
            remapped_engine.unlink(missing_ok=True)

        self.assertIn('widget_engine.html', result)
        self.assertIn('config_b64=abc', result)
        self.assertNotIn('/_MEI12345/', result)

    def test_normalize_url_remaps_foreign_widget_engine_even_when_file_exists(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_root = Path(tmpdir)
            foreign_engine = tmp_root / "_MEIforeign" / "client" / "widget_engine.html"
            foreign_engine.parent.mkdir(parents=True, exist_ok=True)
            foreign_engine.write_text("<html>foreign</html>", encoding="utf-8")

            local_engine = tmp_root / "_MEIlocal" / "client" / "widget_engine.html"
            local_engine.parent.mkdir(parents=True, exist_ok=True)
            local_engine.write_text("<html>local</html>", encoding="utf-8")

            stale = f"{foreign_engine.resolve().as_uri()}?config_b64=abc"
            with patch("client.widget_viewer._resolve_runtime_resource", return_value=local_engine):
                result = widget_viewer._normalize_url(stale)

        self.assertIn(local_engine.resolve().as_uri(), result)
        self.assertIn("config_b64=abc", result)

    def test_normalize_url_remaps_widget_engine_uri_with_trailing_slash(self):
        stale = "file:///C:/ProgramData/BaylanSignage/RuntimeTmp/_MEI12345/client/widget_engine.html/?config_b64=abc"
        with tempfile.NamedTemporaryFile(suffix=".html", delete=False) as handle:
            engine_path = Path(handle.name)
            handle.write(b"<html></html>")
        remapped_engine = engine_path.with_name("widget_engine.html")
        engine_path.rename(remapped_engine)

        try:
            with patch("client.widget_viewer._resolve_runtime_resource", return_value=remapped_engine):
                result = widget_viewer._normalize_url(stale)
        finally:
            remapped_engine.unlink(missing_ok=True)

        self.assertIn(remapped_engine.resolve().as_uri(), result)
        self.assertIn("config_b64=abc", result)

    def test_normalize_url_remaps_widget_engine_uri_with_windows_backslashes(self):
        stale = r"file://C:\ProgramData\BaylanSignage\RuntimeTmp\_MEI12345\client\widget_engine.html?config_b64=abc"
        with tempfile.NamedTemporaryFile(suffix=".html", delete=False) as handle:
            engine_path = Path(handle.name)
            handle.write(b"<html></html>")
        remapped_engine = engine_path.with_name("widget_engine.html")
        engine_path.rename(remapped_engine)

        try:
            with patch("client.widget_viewer._resolve_runtime_resource", return_value=remapped_engine):
                result = widget_viewer._normalize_url(stale)
        finally:
            remapped_engine.unlink(missing_ok=True)

        self.assertIn(remapped_engine.resolve().as_uri(), result)
        self.assertIn("config_b64=abc", result)

    def test_build_engine_url_infers_video_widget_from_iframe_without_extension_via_content_type(self):
        class _FakeResponse:
            def __init__(self):
                self.headers = {"Content-Type": "video/mp4"}

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

        widget_config = {"widgets": [{"type": "iframe", "url": "https://media.example.com/stream?id=1"}]}
        with patch.dict("os.environ", {"WIDGET_SINGLE_ENGINE": "1"}, clear=False), patch(
            "client.widget_viewer.urlopen",
            return_value=_FakeResponse(),
        ):
            result = widget_viewer._build_engine_url(widget_url=None, widget_config=widget_config)

        parsed = urlparse(result)
        encoded = parse_qs(parsed.query)["config_b64"][0]
        payload = json.loads(base64.urlsafe_b64decode(unquote(encoded)).decode("utf-8"))
        self.assertEqual(payload["widgets"][0]["type"], "video")

    def test_build_engine_url_keeps_iframe_when_content_type_is_text_html(self):
        class _FakeResponse:
            def __init__(self):
                self.headers = {"Content-Type": "text/html; charset=utf-8"}

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

        widget_config = {"widgets": [{"type": "iframe", "url": "https://dashboard.example.com/stream"}]}
        with patch.dict("os.environ", {"WIDGET_SINGLE_ENGINE": "1"}, clear=False), patch(
            "client.widget_viewer.urlopen",
            return_value=_FakeResponse(),
        ):
            result = widget_viewer._build_engine_url(widget_url=None, widget_config=widget_config)

        parsed = urlparse(result)
        encoded = parse_qs(parsed.query)["config_b64"][0]
        payload = json.loads(base64.urlsafe_b64decode(unquote(encoded)).decode("utf-8"))
        self.assertEqual(payload["widgets"][0]["type"], "iframe")

    def test_normalize_url_uses_http_for_localhost(self):
        self.assertEqual(widget_viewer._normalize_url("localhost:5080/panel"), "http://localhost:5080/panel")

    def test_build_engine_url_normalizes_iframe_widget_urls(self):
        with patch.dict("os.environ", {"WIDGET_SINGLE_ENGINE": "1"}, clear=False):
            result = widget_viewer._build_engine_url(
                widget_url=None,
                widget_config={"widgets": [{"type": "iframe", "url": "example.com/dashboard"}]},
            )

        parsed = urlparse(result)
        encoded = parse_qs(parsed.query)["config_b64"][0]
        payload = json.loads(base64.urlsafe_b64decode(unquote(encoded)).decode("utf-8"))
        self.assertEqual(payload["widgets"], [{"type": "iframe", "url": "https://example.com/dashboard"}])

    def test_build_engine_url_converts_html_widget_to_card(self):
        with patch.dict("os.environ", {"WIDGET_SINGLE_ENGINE": "1"}, clear=False):
            result = widget_viewer._build_engine_url(
                widget_url=None,
                widget_config={"widgets": [{"type": "html", "content": "<i>Selam</i>"}]},
            )

        parsed = urlparse(result)
        encoded = parse_qs(parsed.query)["config_b64"][0]
        payload = json.loads(base64.urlsafe_b64decode(unquote(encoded)).decode("utf-8"))
        self.assertEqual(payload["widgets"], [{"type": "card", "content": "<i>Selam</i>", "html": "<i>Selam</i>"}])

    def test_build_engine_url_uses_content_when_url_type_missing_url_field(self):
        with patch.dict("os.environ", {"WIDGET_SINGLE_ENGINE": "1"}, clear=False):
            result = widget_viewer._build_engine_url(
                widget_url=None,
                widget_config={"widgets": [{"type": "url", "content": "example.com/content-source"}]},
            )

        parsed = urlparse(result)
        encoded = parse_qs(parsed.query)["config_b64"][0]
        payload = json.loads(base64.urlsafe_b64decode(unquote(encoded)).decode("utf-8"))
        self.assertEqual(payload["widgets"], [{"type": "iframe", "content": "example.com/content-source", "url": "https://example.com/content-source"}])

    def test_build_engine_url_converts_url_widget_to_iframe(self):
        with patch.dict("os.environ", {"WIDGET_SINGLE_ENGINE": "1"}, clear=False):
            result = widget_viewer._build_engine_url(
                widget_url=None,
                widget_config={"widgets": [{"type": "url", "url": "example.com/page"}]},
            )

        parsed = urlparse(result)
        encoded = parse_qs(parsed.query)["config_b64"][0]
        payload = json.loads(base64.urlsafe_b64decode(unquote(encoded)).decode("utf-8"))
        self.assertEqual(payload["widgets"], [{"type": "iframe", "url": "https://example.com/page"}])

    def test_build_engine_url_treats_embed_html_as_embed_widget(self):
        embed_html = '<a href="https://example.com">Widget</a><script>window.__x=1;</script>'

        with patch.dict("os.environ", {"WIDGET_SINGLE_ENGINE": "1"}, clear=False):
            result = widget_viewer._build_engine_url(
                widget_url=None,
                widget_config={"widgets": [{"type": "iframe", "url": embed_html}]},
            )

        parsed = urlparse(result)
        encoded = parse_qs(parsed.query)["config_b64"][0]
        payload = json.loads(base64.urlsafe_b64decode(unquote(encoded)).decode("utf-8"))
        self.assertEqual(payload["widgets"], [{"type": "embed", "html": embed_html}])

    def test_build_engine_url_normalizes_dashboard_video_relative_media_path(self):
        with patch.dict(
            "os.environ",
            {"WIDGET_SINGLE_ENGINE": "1", "SERVER_URL": "http://panel.local:5080"},
            clear=False,
        ):
            result = widget_viewer._build_engine_url(
                widget_url=None,
                widget_config={"widgets": [{"type": "video", "url": "/media/video.mp4"}]},
            )

        parsed = urlparse(result)
        encoded = parse_qs(parsed.query)["config_b64"][0]
        payload = json.loads(base64.urlsafe_b64decode(unquote(encoded)).decode("utf-8"))
        self.assertEqual(payload["widgets"], [{"type": "video", "url": "http://panel.local:5080/media/video.mp4"}])

    def test_build_engine_url_preserves_dashboard_video_absolute_url(self):
        with patch.dict("os.environ", {"WIDGET_SINGLE_ENGINE": "1"}, clear=False):
            result = widget_viewer._build_engine_url(
                widget_url=None,
                widget_config={"widgets": [{"type": "video", "url": "https://cdn.example.com/video.mp4"}]},
            )

        parsed = urlparse(result)
        encoded = parse_qs(parsed.query)["config_b64"][0]
        payload = json.loads(base64.urlsafe_b64decode(unquote(encoded)).decode("utf-8"))
        self.assertEqual(payload["widgets"], [{"type": "video", "url": "https://cdn.example.com/video.mp4"}])

    def test_build_engine_url_converts_iframe_html_url_to_embed(self):
        html_doc = "<!doctype html><html><head><title>Dash</title></head><body><video src=\"/media/v.mp4\"></video></body></html>"

        class _FakeHeaders:
            @staticmethod
            def get_content_charset(_default=None):
                return "utf-8"

        class _FakeResponse:
            headers = _FakeHeaders()

            @staticmethod
            def read(_size=-1):
                return html_doc.encode("utf-8")

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

        with patch.dict("os.environ", {"WIDGET_SINGLE_ENGINE": "1"}, clear=False), patch(
            "client.widget_viewer.urlopen", return_value=_FakeResponse()
        ):
            result = widget_viewer._build_engine_url(
                widget_url=None,
                widget_config={"widgets": [{"type": "iframe", "url": "https://panel.local/dashboard.html"}]},
            )

        parsed = urlparse(result)
        encoded = parse_qs(parsed.query)["config_b64"][0]
        payload = json.loads(base64.urlsafe_b64decode(unquote(encoded)).decode("utf-8"))
        self.assertEqual(payload["widgets"][0]["type"], "embed")
        self.assertIn('<base href="https://panel.local/">', payload["widgets"][0]["html"])
        self.assertEqual(payload["widgets"][0]["source_url"], "https://panel.local/dashboard.html")

    def test_build_engine_url_keeps_iframe_when_inline_disabled(self):
        with patch.dict(
            "os.environ",
            {"WIDGET_SINGLE_ENGINE": "1", "WIDGET_INLINE_HTML_FROM_IFRAME": "0"},
            clear=False,
        ):
            result = widget_viewer._build_engine_url(
                widget_url=None,
                widget_config={"widgets": [{"type": "iframe", "url": "https://panel.local/dashboard.html"}]},
            )

        parsed = urlparse(result)
        encoded = parse_qs(parsed.query)["config_b64"][0]
        payload = json.loads(base64.urlsafe_b64decode(unquote(encoded)).decode("utf-8"))
        self.assertEqual(payload["widgets"], [{"type": "iframe", "url": "https://panel.local/dashboard.html"}])


if __name__ == "__main__":
    unittest.main()
