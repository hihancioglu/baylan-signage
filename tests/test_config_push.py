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


if __name__ == "__main__":
    unittest.main()
