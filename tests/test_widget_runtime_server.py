import importlib
import os
import tempfile
import unittest
from unittest.mock import patch


class TestWidgetRuntimeServer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmpdir = tempfile.TemporaryDirectory()
        os.environ.setdefault("DATABASE_URL", f"sqlite:///{cls._tmpdir.name}/test.db")
        cls.main = importlib.import_module("app.main")

    def setUp(self):
        self.main.LATEST_WIDGET_RUNTIME.clear()

    def test_heartbeat_exposes_normalized_widget_runtime_in_devices_api(self):
        hostname = "SIGNAGE-WIDGET-RUNTIME"
        mac_address = "AA:BB:CC:DD:EE:FF"
        runtime = {
            "runtime_instance_count": 1,
            "viewer_process_count": 1,
            "viewer_pids": [1111],
            "webview2_process_count": 7,
            "viewer_ram_mb": 40.2,
            "webview2_ram_mb": 300.5,
            "total_ram_mb": 340.7,
            "ignored": "not exposed",
        }
        client = self.main.socketio.test_client(self.main.app)
        try:
            client.emit("register", {
                "secret": self.main.SHARED_SECRET,
                "hostname": hostname,
                "mac_address": mac_address,
            })
            client.emit("heartbeat", {
                "hostname": hostname,
                "mac_address": mac_address,
                "widget_runtime": runtime,
            })

            with patch("app.main._auth_failed", return_value=False), patch(
                "app.main._idle_minutes_since_last_state", return_value=None
            ):
                response = self.main.app.test_client().get("/api/devices")

            self.assertEqual(response.status_code, 200)
            device = next(row for row in response.get_json() if row["hostname"] == hostname)
            self.assertEqual(device["widget_runtime"]["viewer_process_count"], 1)
            self.assertEqual(device["widget_runtime"]["runtime_instance_count"], 1)
            self.assertEqual(device["widget_runtime"]["webview2_process_count"], 7)
            self.assertEqual(device["widget_runtime"]["total_ram_mb"], 340.7)
            self.assertNotIn("ignored", device["widget_runtime"])
        finally:
            client.disconnect()

    def test_invalid_widget_runtime_is_ignored_without_breaking_heartbeat(self):
        invalid_payloads = [
            "invalid",
            {
                "viewer_process_count": -5,
                "viewer_pids": [1111],
                "webview2_process_count": 7,
                "viewer_ram_mb": 40.2,
                "webview2_ram_mb": 300.5,
                "total_ram_mb": 340.7,
            },
            {
                "viewer_process_count": 1,
                "viewer_pids": ["invalid"],
                "webview2_process_count": 7,
                "viewer_ram_mb": 40.2,
                "webview2_ram_mb": 300.5,
                "total_ram_mb": 340.7,
            },
        ]
        hostname = "SIGNAGE-INVALID-RUNTIME"
        client = self.main.socketio.test_client(self.main.app)
        try:
            client.emit("register", {"secret": self.main.SHARED_SECRET, "hostname": hostname})
            for payload in invalid_payloads:
                client.emit("heartbeat", {"hostname": hostname, "widget_runtime": payload})

            self.assertNotIn(hostname, self.main.LATEST_WIDGET_RUNTIME)
            db = self.main.db_session()
            try:
                device = db.query(self.main.Device).filter_by(hostname=hostname).first()
                self.assertIsNotNone(device)
                self.assertTrue(device.is_online)
            finally:
                db.close()
        finally:
            client.disconnect()

    def test_widget_runtime_normalizer_rejects_non_finite_ram(self):
        payload = {
            "viewer_process_count": 1,
            "viewer_pids": [1111],
            "webview2_process_count": 7,
            "viewer_ram_mb": float("nan"),
            "webview2_ram_mb": 300.5,
            "total_ram_mb": 340.7,
        }

        self.assertIsNone(self.main._extract_widget_runtime(payload))

    def test_widget_runtime_normalizer_accepts_legacy_payload_without_instance_count(self):
        payload = {
            "viewer_process_count": 2,
            "viewer_pids": [1111, 2222],
            "webview2_process_count": 6,
            "viewer_ram_mb": 50.0,
            "webview2_ram_mb": 250.0,
            "total_ram_mb": 300.0,
        }

        normalized = self.main._extract_widget_runtime(payload)

        self.assertIsNotNone(normalized)
        self.assertNotIn("runtime_instance_count", normalized)


if __name__ == "__main__":
    unittest.main()
