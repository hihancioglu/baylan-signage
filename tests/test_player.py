import unittest
from unittest.mock import patch

from client.player import BorderlessFullscreenPlayer


class TestBorderlessFullscreenPlayer(unittest.TestCase):
    def _build_player(self):
        with patch.object(
            BorderlessFullscreenPlayer,
            "_detect_python_image_viewer_support",
            return_value=True,
        ):
            return BorderlessFullscreenPlayer()

    def test_uses_python_image_viewer_for_png(self):
        player = self._build_player()
        with patch.dict("os.environ", {"PYTHON_IMAGE_VIEWER_ENABLED": "1"}, clear=False):
            self.assertTrue(player._should_use_python_image_viewer("/tmp/a.png"))
            self.assertFalse(player._should_use_python_image_viewer("/tmp/a.webp"))

    def test_build_python_image_command(self):
        player = self._build_player()
        cmd = player._build_python_image_command("/tmp/example.jpg", image_duration_sec=5)
        self.assertEqual(cmd[-2:], ["/tmp/example.jpg", "5"])
        self.assertTrue(cmd[0])
        self.assertTrue(cmd[1].endswith("client/image_viewer.py"))

    def test_disable_python_viewer_after_runtime_failure(self):
        player = self._build_player()
        self.assertTrue(player._should_use_python_image_viewer("/tmp/a.jpg"))
        player._python_image_viewer_runtime_enabled = False
        self.assertFalse(player._should_use_python_image_viewer("/tmp/a.jpg"))

    def test_build_mpv_image_command(self):
        player = self._build_player()
        cmd = player._build_mpv_image_command("/tmp/example.jpg", image_duration_sec=5)
        self.assertEqual(cmd[-1], "/tmp/example.jpg")
        self.assertIn("--image-display-duration=5", cmd)
        self.assertEqual(cmd[0], "mpv")

    def test_python_viewer_disabled_without_display(self):
        with patch.dict("os.environ", {"DISPLAY": "", "WAYLAND_DISPLAY": ""}, clear=False):
            player = BorderlessFullscreenPlayer()
        self.assertFalse(player._python_image_viewer_supported)


if __name__ == "__main__":
    unittest.main()
