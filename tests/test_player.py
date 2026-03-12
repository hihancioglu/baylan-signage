import threading
import unittest
import tempfile
from datetime import datetime, timezone
from pathlib import Path
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

    def test_build_widget_command_prefers_python_viewer_for_url(self):
        player = self._build_player()

        with patch.object(player, "_should_use_python_widget_viewer", return_value=True), patch.object(
            player,
            "_build_python_widget_command",
            return_value=["python", "client/widget_viewer.py", "https://example.com"],
        ):
            command = player._build_widget_command("https://example.com")

        self.assertEqual(command, ["python", "client/widget_viewer.py", "https://example.com"])

    def test_should_use_python_widget_viewer_requires_url_and_runtime_flags(self):
        player = self._build_player()
        player._python_widget_viewer_supported = True
        player._python_widget_viewer_runtime_enabled = True

        with patch.object(player, "_prefer_python_widget_viewer", return_value=True):
            self.assertTrue(player._should_use_python_widget_viewer("https://example.com"))
            self.assertFalse(player._should_use_python_widget_viewer("C:/tmp/file.html"))

        player._python_widget_viewer_runtime_enabled = False
        with patch.object(player, "_prefer_python_widget_viewer", return_value=True):
            self.assertFalse(player._should_use_python_widget_viewer("https://example.com"))

    def test_build_widget_command_uses_windows_kiosk_browser_for_url(self):
        player = self._build_player()

        with patch("client.player.os.name", "nt"), patch.object(
            player,
            "_resolve_windows_kiosk_browser",
            return_value=("msedge", ["--kiosk", "--edge-kiosk-type=fullscreen", "--app={widget}"]),
        ):
            command = player._build_widget_command("https://example.com")

        self.assertEqual(
            command,
            ["msedge", "--kiosk", "--edge-kiosk-type=fullscreen", "--app=https://example.com"],
        )

    def test_play_widget_url_waits_until_stop_request(self):
        player = self._build_player()
        process = unittest.mock.Mock()
        process.poll.side_effect = [None, None, None]
        process.returncode = 0

        with patch.object(player, "_build_widget_command", return_value=["msedge", "--kiosk", "https://example.com"]), patch(
            "subprocess.Popen", return_value=process
        ), patch("time.sleep", side_effect=lambda *_args, **_kwargs: setattr(player, "_stop_requested", True)):
            result = player.play_widget_blocking("https://example.com", duration_sec=1)

        self.assertTrue(result)
        process.terminate.assert_called_once()

    def test_play_widget_url_nonzero_exit_does_not_disable_python_viewer(self):
        player = self._build_player()
        process = unittest.mock.Mock()
        process.poll.side_effect = [None, 1]
        process.returncode = 1
        player._python_widget_viewer_runtime_enabled = True

        with patch.object(
            player,
            "_build_widget_command",
            return_value=["python", "client/widget_viewer.py", "https://example.com"],
        ), patch("subprocess.Popen", return_value=process), patch("time.sleep", return_value=None):
            result = player.play_widget_blocking("https://example.com", duration_sec=1)

        self.assertFalse(result)
        self.assertTrue(player._python_widget_viewer_runtime_enabled)



class _FakePlaybackPlayer:
    image_duration_sec = 8

    @staticmethod
    def is_image(media_path: str) -> bool:
        return media_path.lower().endswith((".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp", ".svg"))

    @staticmethod
    def can_play_with_mpv_playlist(media_paths: list[str]) -> bool:
        return bool(media_paths)

    @staticmethod
    def play_mpv_playlist_blocking(media_paths: list[str], image_duration_sec: int | None = None) -> bool:
        return True

    @staticmethod
    def stop() -> None:
        return None


class _FakeGuiRuntime:
    def download_overlay_active(self) -> bool:
        return False

    def post(self, event_name: str, payload=None):
        return None


