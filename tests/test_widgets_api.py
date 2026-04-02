import importlib
import os
import tempfile
import unittest
from unittest.mock import patch


class TestWidgetsApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmpdir = tempfile.TemporaryDirectory()
        os.environ["DATABASE_URL"] = f"sqlite:///{cls._tmpdir.name}/test_widgets.db"
        cls.main = importlib.import_module("app.main")

    @classmethod
    def tearDownClass(cls):
        cls._tmpdir.cleanup()

    def setUp(self):
        db = self.main.db_session()
        try:
            db.query(self.main.DeviceGroup).delete()
            db.query(self.main.GroupPlaylist).delete()
            db.query(self.main.PlaylistItem).delete()
            db.query(self.main.Playlist).delete()
            db.query(self.main.Device).delete()
            db.query(self.main.Group).delete()
            db.query(self.main.AppSetting).delete()
            db.commit()
        finally:
            db.close()

    def test_widget_crud(self):
        client = self.main.app.test_client()
        with patch("app.main._auth_failed", return_value=False):
            empty = client.get("/api/widgets")
            self.assertEqual(empty.status_code, 200)
            self.assertEqual(empty.get_json(), [])

            create = client.post("/api/widgets", json={"name": "Saat", "type": "html", "content": "<b>12:00</b>"})
            self.assertEqual(create.status_code, 200)
            wid = create.get_json().get("id")
            self.assertIsNotNone(wid)

            listed = client.get("/api/widgets")
            rows = listed.get_json() or []
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["name"], "Saat")

            update = client.patch(f"/api/widgets/{wid}", json={"name": "Hava", "type": "url", "content": "https://example.com"})
            self.assertEqual(update.status_code, 200)

            after_update = client.get("/api/widgets").get_json() or []
            self.assertEqual(after_update[0]["type"], "url")
            self.assertEqual(after_update[0]["content"], "https://example.com")

            delete = client.delete(f"/api/widgets/{wid}")
            self.assertEqual(delete.status_code, 200)
            self.assertEqual(client.get("/api/widgets").get_json(), [])

    def test_widget_name_reflected_in_playlist_item_label(self):
        client = self.main.app.test_client()
        with patch("app.main._auth_failed", return_value=False):
            create_widget = client.post(
                "/api/widgets",
                json={"name": "Hava Durumu", "type": "url", "content": "https://example.com/widget"},
            )
            self.assertEqual(create_widget.status_code, 200)
            wid = create_widget.get_json().get("id")

            create_playlist = client.post("/api/playlists", json={"name": "Widget Label Playlist", "enabled": True})
            self.assertEqual(create_playlist.status_code, 200)
            pid = create_playlist.get_json().get("id")

            add_item = client.post(
                f"/api/playlists/{pid}/items",
                json={"item_type": "widget", "widget_id": wid, "order_no": 0},
            )
            self.assertEqual(add_item.status_code, 200)

            items = client.get(f"/api/playlists/{pid}/items")
            self.assertEqual(items.status_code, 200)
            rows = items.get_json() or []
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["label"], "Hava Durumu")

    def test_deleting_widget_removes_widget_items_from_playlists(self):
        client = self.main.app.test_client()
        with patch("app.main._auth_failed", return_value=False):
            create_widget = client.post(
                "/api/widgets",
                json={"name": "Kampanya", "type": "html", "content": "<div>indirim</div>"},
            )
            self.assertEqual(create_widget.status_code, 200)
            wid = create_widget.get_json().get("id")

            create_playlist = client.post("/api/playlists", json={"name": "Widget Delete Playlist", "enabled": True})
            self.assertEqual(create_playlist.status_code, 200)
            pid = create_playlist.get_json().get("id")

            add_item = client.post(
                f"/api/playlists/{pid}/items",
                json={"item_type": "widget", "widget_id": wid, "order_no": 0},
            )
            self.assertEqual(add_item.status_code, 200)

            before_delete = client.get(f"/api/playlists/{pid}/items").get_json() or []
            self.assertEqual(len(before_delete), 1)

            with patch("app.main._emit_config_update"):
                delete_widget = client.delete(f"/api/widgets/{wid}")
            self.assertEqual(delete_widget.status_code, 200)

            after_delete = client.get(f"/api/playlists/{pid}/items").get_json() or []
            self.assertEqual(after_delete, [])

    def test_dashboard_widget_type_is_supported(self):
        client = self.main.app.test_client()
        dashboard_content = {
            "columns": 2,
            "widgets": [
                {"type": "iframe", "url": "https://example.com/a"},
                {"type": "iframe", "url": "https://example.com/b"},
            ],
        }

        with patch("app.main._auth_failed", return_value=False):
            create = client.post(
                "/api/widgets",
                json={"name": "Dashboard", "type": "dashboard", "content": self.main.json.dumps(dashboard_content)},
            )
            self.assertEqual(create.status_code, 200)
            wid = create.get_json().get("id")

            update = client.patch(
                f"/api/widgets/{wid}",
                json={"name": "Dashboard 2", "type": "dashboard", "content": self.main.json.dumps(dashboard_content)},
            )
            self.assertEqual(update.status_code, 200)

            widgets = client.get('/api/widgets').get_json() or []
            row = next((w for w in widgets if w.get('id') == wid), None)
            self.assertIsNotNone(row)
            self.assertEqual(row.get('type'), 'dashboard')

            cleanup = client.delete(f'/api/widgets/{wid}')
            self.assertEqual(cleanup.status_code, 200)

    def test_widget_playlist_item_accepts_duration_sec(self):
        client = self.main.app.test_client()
        with patch("app.main._auth_failed", return_value=False):
            create_widget = client.post(
                "/api/widgets",
                json={"name": "Süreli Widget", "type": "url", "content": "https://example.com/widget"},
            )
            self.assertEqual(create_widget.status_code, 200)
            wid = create_widget.get_json().get("id")

            create_playlist = client.post("/api/playlists", json={"name": "Widget Duration Playlist", "enabled": True})
            self.assertEqual(create_playlist.status_code, 200)
            pid = create_playlist.get_json().get("id")

            add_item = client.post(
                f"/api/playlists/{pid}/items",
                json={"item_type": "widget", "widget_id": wid, "order_no": 0, "duration_sec": 12},
            )
            self.assertEqual(add_item.status_code, 200)

            items = client.get(f"/api/playlists/{pid}/items")
            self.assertEqual(items.status_code, 200)
            rows = items.get_json() or []
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["duration_sec"], 12)

    def test_dashboard_frame_playlist_is_resolved_into_runtime_payload(self):
        client = self.main.app.test_client()
        with patch("app.main._auth_failed", return_value=False):
            create_group = client.post("/api/groups", json={"name": "Dashboard Playlist Group"})
            self.assertEqual(create_group.status_code, 200)
            gid = create_group.get_json().get("id")

            db = self.main.db_session()
            try:
                db.add(self.main.Device(hostname="screen-dashboard"))
                db.commit()
            finally:
                db.close()

            bind_group = client.post(f"/api/devices/screen-dashboard/group/{gid}")
            self.assertEqual(bind_group.status_code, 200)

            media_playlist = client.post("/api/playlists", json={"name": "Dashboard Frame Playlist", "enabled": True})
            self.assertEqual(media_playlist.status_code, 200)
            frame_playlist_id = media_playlist.get_json().get("id")
            db = self.main.db_session()
            try:
                media_asset = self.main.MediaAsset(
                    original_name="demo.jpg",
                    stored_name="demo.jpg",
                    relative_path="uploads/demo.jpg",
                )
                db.add(media_asset)
                db.commit()
                media_id = media_asset.id
            finally:
                db.close()
            media_item = client.post(
                f"/api/playlists/{frame_playlist_id}/items",
                json={"item_type": "media", "media_id": media_id, "duration_sec": 7},
            )
            self.assertEqual(media_item.status_code, 200)

            dashboard_payload = {
                "columns": 1,
                "rows": 1,
                "widgets": [{"type": "playlist", "playlist_id": frame_playlist_id}],
            }
            create_widget = client.post(
                "/api/widgets",
                json={"name": "Playlist Dashboard", "type": "dashboard", "content": self.main.json.dumps(dashboard_payload)},
            )
            self.assertEqual(create_widget.status_code, 200)
            dashboard_widget_id = create_widget.get_json().get("id")

            main_playlist = client.post("/api/playlists", json={"name": "Main Dashboard Playlist", "enabled": True})
            self.assertEqual(main_playlist.status_code, 200)
            main_playlist_id = main_playlist.get_json().get("id")
            widget_item = client.post(
                f"/api/playlists/{main_playlist_id}/items",
                json={"item_type": "widget", "widget_id": dashboard_widget_id},
            )
            self.assertEqual(widget_item.status_code, 200)

            bind_playlist = client.post(f"/api/groups/{gid}/playlist/{main_playlist_id}")
            self.assertEqual(bind_playlist.status_code, 200)

            config_payload = self.main.build_config("screen-dashboard")
            videos = (config_payload or {}).get("videos") or []
            self.assertEqual(len(videos), 1)
            widget_payload = videos[0].get("widget_payload") or {}
            widgets = widget_payload.get("widgets") or []
            self.assertEqual(len(widgets), 1)
            self.assertEqual(widgets[0].get("type"), "playlist")
            self.assertEqual(widgets[0].get("playlist_id"), frame_playlist_id)
            self.assertEqual(len(widgets[0].get("items") or []), 1)

    def test_widget_playlist_item_rejects_non_positive_duration_sec(self):
        client = self.main.app.test_client()
        with patch("app.main._auth_failed", return_value=False):
            create_widget = client.post(
                "/api/widgets",
                json={"name": "Geçersiz Süre Widget", "type": "url", "content": "https://example.com/widget"},
            )
            self.assertEqual(create_widget.status_code, 200)
            wid = create_widget.get_json().get("id")

            create_playlist = client.post("/api/playlists", json={"name": "Widget Invalid Duration Playlist", "enabled": True})
            self.assertEqual(create_playlist.status_code, 200)
            pid = create_playlist.get_json().get("id")

            add_item = client.post(
                f"/api/playlists/{pid}/items",
                json={"item_type": "widget", "widget_id": wid, "order_no": 0, "duration_sec": 0},
            )
            self.assertEqual(add_item.status_code, 400)
            self.assertEqual(add_item.get_json(), {"error": "duration_sec must be > 0"})

    def test_updating_widget_pushes_config_to_affected_devices(self):
        client = self.main.app.test_client()
        with patch("app.main._auth_failed", return_value=False):
            create_group = client.post("/api/groups", json={"name": "Widget Push Group"})
            self.assertEqual(create_group.status_code, 200)
            gid = create_group.get_json().get("id")

            db = self.main.db_session()
            try:
                db.add(self.main.Device(hostname="screen-1"))
                db.commit()
            finally:
                db.close()

            bind_group = client.post("/api/devices/screen-1/group/{}".format(gid))
            self.assertEqual(bind_group.status_code, 200)

            create_widget = client.post(
                "/api/widgets",
                json={"name": "Dashboard", "type": "url", "content": "https://example.com/one"},
            )
            self.assertEqual(create_widget.status_code, 200)
            wid = create_widget.get_json().get("id")

            create_playlist = client.post(
                "/api/playlists",
                json={"name": "Widget Push Playlist", "enabled": True},
            )
            self.assertEqual(create_playlist.status_code, 200)
            pid = create_playlist.get_json().get("id")

            bind_playlist = client.post(f"/api/groups/{gid}/playlist/{pid}")
            self.assertEqual(bind_playlist.status_code, 200)

            add_item = client.post(
                f"/api/playlists/{pid}/items",
                json={"item_type": "widget", "widget_id": wid, "order_no": 0},
            )
            self.assertEqual(add_item.status_code, 200)

            with patch("app.main._emit_config_update") as emit_config_update:
                update = client.patch(
                    f"/api/widgets/{wid}",
                    json={"name": "Dashboard Updated", "type": "url", "content": "https://example.com/two"},
                )

            self.assertEqual(update.status_code, 200)
            emit_config_update.assert_called_once()
            hostnames = emit_config_update.call_args.args[0]
            self.assertIn("screen-1", hostnames)


if __name__ == "__main__":
    unittest.main()
