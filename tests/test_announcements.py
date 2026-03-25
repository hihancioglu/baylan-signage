import importlib
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch


class TestAnnouncements(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmpdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        os.environ["DATABASE_URL"] = f"sqlite:///{cls._tmpdir.name}/test_announcements.db"
        cls.main = importlib.import_module("app.main")

    @classmethod
    def tearDownClass(cls):
        # app.main is shared across test modules in the same interpreter process.
        # Keeping this temp DB alive avoids cross-module readonly sqlite handles.
        pass

    def test_create_list_and_delete_persistent_announcement(self):
        with patch("app.main._auth_failed", return_value=False):
            create_resp = self.main.app.test_client().post(
                "/api/announcements",
                json={
                    "title": "Kalıcı Duyuru",
                    "message": "Her zaman görün",
                    "target": {"type": "all", "value": "all"},
                    "ttl_sec": 10,
                    "is_persistent": True,
                },
            )

            self.assertEqual(create_resp.status_code, 200)
            announcement_id = create_resp.get_json().get("announcement_id")
            self.assertIsNotNone(announcement_id)

            list_resp = self.main.app.test_client().get("/api/announcements")
            self.assertEqual(list_resp.status_code, 200)
            rows = list_resp.get_json() or []
            row = next((item for item in rows if item.get("id") == announcement_id), None)
            self.assertIsNotNone(row)
            self.assertEqual(row.get("is_persistent"), True)

            delete_resp = self.main.app.test_client().delete(f"/api/announcements/{announcement_id}")
            self.assertEqual(delete_resp.status_code, 200)

            list_after_delete = self.main.app.test_client().get("/api/announcements").get_json() or []
            self.assertFalse(any(item.get("id") == announcement_id for item in list_after_delete))

    def test_active_announcement_expires_when_not_persistent(self):
        db = self.main.db_session()
        try:
            group = self.main.Group(name="Ann Group Announcements")
            device = self.main.Device(hostname="pc-ann-announcements")
            db.add_all([group, device])
            db.commit()

            db.add(self.main.DeviceGroup(device_id=device.id, group_id=group.id, is_active=True))

            old_time = datetime.now(timezone.utc) - timedelta(seconds=20)
            older_time = datetime.now(timezone.utc) - timedelta(seconds=40)
            expired = self.main.Announcement(
                title="Expired",
                message="Old",
                target_type="all",
                target_value="all",
                ttl_sec=10,
                is_persistent=False,
                is_active=True,
                published_at=older_time,
            )
            persistent = self.main.Announcement(
                title="Persistent",
                message="Always",
                target_type="all",
                target_value="all",
                ttl_sec=10,
                is_persistent=True,
                is_active=True,
                published_at=old_time,
            )
            db.add_all([expired, persistent])
            db.commit()

            active = self.main._active_announcement_for_device(db, "pc-ann-announcements", group.id)
            self.assertIsNotNone(active)
            self.assertEqual(active.id, persistent.id)

        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()