class TestPlaybackControllerMpvGate(unittest.TestCase):
    def _build_controller(self):
        from client.client import PlaybackController

        controller = PlaybackController(_FakeGuiRuntime())
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

    def test_sanitize_playback_state_keeps_known_primitive_fields(self):
        from client.client import PlaybackController

        sanitized = PlaybackController._sanitize_playback_state(
            {
                "playlist_key": 123,
                "random_order": ["/tmp/a.mp4", None, 7],
                "random_pos": "2",
                "index": "3",
                "resume_sec": "4.5",
                "unexpected": {"nested": "value"},
            }
        )

        self.assertEqual(
            sanitized,
            {
                "playlist_key": "123",
                "random_order": ["/tmp/a.mp4", "7"],
                "random_pos": 2,
                "index": 3,
                "resume_sec": 4.5,
            },
        )

    def test_sanitize_playback_state_defaults_invalid_numbers(self):
        from client.client import PlaybackController

        sanitized = PlaybackController._sanitize_playback_state(
            {
                "random_pos": object(),
                "index": "bad",
                "resume_sec": object(),
            }
        )

        self.assertEqual(sanitized["random_pos"], 0)
        self.assertEqual(sanitized["index"], 0)
        self.assertEqual(sanitized["resume_sec"], 0.0)

    def test_failed_mpv_playlist_falls_back_to_single_playback(self):
        from client.client import PlaybackController

        controller = PlaybackController(_FakeGuiRuntime())
        player = unittest.mock.Mock()
        player.image_duration_sec = 8
        player.is_image.return_value = False
        player.can_play_with_mpv_playlist.return_value = True
        player.play_mpv_playlist_blocking.return_value = False
        player.last_play_was_interrupted.return_value = False
        player._is_video.return_value = False
        controller.player = player

        def _single_playback(*args, **kwargs):
            controller._running = False
            return True

        player.play_blocking.side_effect = _single_playback

        with patch.object(controller, "_can_use_mpv_playlist_mode", return_value=True), patch.object(
            controller,
            "_effective_playlist",
            return_value=[{"local_path": "/tmp/a.mp4", "duration_sec": None, "media_type": "video"}],
        ), patch.object(controller, "_restore_or_init_runtime_state", return_value={"index": 0, "resume_sec": 0}), patch.object(
            controller,
            "_persist_playback_state",
            return_value=None,
        ), patch("time.sleep", return_value=None):
            controller._running = True
            controller._run()

        self.assertTrue(player.play_mpv_playlist_blocking.called)
        self.assertTrue(player.play_blocking.called)


    def test_is_newer_version_handles_build_prefix_and_unknown_marker(self):
        from client.client import _is_newer_version

        self.assertTrue(_is_newer_version("build-20260301093045", "build-20260228093045"))
        self.assertTrue(_is_newer_version("build-20260301093045", "build-unknown"))

    def test_unversioned_updater_is_treated_as_missing(self):
        from client.client import _is_missing_or_unversioned_build

        self.assertTrue(_is_missing_or_unversioned_build(""))
        self.assertTrue(_is_missing_or_unversioned_build("build-missing"))
        self.assertTrue(_is_missing_or_unversioned_build("build-unknown"))
        self.assertFalse(_is_missing_or_unversioned_build("build-20260227093045"))

    def test_client_updater_update_forces_when_local_version_missing(self):
        from client import client as client_module

        config_data = {
            "client_updater": {
                "version": "build-20260227093045",
                "url": "https://example.com/updater.exe",
                "file_name": "BaylanUpdater.exe",
            }
        }

        with patch.object(client_module, "AUTO_UPDATER_ENABLED", True), patch.object(
            client_module,
            "resolve_local_updater_version",
            return_value="build-missing",
        ), patch.object(client_module, "_wait_for_rollout_slot", return_value=None), patch.object(client_module, "_download_release", return_value=Path("/tmp/updater.exe")) as download_mock, patch.object(
            client_module,
            "_apply_client_updater_package",
            return_value="client_updater_swapped",
        ) as apply_mock:
            client_module._maybe_run_client_updater_update(config_data)

        download_mock.assert_called_once_with(config_data["client_updater"])
        apply_mock.assert_called_once()






    def test_download_release_rejects_size_mismatch(self):
        from client import client as client_module

        class _FakeResp:
            def __init__(self, payload: bytes):
                self._payload = payload
                self._offset = 0

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self, size=-1):
                if self._offset >= len(self._payload):
                    return b""
                if size is None or size < 0:
                    size = len(self._payload) - self._offset
                chunk = self._payload[self._offset:self._offset + size]
                self._offset += len(chunk)
                return chunk

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(client_module, "UPDATER_DOWNLOAD_DIR", Path(tmpdir)), patch.object(
                client_module.urllib_request,
                "urlopen",
                return_value=_FakeResp(b"12345"),
            ):
                with self.assertRaises(RuntimeError) as ctx:
                    client_module._download_release(
                        {
                            "url": "https://example.com/updater.exe",
                            "version": "build-20260304090459",
                            "file_name": "BaylanSignageAgent.exe",
                            "size": 23,
                        }
                    )

        self.assertIn("update_size_mismatch", str(ctx.exception))

    def test_download_release_accepts_exact_size(self):
        from client import client as client_module

        class _FakeResp:
            def __init__(self, payload: bytes):
                self._payload = payload
                self._offset = 0

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self, size=-1):
                if self._offset >= len(self._payload):
                    return b""
                if size is None or size < 0:
                    size = len(self._payload) - self._offset
                chunk = self._payload[self._offset:self._offset + size]
                self._offset += len(chunk)
                return chunk

        payload = b"12345"
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(client_module, "UPDATER_DOWNLOAD_DIR", Path(tmpdir)), patch.object(
                client_module.urllib_request,
                "urlopen",
                return_value=_FakeResp(payload),
            ):
                downloaded = client_module._download_release(
                    {
                        "url": "https://example.com/updater.exe",
                        "version": "build-20260304090459",
                        "file_name": "BaylanSignageAgent.exe",
                        "size": len(payload),
                    }
                )

            self.assertTrue(downloaded.exists())
            self.assertEqual(downloaded.read_bytes(), payload)

    def test_client_updater_update_deduplicates_inflight_version(self):
        from client import client as client_module

        config_data = {
            "client_updater": {
                "version": "build-20260227093045",
                "url": "https://example.com/updater.exe",
                "file_name": "BaylanUpdater.exe",
            }
        }

        try:
            with patch.object(client_module, "AUTO_UPDATER_ENABLED", True), patch.object(
                client_module,
                "resolve_local_updater_version",
                return_value="build-20260101010101",
            ), patch.object(client_module, "_wait_for_rollout_slot", return_value=None), patch.object(
                client_module,
                "_download_release",
                return_value=Path("/tmp/updater.exe"),
            ) as download_mock, patch.object(
                client_module,
                "_apply_client_updater_package",
                return_value="client_updater_swapped",
            ):
                client_module._client_updater_inflight_versions.clear()
                client_module._client_updater_completed_versions.clear()
                client_module._client_updater_inflight_versions.add("build-20260227093045")
                client_module._maybe_run_client_updater_update(config_data)

            download_mock.assert_not_called()
        finally:
            client_module._client_updater_inflight_versions.clear()
            client_module._client_updater_completed_versions.clear()

    def test_client_update_deduplicates_inflight_version(self):
        from client import client as client_module

        config_data = {
            "updater": {
                "version": "build-20260304090459",
                "url": "https://example.com/agent.exe",
                "file_name": "BaylanSignageAgent.exe",
            }
        }

        try:
            with patch.object(client_module, "AUTO_UPDATER_ENABLED", True), patch.object(
                client_module,
                "CLIENT_VERSION",
                "build-20260303235938",
            ), patch.object(client_module, "_wait_for_rollout_slot", return_value=None), patch.object(
                client_module,
                "_download_release",
                return_value=Path("/tmp/agent.exe"),
            ) as download_mock, patch.object(
                client_module,
                "_apply_update_package",
                return_value="windows_update_shutdown_requested",
            ):
                client_module._client_update_inflight_versions.clear()
                client_module._client_update_completed_versions.clear()
                client_module._client_update_inflight_versions.add("build-20260304090459")
                client_module._maybe_run_auto_update(config_data)

            download_mock.assert_not_called()
        finally:
            client_module._client_update_inflight_versions.clear()
            client_module._client_update_completed_versions.clear()

    def test_rollout_wait_runs_only_once_per_version(self):
        from client import client as client_module

        with patch.object(client_module, "AUTO_UPDATE_ROLLOUT_WINDOW_SEC", 900), patch.object(
            client_module,
            "hostname",
            "test-host",
        ), patch.object(client_module.shutdown_event, "wait", return_value=False) as wait_mock:
            client_module._rollout_waited_versions.clear()
            client_module._rollout_inflight_versions.clear()
            client_module._wait_for_rollout_slot("client_updater", "build-20260303165001")
            client_module._wait_for_rollout_slot("client_updater", "build-20260303165001")

        self.assertEqual(wait_mock.call_count, 1)

    def test_rollout_wait_respects_elapsed_time_since_publish(self):
        from client import client as client_module

        published_at = "2026-03-04T10:40:00+00:00"
        now_value = datetime(2026, 3, 4, 10, 45, 0, tzinfo=timezone.utc)

        with patch.object(client_module, "AUTO_UPDATE_ROLLOUT_WINDOW_SEC", 900), patch.object(
            client_module,
            "hostname",
            "test-host",
        ), patch.object(client_module.shutdown_event, "wait", return_value=False) as wait_mock, patch.object(
            client_module,
            "_parse_published_at",
            return_value=datetime.fromisoformat(published_at),
        ), patch.object(client_module, "datetime") as datetime_mock:
            datetime_mock.now.return_value = now_value
            datetime_mock.fromisoformat.side_effect = datetime.fromisoformat
            client_module._rollout_waited_versions.clear()
            client_module._rollout_inflight_versions.clear()
            client_module._wait_for_rollout_slot("client", "build-20260304103445", published_at)

        waited_for = wait_mock.call_args[0][0]
        self.assertLess(waited_for, 900)
        self.assertGreaterEqual(waited_for, 0)

    def test_rollout_wait_is_deduplicated_across_threads(self):
        from client import client as client_module

        with patch.object(client_module, "AUTO_UPDATE_ROLLOUT_WINDOW_SEC", 900), patch.object(
            client_module,
            "hostname",
            "test-host",
        ), patch.object(client_module.shutdown_event, "wait", return_value=False) as wait_mock:
            client_module._rollout_waited_versions.clear()
            client_module._rollout_inflight_versions.clear()

            threads = [
                threading.Thread(
                    target=client_module._wait_for_rollout_slot,
                    args=("client_updater", "build-20260303165001"),
                )
                for _ in range(8)
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        self.assertEqual(wait_mock.call_count, 1)

    def test_update_from_config_stops_fallback_playback_when_enabled_config_arrives_with_same_version(self):
        from client.client import PlaybackController

        controller = PlaybackController(_FakeGuiRuntime())
        player = unittest.mock.Mock()
        player.image_duration_sec = 8
        controller.player = player

        controller._fallback_only_mode = True
        controller._version = "ver-1"
        controller._playlist_entries = [{"local_path": "/tmp/a.mp4", "duration_sec": None, "media_type": "video"}]

        with patch.object(controller.media_manager, "sync_playlist_entries", return_value=[]):
            controller.update_from_config(
                {
                    "enabled": True,
                    "videos": [{"path": "https://example.com/a.mp4", "media_type": "video", "duration_sec": None}],
                    "playlist_version": "ver-1",
                    "media_signatures": {},
                    "loop_mode": "sequential",
                }
            )

        player.stop.assert_called_once()


if __name__ == "__main__":
    unittest.main()
