import importlib
import os
import tempfile
import unittest
from unittest.mock import patch


class TestCallManagement(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmpdir = tempfile.TemporaryDirectory()
        os.environ["DATABASE_URL"] = f"sqlite:///{cls._tmpdir.name}/test_call_management.db"
        cls.main = importlib.import_module("app.main")

    @classmethod
    def tearDownClass(cls):
        cls._tmpdir.cleanup()

    def test_call_management_enable_and_cancel(self):
        with patch("app.main._auth_failed", return_value=False):
            db = self.main.db_session()
            try:
                db.add(self.main.Device(hostname="pc-call"))
                db.commit()
                db.add(
                    self.main.CallRequest(
                        hostname="pc-call",
                        requested_role="Vardiya Amiri",
                        status="active",
                    )
                )
                db.commit()
            finally:
                db.close()

            patch_resp = self.main.app.test_client().patch(
                "/api/call-management",
                json={"enabled_hostnames": ["pc-call"]},
            )
            self.assertEqual(patch_resp.status_code, 200)
            self.assertTrue(patch_resp.get_json().get("ok"))

            list_resp = self.main.app.test_client().get("/api/call-management")
            self.assertEqual(list_resp.status_code, 200)
            payload = list_resp.get_json() or {}
            self.assertIn("pc-call", payload.get("enabled_hostnames") or [])
            host_calls = [row for row in (payload.get("active_calls") or []) if row.get("hostname") == "pc-call"]
            self.assertEqual(len(host_calls), 1)
            active_call_id = host_calls[0]["id"]

            cancel_resp = self.main.app.test_client().post(f"/api/call-management/calls/{active_call_id}/cancel", json={})
            self.assertEqual(cancel_resp.status_code, 200)
            self.assertTrue(cancel_resp.get_json().get("ok"))

            list_after_cancel = self.main.app.test_client().get("/api/call-management").get_json() or {}
            remaining_host_calls = [row for row in (list_after_cancel.get("active_calls") or []) if row.get("hostname") == "pc-call"]
            self.assertEqual(len(remaining_host_calls), 0)

    def test_build_config_includes_call_flags(self):
        db = self.main.db_session()
        try:
            db.add(self.main.Device(hostname="pc-call-config"))
            self.main._set_setting(
                db,
                self.main.CALL_FEATURE_ENABLED_HOSTNAMES_KEY,
                '["pc-call-config"]',
            )
            db.add(
                self.main.CallRequest(
                    hostname="pc-call-config",
                    requested_role="Bilgi İşlem",
                    status="active",
                )
            )
            db.commit()
        finally:
            db.close()

        config = self.main.build_config("pc-call-config")
        self.assertTrue(config.get("call_feature_enabled"))
        self.assertEqual((config.get("active_call") or {}).get("requested_role"), "Bilgi İşlem")


if __name__ == "__main__":
    unittest.main()
