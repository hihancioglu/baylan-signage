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
            self.assertEqual(widget_viewer._viewer_backend_order(), ["cef", "pywebview"])

    def test_build_engine_url_wraps_source_when_enabled(self):
        with patch.dict("os.environ", {"WIDGET_SINGLE_ENGINE": "1"}, clear=False):
            result = widget_viewer._build_engine_url("https://example.com")

        self.assertIn("widget_engine.html", result)
        self.assertIn("config_b64=", result)


    def test_build_engine_url_falls_back_when_meipass_resource_missing(self):
        with patch.dict('os.environ', {'WIDGET_SINGLE_ENGINE': '1'}, clear=False), patch(
            'client.widget_viewer.sys._MEIPASS', '/tmp/nonexistent-meipass', create=True
        ):
            result = widget_viewer._build_engine_url('https://example.com')

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

    def test_normalize_url_uses_file_uri_for_existing_local_file(self):
        with tempfile.NamedTemporaryFile(suffix=".html", delete=False) as handle:
            handle.write(b"<html></html>")
            local_path = handle.name

        try:
            self.assertEqual(widget_viewer._normalize_url(local_path), Path(local_path).resolve().as_uri())
        finally:
            Path(local_path).unlink(missing_ok=True)

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


if __name__ == "__main__":
    unittest.main()
