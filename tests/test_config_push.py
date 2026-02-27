import importlib
import io
import os
import tempfile
from pathlib import Path
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

    def test_update_device_settings_is_disabled(self):
        with patch("app.main._auth_failed", return_value=False):
            resp = self.main.app.test_client().patch(
                "/api/devices/pc-device-settings/settings",
                json={"idle_mode_enabled": False, "content_enabled": False},
            )

        self.assertEqual(resp.status_code, 410)
        self.assertEqual(resp.get_json().get("error"), "device-level settings are disabled; use group settings")

    def test_build_config_uses_group_flags_without_device_override(self):
        db = self.main.db_session()
        try:
            group = self.main.Group(name="HQ", idle_mode_enabled=False, content_enabled=False)
            playlist = self.main.Playlist(name="HQ Playlist", enabled=True)
            db.add_all([group, playlist])
            db.commit()

            item = self.main.PlaylistItem(
                playlist_id=playlist.id,
                path="https://example.com/video.mp4",
                media_type="video",
                duration_sec=15,
                order_no=1,
            )
            device = self.main.Device(hostname="pc-hq", idle_mode_enabled=True, content_enabled=True)
            db.add_all([item, device])
            db.commit()

            db.add(self.main.DeviceGroup(device_id=device.id, group_id=group.id, is_active=True))
            db.add(self.main.GroupPlaylist(group_id=group.id, playlist_id=playlist.id))
            db.commit()
        finally:
            db.close()

        cfg = self.main.build_config("pc-hq")
        self.assertEqual(cfg["idle_mode_enabled"], False)
        self.assertEqual(cfg["content_enabled"], False)

    def test_devices_api_includes_agent_version(self):
        db = self.main.db_session()
        try:
            db.add(self.main.Device(hostname="pc-ver", agent_version="build-20260101120000", is_online=True))
            db.commit()
        finally:
            db.close()

        with patch("app.main._auth_failed", return_value=False):
            resp = self.main.app.test_client().get("/api/devices")

        self.assertEqual(resp.status_code, 200)
        devices = resp.get_json() or []
        target = next((d for d in devices if d.get("hostname") == "pc-ver"), None)
        self.assertIsNotNone(target)
        self.assertEqual(target.get("agent_version"), "build-20260101120000")


    def test_resolve_update_version_prefers_embedded_build_marker(self):
        marker = b"BAYLAN_CLIENT_BUILD:build-20260227091530"
        temp_file = Path(self._tmpdir.name) / "embedded-version.bin"
        temp_file.write_bytes(b"prefix" + marker + b"suffix")

        version = self.main._resolve_update_version("", "client.exe", temp_file)
        self.assertEqual(version, "build-20260227091530")

    def test_upload_client_update_detects_embedded_build_version(self):
        client = self.main.app.test_client()
        payload = b"abcBAYLAN_CLIENT_BUILD:build-20260227093045xyz"

        with patch("app.main._auth_failed", return_value=False):
            resp = client.post(
                "/api/updater/upload",
                data={"file": (io.BytesIO(payload), "client-update.exe")},
                content_type="multipart/form-data",
            )

        self.assertEqual(resp.status_code, 200)
        body = resp.get_json() or {}
        self.assertTrue(body.get("ok"))
        release = body.get("release") or {}
        self.assertEqual(release.get("version"), "build-20260227093045")


if __name__ == "__main__":
    unittest.main()
