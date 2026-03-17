import importlib
import io
import os
import tempfile
from datetime import datetime, timedelta
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


    def test_build_command_contract_generates_unique_command_ids(self):
        first = self.main.build_command_contract("CAPTURE_SCREEN")
        second = self.main.build_command_contract("CAPTURE_SCREEN")

        self.assertNotEqual(first["command_id"], second["command_id"])
        self.assertTrue(first["command_id"].startswith("cmd-"))
        self.assertTrue(second["command_id"].startswith("cmd-"))

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

    def test_build_config_playlist_version_changes_when_order_changes(self):
        db = self.main.db_session()
        try:
            group = self.main.Group(name="Order Group")
            playlist = self.main.Playlist(name="Order Playlist", enabled=True, loop_mode="sequential")
            device = self.main.Device(hostname="pc-order")
            db.add_all([group, playlist, device])
            db.commit()

            db.add(self.main.DeviceGroup(device_id=device.id, group_id=group.id, is_active=True))
            db.add(self.main.GroupPlaylist(group_id=group.id, playlist_id=playlist.id))
            db.add_all(
                [
                    self.main.PlaylistItem(
                        playlist_id=playlist.id,
                        path="https://example.com/a.mp4",
                        media_type="video",
                        order_no=0,
                    ),
                    self.main.PlaylistItem(
                        playlist_id=playlist.id,
                        path="https://example.com/b.mp4",
                        media_type="video",
                        order_no=1,
                    ),
                ]
            )
            db.commit()

            first_version = self.main.build_config("pc-order")["playlist_version"]

            first_item = (
                db.query(self.main.PlaylistItem)
                .filter_by(playlist_id=playlist.id, path="https://example.com/a.mp4")
                .first()
            )
            second_item = (
                db.query(self.main.PlaylistItem)
                .filter_by(playlist_id=playlist.id, path="https://example.com/b.mp4")
                .first()
            )
            first_item.order_no = 1
            second_item.order_no = 0
            db.commit()

            second_version = self.main.build_config("pc-order")["playlist_version"]
        finally:
            db.close()

        self.assertNotEqual(first_version, second_version)

    def test_build_config_widget_item_reflects_updated_widget_definition(self):
        db = self.main.db_session()
        try:
            group = self.main.Group(name="Widget Group")
            playlist = self.main.Playlist(name="Widget Playlist", enabled=True, loop_mode="sequential")
            device = self.main.Device(hostname="pc-widget")
            db.add_all([group, playlist, device])
            db.commit()

            db.add(self.main.DeviceGroup(device_id=device.id, group_id=group.id, is_active=True))
            db.add(self.main.GroupPlaylist(group_id=group.id, playlist_id=playlist.id))
            db.add(
                self.main.PlaylistItem(
                    playlist_id=playlist.id,
                    item_type="widget",
                    media_type="widget",
                    widget_id=7,
                    widget_payload='{"name":"Eski","type":"html","content":"<div>eski</div>"}',
                    order_no=0,
                )
            )
            db.commit()

            self.main._save_widgets(
                db,
                [
                    {
                        "id": 7,
                        "name": "Yeni Widget",
                        "type": "html",
                        "content": "<div>yeni</div>",
                    }
                ],
            )
            db.commit()
        finally:
            db.close()

        cfg = self.main.build_config("pc-widget")
        self.assertEqual(cfg["videos"][0]["display_name"], "Yeni Widget")
        self.assertEqual(cfg["videos"][0]["widget_payload"]["name"], "Yeni Widget")
        self.assertEqual(cfg["videos"][0]["widget_payload"]["content"], "<div>yeni</div>")
        self.assertIsNone(cfg["videos"][0]["widget_url"])

    def test_build_config_media_item_uses_uploaded_original_name_for_display(self):
        db = self.main.db_session()
        try:
            group = self.main.Group(name="Media Group")
            playlist = self.main.Playlist(name="Media Playlist", enabled=True, loop_mode="sequential")
            device = self.main.Device(hostname="pc-media")
            db.add_all([group, playlist, device])
            db.commit()

            db.add(self.main.DeviceGroup(device_id=device.id, group_id=group.id, is_active=True))
            db.add(self.main.GroupPlaylist(group_id=group.id, playlist_id=playlist.id))
            db.add(
                self.main.MediaAsset(
                    original_name="Dokumanhane Tanitim.mp4",
                    stored_name="41c6520085cd25ee7bd1.mp4",
                    relative_path="media/41c6520085cd25ee7bd1.mp4",
                    content_type="video/mp4",
                    file_size=123,
                    checksum="abc123",
                )
            )
            db.add(
                self.main.PlaylistItem(
                    playlist_id=playlist.id,
                    item_type="media",
                    media_type="video",
                    path="media/41c6520085cd25ee7bd1.mp4",
                    order_no=0,
                )
            )
            db.commit()
        finally:
            db.close()

        cfg = self.main.build_config("pc-media")
        self.assertEqual(cfg["videos"][0]["display_name"], "Dokumanhane Tanitim.mp4")

    def test_build_config_widget_payload_string_is_decoded_for_html_widget(self):
        db = self.main.db_session()
        try:
            group = self.main.Group(name="Widget Group Decode")
            playlist = self.main.Playlist(name="Widget Playlist Decode", enabled=True, loop_mode="sequential")
            device = self.main.Device(hostname="pc-widget-decode")
            db.add_all([group, playlist, device])
            db.commit()

            db.add(self.main.DeviceGroup(device_id=device.id, group_id=group.id, is_active=True))
            db.add(self.main.GroupPlaylist(group_id=group.id, playlist_id=playlist.id))
            db.add(
                self.main.PlaylistItem(
                    playlist_id=playlist.id,
                    item_type="widget",
                    media_type="widget",
                    widget_payload='{"type":"html","content":"<b>merhaba</b>"}',
                    order_no=0,
                )
            )
            db.commit()
        finally:
            db.close()

        cfg = self.main.build_config("pc-widget-decode")
        self.assertEqual(cfg["videos"][0]["widget_payload"], {"type": "html", "content": "<b>merhaba</b>"})


    def test_build_config_dashboard_widget_resolves_iframe_layout(self):
        db = self.main.db_session()
        try:
            group = self.main.Group(name="Widget Dashboard Group")
            playlist = self.main.Playlist(name="Widget Dashboard Playlist", enabled=True, loop_mode="sequential")
            device = self.main.Device(hostname="pc-widget-dashboard")
            db.add_all([group, playlist, device])
            db.commit()

            db.add(self.main.DeviceGroup(device_id=device.id, group_id=group.id, is_active=True))
            db.add(self.main.GroupPlaylist(group_id=group.id, playlist_id=playlist.id))
            db.add(
                self.main.PlaylistItem(
                    playlist_id=playlist.id,
                    item_type="widget",
                    media_type="widget",
                    widget_id=8,
                    order_no=0,
                )
            )
            db.commit()

            self.main._save_widgets(
                db,
                [
                    {
                        "id": 8,
                        "name": "Dashboard Widget",
                        "type": "dashboard",
                        "content": '{"columns":2,"widgets":[{"type":"iframe","url":"https://example.com/a"},{"type":"iframe","url":"https://example.com/b"}]}',
                    }
                ],
            )
            db.commit()
        finally:
            db.close()

        cfg = self.main.build_config("pc-widget-dashboard")
        payload = cfg["videos"][0]["widget_payload"]
        self.assertEqual(payload["name"], "Dashboard Widget")
        self.assertEqual(payload["widgets"][0]["url"], "https://example.com/a")
        self.assertEqual(payload["widgets"][1]["url"], "https://example.com/b")
        self.assertEqual(payload["columns"], 2)
        self.assertIsNone(cfg["videos"][0]["widget_url"])


    def test_build_config_dashboard_widget_preserves_empty_cells_and_rows(self):
        db = self.main.db_session()
        try:
            group = self.main.Group(name="Widget Dashboard Grid Group")
            playlist = self.main.Playlist(name="Widget Dashboard Grid Playlist", enabled=True, loop_mode="sequential")
            device = self.main.Device(hostname="pc-widget-dashboard-grid")
            db.add_all([group, playlist, device])
            db.commit()

            db.add(self.main.DeviceGroup(device_id=device.id, group_id=group.id, is_active=True))
            db.add(self.main.GroupPlaylist(group_id=group.id, playlist_id=playlist.id))
            db.add(
                self.main.PlaylistItem(
                    playlist_id=playlist.id,
                    item_type="widget",
                    media_type="widget",
                    widget_id=18,
                    order_no=0,
                )
            )
            db.commit()

            self.main._save_widgets(
                db,
                [
                    {
                        "id": 18,
                        "name": "Dashboard Widget Grid",
                        "type": "dashboard",
                        "content": '{"columns":2,"rows":2,"widgets":[{"type":"iframe","url":"https://example.com/a"},{"type":"empty"},{"type":"iframe","url":""},{"type":"iframe","url":"https://example.com/d"}]}',
                    }
                ],
            )
            db.commit()
        finally:
            db.close()

        cfg = self.main.build_config("pc-widget-dashboard-grid")
        payload = cfg["videos"][0]["widget_payload"]
        self.assertEqual(payload["columns"], 2)
        self.assertEqual(payload["rows"], 2)
        self.assertEqual([w.get("type") for w in payload["widgets"]], ["iframe", "empty", "empty", "iframe"])

    def test_build_config_dashboard_widget_accepts_string_url_entries(self):
        db = self.main.db_session()
        try:
            group = self.main.Group(name="Widget Dashboard String Group")
            playlist = self.main.Playlist(name="Widget Dashboard String Playlist", enabled=True, loop_mode="sequential")
            device = self.main.Device(hostname="pc-widget-dashboard-string")
            db.add_all([group, playlist, device])
            db.commit()

            db.add(self.main.DeviceGroup(device_id=device.id, group_id=group.id, is_active=True))
            db.add(self.main.GroupPlaylist(group_id=group.id, playlist_id=playlist.id))
            db.add(
                self.main.PlaylistItem(
                    playlist_id=playlist.id,
                    item_type="widget",
                    media_type="widget",
                    widget_id=19,
                    order_no=0,
                )
            )
            db.commit()

            self.main._save_widgets(
                db,
                [
                    {
                        "id": 19,
                        "name": "Dashboard Widget String",
                        "type": "dashboard",
                        "content": '{"columns":3,"widgets":["https://example.com/a","https://example.com/b","https://example.com/c"]}',
                    }
                ],
            )
            db.commit()
        finally:
            db.close()

        cfg = self.main.build_config("pc-widget-dashboard-string")
        payload = cfg["videos"][0]["widget_payload"]
        self.assertEqual(payload["columns"], 3)
        self.assertEqual([w.get("type") for w in payload["widgets"]], ["iframe", "iframe", "iframe"])
        self.assertEqual(
            [w.get("url") for w in payload["widgets"]],
            ["https://example.com/a", "https://example.com/b", "https://example.com/c"],
        )

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



    def test_register_and_heartbeat_store_updater_version(self):
        self.main.connected.clear()
        self.main.sid_to_host.clear()

        client_socket = self.main.socketio.test_client(self.main.app)
        self.assertTrue(client_socket.is_connected())

        client_socket.emit(
            "register",
            {
                "secret": self.main.SHARED_SECRET,
                "hostname": "pc-updater",
                "ip": "127.0.0.1",
                "agent_version": "build-client-1",
                "updater_version": "build-updater-1",
                "client_update_status": "ok:build-client-1",
                "client_updater_status": "ok:build-updater-1",
            },
        )

        client_socket.emit(
            "heartbeat",
            {
                "hostname": "pc-updater",
                "state": "IDLE",
                "agent_version": "build-client-2",
                "updater_version": "build-updater-2",
                "client_update_status": "failed:build-client-2:update_size_mismatch",
                "client_updater_status": "failed:build-updater-2:update_size_mismatch",
            },
        )
        client_socket.disconnect()

        db = self.main.db_session()
        try:
            device = db.query(self.main.Device).filter_by(hostname="pc-updater").first()
            self.assertIsNotNone(device)
            self.assertEqual(device.agent_version, "build-client-2")
            self.assertEqual(device.updater_version, "build-updater-2")
            self.assertEqual(device.last_client_update_status, "failed:build-client-2:update_size_mismatch")
            self.assertEqual(device.last_client_updater_status, "failed:build-updater-2:update_size_mismatch")
        finally:
            db.close()

    def test_client_updater_rollout_uses_updater_version_field(self):
        db = self.main.db_session()
        try:
            db.add_all(
                [
                    self.main.Device(hostname="pc-a", agent_version="build-client-old", updater_version="build-upd-new", is_online=True),
                    self.main.Device(hostname="pc-b", agent_version="build-client-new", updater_version="build-upd-old", is_online=True),
                ]
            )
            self.main._set_setting(db, "client_updater_version", "build-upd-new")
            self.main._set_setting(db, "client_updater_file_name", "BaylanUpdater.exe")
            self.main._set_setting(db, "client_updater_file_path", "client-updater/build-upd-new/BaylanUpdater.exe")
            db.commit()
        finally:
            db.close()

        with patch("app.main._auth_failed", return_value=False):
            resp = self.main.app.test_client().get("/api/client-updater")

        self.assertEqual(resp.status_code, 200)
        rollout = (resp.get_json() or {}).get("rollout") or {}
        clients = {item.get("hostname"): item for item in rollout.get("clients") or []}

        self.assertTrue(clients["pc-a"].get("is_updated"))
        self.assertFalse(clients["pc-b"].get("is_updated"))
        self.assertIn("last_client_updater_status", clients["pc-a"])

    def test_rollout_waiting_seconds_reports_remaining_slot_time(self):
        db = self.main.db_session()
        try:
            db.add(self.main.Device(hostname="pc-remaining", agent_version="build-old", is_online=True))
            db.commit()

            release = {
                "version": "build-new",
                "published_at": (datetime.utcnow() - timedelta(seconds=30)).isoformat(),
            }

            with patch("app.main._rollout_delay_seconds", return_value=120):
                rollout = self.main._build_updater_rollout_payload(db, release, channel="client")
        finally:
            db.close()

        clients = rollout.get("clients") or []
        self.assertEqual(len(clients), 1)
        waiting_seconds = clients[0].get("waiting_seconds")
        self.assertIsNotNone(waiting_seconds)
        self.assertGreaterEqual(waiting_seconds, 80)
        self.assertLessEqual(waiting_seconds, 90)

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

    def test_upload_client_updater_detects_embedded_updater_build_version(self):
        client = self.main.app.test_client()
        payload = b"abcBAYLAN_UPDATER_BUILD:build-20260303110657xyz"

        with patch("app.main._auth_failed", return_value=False):
            resp = client.post(
                "/api/client-updater/upload",
                data={"file": (io.BytesIO(payload), "updater-release.exe")},
                content_type="multipart/form-data",
            )

        self.assertEqual(resp.status_code, 200)
        body = resp.get_json() or {}
        self.assertTrue(body.get("ok"))
        release = body.get("release") or {}
        self.assertEqual(release.get("version"), "build-20260303110657")

    def test_playlist_items_api_returns_original_media_name_as_label(self):
        db = self.main.db_session()
        try:
            playlist = self.main.Playlist(name="Real Name Playlist", enabled=True)
            db.add(playlist)
            db.commit()
            playlist_id = playlist.id

            asset = self.main.MediaAsset(
                original_name="Kampanya Videosu Final.mp4",
                stored_name="abc123.mp4",
                relative_path="abc123.mp4",
                content_type="video/mp4",
            )
            db.add(asset)
            db.commit()

            item = self.main.PlaylistItem(
                playlist_id=playlist_id,
                path="http://localhost/media/abc123.mp4",
                media_type="video",
                order_no=1,
            )
            db.add(item)
            db.commit()
        finally:
            db.close()

        with patch("app.main._auth_failed", return_value=False):
            resp = self.main.app.test_client().get(f"/api/playlists/{playlist_id}/items")

        self.assertEqual(resp.status_code, 200)
        items = resp.get_json() or []
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].get("label"), "Kampanya Videosu Final.mp4")

    def test_build_config_includes_active_announcement_for_target_device(self):
        db = self.main.db_session()
        try:
            group = self.main.Group(name="Ann Group")
            device = self.main.Device(hostname="pc-ann")
            db.add_all([group, device])
            db.commit()

            db.add(self.main.DeviceGroup(device_id=device.id, group_id=group.id, is_active=True))
            db.add(
                self.main.Announcement(
                    title="Duyuru",
                    message="Bakım bildirimi",
                    target_type="group",
                    target_value=str(group.id),
                    ttl_sec=120,
                    is_active=True,
                    published_at=datetime.utcnow(),
                )
            )
            db.commit()
        finally:
            db.close()

        cfg = self.main.build_config("pc-ann")
        self.assertTrue(cfg.get("announcement_active"))
        self.assertEqual(cfg.get("announcement_message"), "Bakım bildirimi")

    def test_publish_announcement_sets_active_and_emits_config_update(self):
        db = self.main.db_session()
        try:
            group = self.main.Group(name="Ann Pub Group")
            device = self.main.Device(hostname="pc-ann-pub")
            announcement = self.main.Announcement(
                title="Duyuru",
                message="Yayınlandı",
                target_type="group",
                target_value="0",
                ttl_sec=120,
                is_active=False,
            )
            db.add_all([group, device, announcement])
            db.commit()

            announcement.target_value = str(group.id)
            db.add(self.main.DeviceGroup(device_id=device.id, group_id=group.id, is_active=True))
            db.commit()
            announcement_id = announcement.id
        finally:
            db.close()

        with patch("app.main._auth_failed", return_value=False):
            with patch("app.main._emit_config_update") as mock_emit_config:
                with patch("app.main._emit_command") as mock_emit_command:
                    resp = self.main.app.test_client().post(f"/api/announcements/{announcement_id}/publish")

        self.assertEqual(resp.status_code, 200)
        self.assertFalse(mock_emit_command.called)
        mock_emit_config.assert_called_once_with(["pc-ann-pub"])

    def test_unpublish_announcement_sets_passive_and_emits_config_update(self):
        db = self.main.db_session()
        try:
            group = self.main.Group(name="Ann Unpub Group")
            device = self.main.Device(hostname="pc-ann-unpub")
            announcement = self.main.Announcement(
                title="Duyuru",
                message="Yayından kaldırıldı",
                target_type="group",
                target_value="0",
                ttl_sec=120,
                is_active=True,
                published_at=datetime.utcnow(),
            )
            db.add_all([group, device, announcement])
            db.commit()

            announcement.target_value = str(group.id)
            db.add(self.main.DeviceGroup(device_id=device.id, group_id=group.id, is_active=True))
            db.commit()
            announcement_id = announcement.id
        finally:
            db.close()

        with patch("app.main._auth_failed", return_value=False):
            with patch("app.main._emit_config_update") as mock_emit_config:
                with patch("app.main._emit_command") as mock_emit_command:
                    resp = self.main.app.test_client().post(f"/api/announcements/{announcement_id}/unpublish")

        self.assertEqual(resp.status_code, 200)
        self.assertFalse(mock_emit_command.called)
        mock_emit_config.assert_called_once_with(["pc-ann-unpub"])

    def test_list_devices_resolves_url_encoded_content_name_to_original_name(self):
        db = self.main.db_session()
        try:
            device = self.main.Device(
                hostname="pc-encoded",
                is_online=True,
                last_state="IDLE",
                last_content_name="http://panel.local/media/Kampanya%20Videosu%20Final.mp4",
            )
            asset = self.main.MediaAsset(
                original_name="Kampanya Videosu Final.mp4",
                stored_name="abc123.mp4",
                relative_path="Kampanya Videosu Final.mp4",
                content_type="video/mp4",
            )
            db.add_all([device, asset])
            db.commit()
        finally:
            db.close()

        with patch("app.main._auth_failed", return_value=False):
            resp = self.main.app.test_client().get("/api/devices")

        self.assertEqual(resp.status_code, 200)
        devices = {row.get("hostname"): row for row in (resp.get_json() or [])}
        self.assertEqual(devices["pc-encoded"].get("last_content_display_name"), "Kampanya Videosu Final.mp4")

    def test_list_devices_resolves_relative_media_path_to_original_name(self):
        db = self.main.db_session()
        try:
            device = self.main.Device(
                hostname="pc-relative-path",
                is_online=True,
                last_state="IDLE",
                last_content_name="media/946ee45fc71368270f5b.mp4",
            )
            asset = self.main.MediaAsset(
                original_name="MacaHane Tanitim.mp4",
                stored_name="946ee45fc71368270f5b.mp4",
                relative_path="media/946ee45fc71368270f5b.mp4",
                content_type="video/mp4",
            )
            db.add_all([device, asset])
            db.commit()
        finally:
            db.close()

        with patch("app.main._auth_failed", return_value=False):
            resp = self.main.app.test_client().get("/api/devices")

        self.assertEqual(resp.status_code, 200)
        devices = {row.get("hostname"): row for row in (resp.get_json() or [])}
        self.assertEqual(devices["pc-relative-path"].get("last_content_display_name"), "MacaHane Tanitim.mp4")

    def test_build_config_resolves_encoded_playlist_path_display_name(self):
        db = self.main.db_session()
        try:
            group = self.main.Group(name="Encoded Path Group")
            playlist = self.main.Playlist(name="Encoded Path Playlist", enabled=True, loop_mode="sequential")
            device = self.main.Device(hostname="pc-encoded-path")
            db.add_all([group, playlist, device])
            db.commit()

            db.add(self.main.DeviceGroup(device_id=device.id, group_id=group.id, is_active=True))
            db.add(self.main.GroupPlaylist(group_id=group.id, playlist_id=playlist.id))
            db.add(
                self.main.MediaAsset(
                    original_name="Ağır Sanayi Sunumu.mp4",
                    stored_name="stored-video.mp4",
                    relative_path="media/Ağır Sanayi Sunumu.mp4",
                    content_type="video/mp4",
                )
            )
            db.add(
                self.main.PlaylistItem(
                    playlist_id=playlist.id,
                    item_type="media",
                    media_type="video",
                    path="media/A%C4%9F%C4%B1r%20Sanayi%20Sunumu.mp4",
                    order_no=0,
                )
            )
            db.commit()
        finally:
            db.close()

        cfg = self.main.build_config("pc-encoded-path")
        self.assertEqual(cfg["videos"][0]["display_name"], "Ağır Sanayi Sunumu.mp4")


if __name__ == "__main__":
    unittest.main()
