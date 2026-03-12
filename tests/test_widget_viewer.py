import unittest
from unittest.mock import patch

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
            widget_viewer._start_with_fallback(fake_webview, "https://example.com")

        self.assertEqual(fake_webview.start.call_count, 2)
        self.assertEqual(fake_webview.start.call_args_list[0].kwargs["gui"], "edgechromium")
        self.assertEqual(fake_webview.start.call_args_list[1].kwargs["gui"], "qt")


if __name__ == "__main__":
    unittest.main()
