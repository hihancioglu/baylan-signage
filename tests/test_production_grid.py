import importlib
import json
import os
import tempfile
import unittest
from urllib.parse import parse_qs, urlparse
from unittest.mock import patch


class TestProductionGrid(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmpdir = tempfile.TemporaryDirectory()
        os.environ["DATABASE_URL"] = f"sqlite:///{cls._tmpdir.name}/production_grid.db"
        cls.main = importlib.import_module("app.main")

    def _payload(self, inventory_id, **overrides):
        config = {
            "base_url": "https://hub.baylan.info.tr/automation/production-widget",
            "theme": "light",
            "refresh_interval": 30,
            "scale": None,
            **overrides,
        }
        device = self.main.Device(hostname="production-screen", inventory_id=inventory_id)
        return self.main._build_production_grid_payload(device, config)

    def test_expected_dimensions_and_inventory_order(self):
        scenarios = [
            ("576", (1, 1), ["576"]),
            ("576,577", (2, 1), ["576", "577"]),
            ("576,577,581,590", (2, 2), ["576", "577", "581", "590"]),
            ("576,577,581,590,602", (3, 2), ["576", "577", "581", "590", "602"]),
        ]
        for inventory_id, dimensions, aliases in scenarios:
            with self.subTest(inventory_id=inventory_id):
                payload = self._payload(inventory_id)
                self.assertEqual((payload["columns"], payload["rows"]), dimensions)
                self.assertEqual(len(payload["widgets"]), len(aliases))
                self.assertEqual(
                    [parse_qs(urlparse(widget["url"]).query)["deviceAlias"][0] for widget in payload["widgets"]],
                    aliases,
                )

    def test_all_production_iframes_use_stable_reload_policy(self):
        for count in (1, 4, 6):
            inventory_ids = ",".join(str(576 + index) for index in range(count))
            with self.subTest(count=count):
                widgets = self._payload(inventory_ids)["widgets"]
                self.assertEqual(len(widgets), count)
                self.assertTrue(all(widget["type"] == "iframe" for widget in widgets))
                self.assertTrue(all(widget["reload_policy"] == "stable" for widget in widgets))
                self.assertTrue(all("refreshInterval=30" in widget["url"] for widget in widgets))

    def test_duplicate_inventory_ids_are_removed_by_existing_parser(self):
        payload = self._payload("576,577,576")
        self.assertEqual(len(payload["widgets"]), 2)

    def test_empty_inventory_returns_card_without_broken_iframe(self):
        payload = self._payload("")
        self.assertEqual((payload["columns"], payload["rows"]), (1, 1))
        self.assertEqual(payload["widgets"][0]["type"], "card")
        self.assertIn("Inventory ID tanımlı değil", payload["widgets"][0]["html"])

    def test_url_options_encoding_and_existing_query_parameters(self):
        payload = self._payload(
            "A/1 & B",
            base_url="https://example.com/widget?kept=yes&theme=old&scale=old",
            theme="dark",
            refresh_interval=60,
            scale="2.8",
        )
        query = parse_qs(urlparse(payload["widgets"][0]["url"]).query)
        self.assertEqual(query["deviceAlias"], ["A/1 & B"])
        self.assertEqual(query["theme"], ["dark"])
        self.assertEqual(query["refreshInterval"], ["60"])
        self.assertEqual(query["scale"], ["2.8"])
        self.assertEqual(query["kept"], ["yes"])

    def test_empty_scale_is_omitted(self):
        url = self._payload("576", scale_mode="manual", scale="")["widgets"][0]["url"]
        self.assertNotIn("scale", parse_qs(urlparse(url).query, keep_blank_values=True))

    def test_auto_mode_uses_neutral_hub_scale_and_client_fit_metadata(self):
        for count in (1, 2, 4, 6, 7, 9, 12):
            inventory_id = ",".join(str(576 + index) for index in range(count))
            with self.subTest(inventory_id=inventory_id):
                payload = self._payload(inventory_id, scale_mode="auto", scale="1.23")
                scales = {
                    parse_qs(urlparse(widget["url"]).query)["scale"][0]
                    for widget in payload["widgets"]
                }
                self.assertEqual(scales, {"1"})
                for widget in payload["widgets"]:
                    self.assertEqual(widget["reload_policy"], "stable")
                    self.assertEqual(widget["fit_mode"], "production_auto")
                    self.assertEqual(widget["fit_width"], 640)
                    self.assertEqual(widget["fit_height"], 520)
                    self.assertEqual(widget["fit_safety"], 0.97)

    def test_seven_inventory_grid_uses_client_auto_fit_and_stable_iframes(self):
        payload = self._payload("576,577,578,579,580,581,582", scale_mode="auto")
        self.assertEqual((payload["columns"], payload["rows"]), (3, 3))
        self.assertTrue(all(parse_qs(urlparse(widget["url"]).query)["scale"] == ["1"] for widget in payload["widgets"]))
        self.assertTrue(all(widget["fit_mode"] == "production_auto" for widget in payload["widgets"]))
        self.assertTrue(all(widget["reload_policy"] == "stable" for widget in payload["widgets"]))

    def test_legacy_scale_is_manual_and_missing_scale_defaults_to_auto(self):
        legacy = self.main._production_grid_config({"scale": 1.75})
        self.assertEqual(legacy["scale_mode"], "manual")
        self.assertEqual(legacy["scale"], "1.75")

        automatic = self.main._production_grid_config({"theme": "light"})
        self.assertEqual(automatic["scale_mode"], "auto")
        payload = self._payload("576,577,578,579", scale_mode="auto", scale=None)
        query = parse_qs(urlparse(payload["widgets"][0]["url"]).query)
        self.assertEqual(query["scale"], ["1"])

    def test_manual_mode_uses_stored_scale_for_every_widget(self):
        payload = self._payload("576,577,578,579", scale_mode="manual", scale=1.25)
        scales = [
            parse_qs(urlparse(widget["url"]).query)["scale"][0]
            for widget in payload["widgets"]
        ]
        self.assertEqual(scales, ["1.25"] * 4)
        self.assertTrue(all("fit_mode" not in widget for widget in payload["widgets"]))

    def test_production_grid_widget_api_and_device_specific_runtime_payload(self):
        client = self.main.app.test_client()
        config = json.dumps({"theme": "dark", "refresh_interval": 60, "scale": None})
        with patch("app.main._auth_failed", return_value=False):
            response = client.post(
                "/api/widgets",
                json={"name": "Üretim Durumu", "type": "production_grid", "content": config},
            )
            self.assertEqual(response.status_code, 200)
            widget_id = response.get_json()["id"]
            update_response = client.patch(
                f"/api/widgets/{widget_id}",
                json={"name": "Üretim Durumu", "type": "production_grid", "content": config},
            )
            self.assertEqual(update_response.status_code, 200)

        db = self.main.db_session()
        try:
            group = self.main.Group(name="Production Grid Group")
            playlist = self.main.Playlist(name="Production Grid Playlist", enabled=True)
            device = self.main.Device(hostname="production-grid-device", inventory_id="576,577,581,590,602")
            db.add_all([group, playlist, device])
            db.commit()
            db.add_all([
                self.main.DeviceGroup(device_id=device.id, group_id=group.id, is_active=True),
                self.main.GroupPlaylist(group_id=group.id, playlist_id=playlist.id),
                self.main.PlaylistItem(playlist_id=playlist.id, item_type="widget", widget_id=widget_id, order_no=0),
            ])
            db.commit()
        finally:
            db.close()

        with self.main.app.test_request_context():
            item = self.main.build_config("production-grid-device")["videos"][0]
        self.assertIsNone(item["widget_url"])
        self.assertEqual(item["widget_payload"]["name"], "Üretim Durumu")
        self.assertEqual((item["widget_payload"]["columns"], item["widget_payload"]["rows"]), (3, 2))
        self.assertTrue(all("theme=dark" in widget["url"] for widget in item["widget_payload"]["widgets"]))
        self.assertTrue(all("refreshInterval=60" in widget["url"] for widget in item["widget_payload"]["widgets"]))


if __name__ == "__main__":
    unittest.main()
