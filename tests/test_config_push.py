import importlib
import os
import tempfile
import unittest
from unittest.mock import patch


class TestConfigPush(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmpdir = tempfile.TemporaryDirectory()
        os.environ["DATABASE_URL"] = f"sqlite:///{cls._tmpdir.name}/test.db"
        cls.main = importlib.import_module("app.main")

    @classmethod
    def tearDownClass(cls):
        cls._tmpdir.cleanup()

    def test_emit_config_update_only_to_connected_unique_hosts(self):
        original_connected = dict(self.main.connected)
        self.main.connected.clear()
        self.main.connected.update({"pc-a": "sid-a", "pc-b": "sid-b"})

        try:
            with patch("app.main.build_config", side_effect=lambda host: {"hostname": host}) as mock_build:
                with patch("app.main.socketio.emit") as mock_emit:
                    self.main._emit_config_update(["pc-a", "pc-a", "pc-c", "pc-b"])

            self.assertEqual(mock_build.call_count, 2)
            mock_build.assert_any_call("pc-a")
            mock_build.assert_any_call("pc-b")

            self.assertEqual(mock_emit.call_count, 2)
            mock_emit.assert_any_call("config", {"hostname": "pc-a"}, room="device:pc-a")
            mock_emit.assert_any_call("config", {"hostname": "pc-b"}, room="device:pc-b")
        finally:
            self.main.connected.clear()
            self.main.connected.update(original_connected)

    def test_update_group_idle_timeout_emits_config_for_group_devices(self):
        db = self.main.db_session()
        try:
            group = self.main.Group(name="Lobby", idle_timeout_sec=30)
            device = self.main.Device(hostname="pc-lobby")
            db.add_all([group, device])
            db.commit()

            membership = self.main.DeviceGroup(device_id=device.id, group_id=group.id, is_active=True)
            db.add(membership)
            db.commit()
            group_id = group.id
        finally:
            db.close()

        with patch("app.main._auth_failed", return_value=False):
            with patch("app.main._emit_config_update") as mock_emit:
                resp = self.main.app.test_client().patch(
                    f"/api/groups/{group_id}",
                    json={"name": "Lobby", "idle_timeout_sec": 45},
                )

        self.assertEqual(resp.status_code, 200)
        mock_emit.assert_called_once_with(["pc-lobby"])

    def test_update_group_flags_emit_config_for_group_devices(self):
        db = self.main.db_session()
        try:
            group = self.main.Group(name="Store", idle_timeout_sec=30, idle_mode_enabled=True, content_enabled=True)
            device = self.main.Device(hostname="pc-store")
            db.add_all([group, device])
            db.commit()

            membership = self.main.DeviceGroup(device_id=device.id, group_id=group.id, is_active=True)
            db.add(membership)
            db.commit()
            group_id = group.id
        finally:
            db.close()

        with patch("app.main._auth_failed", return_value=False):
            with patch("app.main._emit_config_update") as mock_emit:
                resp = self.main.app.test_client().patch(
                    f"/api/groups/{group_id}",
                    json={"name": "Store", "idle_timeout_sec": 30, "idle_mode_enabled": False, "content_enabled": False},
                )

        self.assertEqual(resp.status_code, 200)
        mock_emit.assert_called_once_with(["pc-store"])

    def test_update_device_settings_emits_config(self):
        db = self.main.db_session()
        try:
            device = self.main.Device(hostname="pc-device-settings")
            db.add(device)
            db.commit()
        finally:
            db.close()

        with patch("app.main._auth_failed", return_value=False):
            with patch("app.main._emit_config_update") as mock_emit:
                resp = self.main.app.test_client().patch(
                    "/api/devices/pc-device-settings/settings",
                    json={"idle_mode_enabled": False, "content_enabled": False},
                )

        self.assertEqual(resp.status_code, 200)
        mock_emit.assert_called_once_with(["pc-device-settings"])



if __name__ == "__main__":
    unittest.main()
