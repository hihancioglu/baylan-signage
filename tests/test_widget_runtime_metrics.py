import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from client.client import _widget_runtime_telemetry


class TestWidgetRuntimeTelemetry(unittest.TestCase):
    def test_sums_viewer_and_webview2_descendant_rss(self):
        webview_a = Mock(pid=21)
        webview_a.name.return_value = "msedgewebview2.exe"
        webview_a.memory_info.return_value = SimpleNamespace(rss=100 * 1024 * 1024)
        webview_b = Mock(pid=22)
        webview_b.name.return_value = "msedgewebview2.exe"
        webview_b.memory_info.return_value = SimpleNamespace(rss=50 * 1024 * 1024)
        unrelated = Mock(pid=23)
        unrelated.name.return_value = "other.exe"

        viewer = Mock(pid=10)
        viewer.info = {
            "pid": 10,
            "cmdline": ["agent.exe", "--runtime-ipc", "--baylan-widget-runtime"],
        }
        viewer.memory_info.return_value = SimpleNamespace(rss=40 * 1024 * 1024)
        viewer.children.return_value = [webview_a, webview_b, unrelated]
        psutil_module = Mock()
        psutil_module.process_iter.return_value = [viewer]

        with patch("client.client.platform.system", return_value="Windows"):
            telemetry = _widget_runtime_telemetry(psutil_module)

        self.assertEqual(
            telemetry,
            {
                "viewer_process_count": 1,
                "viewer_pids": [10],
                "webview2_process_count": 2,
                "viewer_ram_mb": 40.0,
                "webview2_ram_mb": 150.0,
                "total_ram_mb": 190.0,
            },
        )


if __name__ == "__main__":
    unittest.main()
