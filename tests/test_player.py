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


    def test_play_blocking_ignores_stdout_oserror(self):
        player = self._build_player()
        with patch("builtins.print", side_effect=OSError(6, "invalid handle")):
            self.assertFalse(player.play_blocking("/tmp/missing.jpg"))

    def test_supports_slideshow_manifest(self):
        player = self._build_player()
        self.assertTrue(player.supports_media("/tmp/slides.json"))

    def test_detects_slideshow_manifest(self):
        player = self._build_player()
        self.assertTrue(player._is_slideshow_manifest("/tmp/slides.json"))
        self.assertFalse(player._is_slideshow_manifest("/tmp/image.jpg"))

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


    def test_split_command_text_windows_uses_commandlinetoargvw(self):
        player = self._build_player()
        with patch("client.player.os.name", "nt"), patch.object(
            player,
            "_split_windows_command",
            return_value=["C:/Program Files/VideoLAN/VLC/vlc.exe", "--fullscreen", "{media}"],
        ) as split_windows:
            parts = player._split_command_text('"C:/Program Files/VideoLAN/VLC/vlc.exe" --fullscreen {media}')

        self.assertEqual(parts, ["C:/Program Files/VideoLAN/VLC/vlc.exe", "--fullscreen", "{media}"])
        split_windows.assert_called_once()

    def test_split_command_text_falls_back_to_simple_split_on_error(self):
        player = self._build_player()
        with patch("client.player.shlex.split", side_effect=ValueError("bad quote")):
            parts = player._split_command_text('mpv --flag {media}')

        self.assertEqual(parts, ["mpv", "--flag", "{media}"])

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

    def test_build_python_image_command_prefers_frozen_viewer_executable(self):
        player = self._build_player()
        with patch("client.player.getattr", return_value=True), patch.object(
            player,
            "_find_frozen_image_viewer_executable",
            return_value="C:/app/image_viewer.exe",
        ):
            cmd = player._build_python_image_command("/tmp/example.jpg", image_duration_sec=7)
        self.assertEqual(cmd, ["C:/app/image_viewer.exe", "/tmp/example.jpg", "7"])

    def test_detect_python_viewer_support_disabled_when_frozen_viewer_missing(self):
        with patch("client.player.getattr", return_value=True), patch.object(
            BorderlessFullscreenPlayer,
            "_find_frozen_image_viewer_executable",
            return_value=None,
        ):
            player = BorderlessFullscreenPlayer()
        self.assertFalse(player._python_image_viewer_supported)

    def test_skips_slideshow_manifest_when_python_viewer_unavailable(self):
        player = self._build_player()
        with patch.object(player, "_should_use_python_image_viewer", return_value=False):
            self.assertFalse(player.play_blocking("/tmp/slides.json"))

    def test_does_not_fallback_to_media_player_for_slideshow_manifest(self):
        player = self._build_player()
        process = unittest.mock.Mock()
        process.returncode = 1
        process.wait.return_value = None

        with patch("pathlib.Path.exists", return_value=True), patch.object(
            player,
            "_should_use_python_image_viewer",
            return_value=True,
        ), patch.object(player, "_build_python_image_command", return_value=["python", "viewer", "x", "5"]), patch.object(
            player,
            "_resolve_executable",
            return_value=True,
        ), patch("subprocess.Popen", return_value=process) as popen:
            self.assertFalse(player.play_blocking("/tmp/slides.json", image_duration_sec=5))
        self.assertEqual(popen.call_count, 1)


class _FakePlaybackPlayer:
    image_duration_sec = 8

    @staticmethod
    def is_image(media_path: str) -> bool:
        return media_path.lower().endswith((".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp", ".svg"))


class TestPlaybackControllerMpvGate(unittest.TestCase):
    def _build_controller(self):
        from client.client import PlaybackController

        controller = PlaybackController()
        controller.player = _FakePlaybackPlayer()
        return controller

    def test_disables_mpv_playlist_when_image_has_custom_duration(self):
        controller = self._build_controller()
        entries = [
            {"local_path": "/tmp/a.jpg", "duration_sec": 20, "media_type": "image"},
            {"local_path": "/tmp/b.jpg", "duration_sec": 20, "media_type": "image"},
        ]
        self.assertFalse(controller._can_use_mpv_playlist_mode(entries))

    def test_allows_mpv_playlist_when_image_duration_matches_default(self):
        controller = self._build_controller()
        entries = [
            {"local_path": "/tmp/a.jpg", "duration_sec": 8, "media_type": "image"},
            {"local_path": "/tmp/b.png", "duration_sec": None, "media_type": "image"},
        ]
        self.assertTrue(controller._can_use_mpv_playlist_mode(entries))


if __name__ == "__main__":
    unittest.main()
