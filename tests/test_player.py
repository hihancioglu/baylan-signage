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
            self.assertTrue(player._should_use_python_image_viewer("/tmp/a.webp"))
            self.assertTrue(player._should_use_python_image_viewer("/tmp/slides.json"))

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


    def test_supports_slideshow_manifest(self):
        player = self._build_player()
        self.assertTrue(player.supports_media("/tmp/slides.json"))

    def test_build_mpv_video_command_hides_controls_and_sets_black_background(self):
        with patch.dict(
            "os.environ",
            {"PLAYER_VIDEO_COMMAND": "mpv --fs --border=no --force-window=immediate --ontop --quiet --background-color=0/0/0 --osc=no --osd-level=0 --input-cursor=no {media}"},
            clear=False,
        ):
            player = self._build_player()
        cmd = player._build_command("/tmp/example.mp4")
        self.assertIn("--osc=no", cmd)
        self.assertIn("--osd-level=0", cmd)
        self.assertIn("--background-color=0/0/0", cmd)
        self.assertIn("--force-window=immediate", cmd)

    def test_build_mpv_image_command(self):
        player = self._build_player()
        cmd = player._build_mpv_image_command("/tmp/example.jpg", image_duration_sec=5)
        self.assertEqual(cmd[-1], "/tmp/example.jpg")
        self.assertIn("--image-display-duration=5", cmd)
        self.assertEqual(cmd[0], "mpv")

    def test_prefers_mpv_for_images_when_vlc_selected(self):
        player = self._build_player()
        with patch.object(player, "_build_mpv_image_command", return_value=["mpv", "img.jpg"]), patch.object(
            player,
            "_resolve_executable",
            return_value=True,
        ), patch.dict("os.environ", {"ALLOW_VLC_IMAGE_FALLBACK": "0"}, clear=False):
            cmd = player._prefer_non_vlc_image_command("/tmp/example.jpg", ["vlc", "/tmp/example.jpg"])
        self.assertEqual(cmd, ["mpv", "img.jpg"])

    def test_skips_vlc_image_when_mpv_missing_and_fallback_disallowed(self):
        player = self._build_player()
        with patch.object(player, "_build_mpv_image_command", return_value=["mpv", "img.jpg"]), patch.object(
            player,
            "_resolve_executable",
            return_value=False,
        ), patch.dict("os.environ", {"ALLOW_VLC_IMAGE_FALLBACK": "0"}, clear=False):
            cmd = player._prefer_non_vlc_image_command("/tmp/example.jpg", ["vlc", "/tmp/example.jpg"])
        self.assertIsNone(cmd)

    def test_allows_vlc_image_when_fallback_enabled(self):
        player = self._build_player()
        with patch.dict("os.environ", {"ALLOW_VLC_IMAGE_FALLBACK": "1"}, clear=False):
            cmd = player._prefer_non_vlc_image_command("/tmp/example.jpg", ["vlc", "/tmp/example.jpg"])
        self.assertEqual(cmd, ["vlc", "/tmp/example.jpg"])

    def test_python_viewer_disabled_without_display(self):
        with patch.dict("os.environ", {"DISPLAY": "", "WAYLAND_DISPLAY": ""}, clear=False):
            player = BorderlessFullscreenPlayer()
        self.assertFalse(player._python_image_viewer_supported)


if __name__ == "__main__":
    unittest.main()
