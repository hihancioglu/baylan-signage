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


if __name__ == "__main__":
    unittest.main()
