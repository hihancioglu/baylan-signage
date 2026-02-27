import unittest
from unittest.mock import patch

from client.player import BorderlessFullscreenPlayer


class TestBorderlessFullscreenPlayer(unittest.TestCase):
    def test_uses_python_image_viewer_for_png(self):
        player = BorderlessFullscreenPlayer()
        with patch.dict("os.environ", {"PYTHON_IMAGE_VIEWER_ENABLED": "1"}, clear=False):
            self.assertTrue(player._should_use_python_image_viewer("/tmp/a.png"))
            self.assertFalse(player._should_use_python_image_viewer("/tmp/a.webp"))

    def test_build_python_image_command(self):
        player = BorderlessFullscreenPlayer()
        cmd = player._build_python_image_command("/tmp/example.jpg", image_duration_sec=5)
        self.assertEqual(cmd[-2:], ["/tmp/example.jpg", "5"])
        self.assertTrue(cmd[0])
        self.assertTrue(cmd[1].endswith("client/image_viewer.py"))

    def test_disable_python_viewer_after_runtime_failure(self):
        player = BorderlessFullscreenPlayer()
        self.assertTrue(player._should_use_python_image_viewer("/tmp/a.jpg"))
        player._python_image_viewer_runtime_enabled = False
        self.assertFalse(player._should_use_python_image_viewer("/tmp/a.jpg"))

    def test_build_mpv_image_command(self):
        player = BorderlessFullscreenPlayer()
        cmd = player._build_mpv_image_command("/tmp/example.jpg", image_duration_sec=5)
        self.assertEqual(cmd[-1], "/tmp/example.jpg")
        self.assertIn("--image-display-duration=5", cmd)
        self.assertEqual(cmd[0], "mpv")


if __name__ == "__main__":
    unittest.main()
