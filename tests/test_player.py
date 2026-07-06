import base64
import json
import os
from urllib.parse import parse_qs, unquote, urlparse
import threading
import time
import unittest
import tempfile
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from client.player import BorderlessFullscreenPlayer, WIDGET_ENGINE_SENTINEL


class TestBorderlessFullscreenPlayer(unittest.TestCase):
    def _build_player(self):
        return BorderlessFullscreenPlayer()

    def _decode_widget_engine_source(self, source):
        parsed = urlparse(source)
        encoded = parse_qs(parsed.query)["config_b64"][0]
        return json.loads(base64.urlsafe_b64decode(unquote(encoded)).decode("utf-8"))

    def test_play_blocking_ignores_stdout_oserror(self):
        player = self._build_player()
        with patch("builtins.print", side_effect=OSError(6, "invalid handle")):
            self.assertFalse(player.play_blocking("/tmp/missing.jpg"))

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

    def test_play_blocking_retries_with_alternate_player_after_failure(self):
        player = self._build_player()

        with tempfile.NamedTemporaryFile(suffix=".mp4") as media_file:
            with patch.object(player, "_resolve_executable", return_value=True), patch.object(
                player,
                "_build_command",
                return_value=["mpv", media_file.name],
            ), patch.object(
                player,
                "_build_alternate_command",
                return_value=["vlc", media_file.name],
            ), patch("client.player.subprocess.Popen") as popen:
                first_process = unittest.mock.Mock()
                first_process.returncode = 1
                first_process.wait.return_value = 1
                first_process.poll.return_value = 1

                second_process = unittest.mock.Mock()
                second_process.returncode = 0
                second_process.wait.return_value = 0
                second_process.poll.return_value = 0

                popen.side_effect = [first_process, second_process]

                ok = player.play_blocking(media_file.name)

        self.assertTrue(ok)
        self.assertEqual(popen.call_count, 2)

    def test_play_blocking_returns_false_when_primary_fails_and_no_alternate(self):
        player = self._build_player()

        with tempfile.NamedTemporaryFile(suffix=".mp4") as media_file:
            with patch.object(player, "_resolve_executable", return_value=True), patch.object(
                player,
                "_build_command",
                return_value=["mpv", media_file.name],
            ), patch.object(
                player,
                "_build_alternate_command",
                return_value=None,
            ), patch("client.player.subprocess.Popen") as popen:
                first_process = unittest.mock.Mock()
                first_process.returncode = 2
                first_process.wait.return_value = 2
                first_process.poll.return_value = 2
                popen.return_value = first_process

                ok = player.play_blocking(media_file.name)

        self.assertFalse(ok)
        self.assertEqual(popen.call_count, 1)

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

    def test_build_widget_command_prefers_python_viewer_for_url(self):
        player = self._build_player()

        with patch.object(player, "_should_use_python_widget_viewer", return_value=True), patch.object(
            player,
            "_build_python_widget_command",
            return_value=["python", "client/widget_viewer.py", "https://example.com"],
        ):
            command = player._build_widget_command("https://example.com")

        self.assertEqual(command, ["python", "client/widget_viewer.py", "https://example.com"])

    def test_build_widget_command_falls_back_when_python_viewer_script_missing(self):
        player = self._build_player()

        with patch.object(player, "_should_use_python_widget_viewer", return_value=True), patch.object(
            player,
            "_build_python_widget_command",
            return_value=None,
        ), patch("client.player.os.name", "nt"), patch.object(
            player,
            "_resolve_windows_kiosk_browser",
            return_value=("msedge", ["--kiosk", "--app={widget}"]),
        ):
            command = player._build_widget_command("https://example.com")

        self.assertEqual(command, ["msedge", "--kiosk", "--app=https://example.com"])
        self.assertFalse(player._python_widget_viewer_runtime_enabled)

    def test_build_widget_command_returns_empty_for_blank_source(self):
        player = self._build_player()

        with patch.object(player, "_should_use_python_widget_viewer", return_value=False), patch("client.player.os.name", "nt"):
            command = player._build_widget_command("")

        self.assertEqual(command, [])

    def test_native_url_row_sources_extracts_two_row_iframe_layout(self):
        player = self._build_player()

        with patch.dict("os.environ", {"WIDGET_NATIVE_URL_ROWS": "1"}, clear=False):
            sources = player._native_url_row_sources(
                "",
                widget_config={
                    "rows": 2,
                    "columns": 1,
                    "widgets": [
                        {"type": "iframe", "url": "172.35.10.5/top"},
                        {"type": "url", "content": "172.35.10.5/bottom"},
                    ],
                },
            )

        self.assertEqual(
            sources,
            ["http://172.35.10.5/top", "http://172.35.10.5/bottom"],
        )

    def test_native_url_rows_are_disabled_by_default(self):
        player = self._build_player()

        with patch.dict("os.environ", {}, clear=True):
            self.assertFalse(player.is_native_url_row_widget(
                "",
                widget_config={
                    "rows": 2,
                    "columns": 1,
                    "widgets": [
                        {"type": "iframe", "url": "http://172.35.10.5/top"},
                        {"type": "iframe", "url": "http://172.35.10.5/bottom"},
                    ],
                },
            ))

    def test_native_url_rows_can_be_disabled_by_env(self):
        player = self._build_player()

        with patch.dict("os.environ", {"WIDGET_NATIVE_URL_ROWS": "0"}, clear=True):
            self.assertFalse(player.is_native_url_row_widget(
                "",
                widget_config={
                    "rows": 2,
                    "columns": 1,
                    "widgets": [
                        {"type": "iframe", "url": "http://172.35.10.5/top"},
                        {"type": "iframe", "url": "http://172.35.10.5/bottom"},
                    ],
                },
            ))

    def test_native_widget_grid_specs_supports_multi_column_iframe_and_image_layout(self):
        player = self._build_player()

        with patch.dict("os.environ", {"WIDGET_NATIVE_GRID": "1"}, clear=False), patch("client.player.os.name", "nt"), patch.object(
            player,
            "_windows_connected_monitor_bounds",
            return_value=[(0, 0, 200, 100)],
        ), patch.object(
            player,
            "_build_native_image_page",
            side_effect=lambda source: f"image-page:{source}",
        ):
            specs = player._native_widget_grid_specs(
                "",
                widget_config={
                    "rows": 2,
                    "columns": 2,
                    "widgets": [
                        {"type": "iframe", "url": "172.35.10.5/cycle"},
                        {"type": "iframe", "url": "172.35.10.5/total"},
                        {"type": "image", "url": "http://baylan-portainer:5080/media/logo.png"},
                        {"type": "empty"},
                    ],
                },
                target_monitor_index=0,
            )

        self.assertEqual(len(specs), 3)
        self.assertEqual(specs[0], (0, "http://172.35.10.5/cycle", (0, 0, 104, 54)))
        self.assertEqual(specs[1], (1, "http://172.35.10.5/total", (96, 0, 104, 54)))
        self.assertEqual(specs[2], (2, "image-page:http://baylan-portainer:5080/media/logo.png", (0, 46, 104, 54)))

    def test_native_widget_grid_specs_disabled_by_default(self):
        player = self._build_player()

        with patch.dict("os.environ", {}, clear=True), patch("client.player.os.name", "nt"), patch.object(
            player,
            "_windows_connected_monitor_bounds",
            return_value=[(0, 0, 200, 100)],
        ):
            specs = player._native_widget_grid_specs(
                "",
                widget_config={
                    "rows": 2,
                    "columns": 2,
                    "widgets": [
                        {"type": "iframe", "url": "172.35.10.5/cycle"},
                        {"type": "iframe", "url": "172.35.10.5/total"},
                    ],
                },
                target_monitor_index=0,
            )

        self.assertEqual(specs, [])

    def test_native_widget_grid_specs_accepts_source_aliases_when_enabled(self):
        player = self._build_player()

        with patch.dict("os.environ", {"WIDGET_NATIVE_GRID": "1"}, clear=True), patch("client.player.os.name", "nt"), patch.object(
            player,
            "_windows_connected_monitor_bounds",
            return_value=[(0, 0, 200, 100)],
        ), patch.object(
            player,
            "_build_native_image_page",
            side_effect=lambda source: f"image-page:{source}",
        ):
            specs = player._native_widget_grid_specs(
                "",
                widget_config={
                    "rows": 2,
                    "columns": 2,
                    "widgets": [
                        {"type": "Iframe/URL", "content": "172.35.10.5/cycle"},
                        {"type": "iframe", "source": "172.35.10.5/total"},
                        {"type": "IMAGE", "source_url": "http://baylan-portainer:5080/media/logo.png"},
                        {"type": "empty"},
                    ],
                },
                target_monitor_index=0,
            )

        self.assertEqual(len(specs), 3)
        self.assertEqual(specs[0], (0, "http://172.35.10.5/cycle", (0, 0, 104, 54)))
        self.assertEqual(specs[1], (1, "http://172.35.10.5/total", (96, 0, 104, 54)))
        self.assertEqual(specs[2], (2, "image-page:http://baylan-portainer:5080/media/logo.png", (0, 46, 104, 54)))

    def test_native_widget_grid_specs_can_be_disabled_by_env(self):
        player = self._build_player()

        with patch.dict("os.environ", {"WIDGET_NATIVE_GRID": "0"}, clear=True), patch("client.player.os.name", "nt"), patch.object(
            player,
            "_windows_connected_monitor_bounds",
            return_value=[(0, 0, 200, 100)],
        ):
            specs = player._native_widget_grid_specs(
                "",
                widget_config={
                    "rows": 2,
                    "columns": 2,
                    "widgets": [
                        {"type": "iframe", "url": "172.35.10.5/cycle"},
                        {"type": "iframe", "url": "172.35.10.5/total"},
                    ],
                },
                target_monitor_index=0,
            )

        self.assertEqual(specs, [])

    def test_play_widget_blocking_routes_multi_column_grid_to_native_grid_windows(self):
        player = self._build_player()
        player._python_widget_viewer_supported = True
        specs = [
            (0, "http://172.35.10.5/cycle", (0, 0, 100, 50)),
            (1, "http://172.35.10.5/total", (100, 0, 100, 50)),
        ]

        with patch("client.player.os.name", "nt"), patch.object(
            player,
            "_windows_connected_monitor_bounds",
            return_value=[(0, 0, 200, 100)],
        ), patch.object(
            player,
            "_native_widget_grid_specs",
            return_value=specs,
        ), patch.object(
            player,
            "_play_native_widget_specs_blocking",
            return_value=True,
        ) as play_native_grid:
            ok = player.play_widget_blocking(
                "",
                20,
                widget_config={
                    "rows": 2,
                    "columns": 2,
                    "widgets": [
                        {"type": "iframe", "url": "http://172.35.10.5/cycle"},
                        {"type": "iframe", "url": "http://172.35.10.5/total"},
                    ],
                },
                target_monitor_index=0,
                clone_to_all_monitors=False,
            )

        self.assertTrue(ok)
        play_native_grid.assert_called_once_with(
            specs,
            20,
            signature_kind="grid",
            target_monitor_index=0,
        )

    def test_play_widget_blocking_uses_native_row_windows_for_two_iframes_on_windows(self):
        player = self._build_player()
        player._python_widget_viewer_supported = True

        with patch.dict("os.environ", {"WIDGET_NATIVE_URL_ROWS": "1", "WIDGET_NATIVE_URL_ROWS_PRELOAD_SEC": "0"}, clear=False), patch("client.player.os.name", "nt"), patch.object(
            player,
            "_windows_connected_monitor_bounds",
            return_value=[(0, 0, 1920, 1080)],
        ), patch.object(
            player,
            "_build_python_widget_command",
            side_effect=lambda source: ["python", "widget_viewer.py", source],
        ), patch.object(
            player,
            "_widget_popen_kwargs",
            return_value={},
        ), patch.object(
            player,
            "_wait_native_row_processes_for_slot",
            return_value=True,
        ) as wait_mock, patch.object(player, "_terminate_process") as terminate_mock, patch("client.player.subprocess.Popen") as popen:
            first_process = unittest.mock.Mock()
            second_process = unittest.mock.Mock()
            first_process.poll.return_value = None
            second_process.poll.return_value = None
            popen.side_effect = [first_process, second_process]

            ok = player.play_widget_blocking(
                "",
                20,
                widget_config={
                    "rows": 2,
                    "columns": 1,
                    "widgets": [
                        {"type": "iframe", "url": "http://172.35.10.5/top"},
                        {"type": "iframe", "url": "http://172.35.10.5/bottom"},
                    ],
                },
                target_monitor_index=0,
                clone_to_all_monitors=False,
            )

        self.assertTrue(ok)
        self.assertEqual(popen.call_count, 2)
        first_command = popen.call_args_list[0].args[0]
        second_command = popen.call_args_list[1].args[0]
        self.assertIn("--monitor-bounds=0,0,1920,544", first_command)
        self.assertIn("--monitor-bounds=0,536,1920,544", second_command)
        self.assertIn("--runtime-ipc", first_command)
        self.assertIn("--runtime-ipc", second_command)
        self.assertNotIn("--start-hidden", first_command)
        self.assertNotIn("--start-hidden", second_command)
        self.assertEqual(popen.call_args_list[0].kwargs["stdin"], subprocess.PIPE)
        first_process.stdin.write.assert_not_called()
        second_process.stdin.write.assert_not_called()
        wait_mock.assert_called_once()

    def test_play_widget_blocking_can_use_single_native_row_window_when_enabled(self):
        player = self._build_player()
        player._python_widget_viewer_supported = True

        with patch.dict(
            "os.environ",
            {"WIDGET_NATIVE_URL_ROWS": "1", "WIDGET_NATIVE_URL_ROWS_SINGLE_WINDOW": "1", "WIDGET_NATIVE_URL_ROWS_PRELOAD_SEC": "0"},
            clear=False,
        ), patch("client.player.os.name", "nt"), patch.object(
            player,
            "_windows_connected_monitor_bounds",
            return_value=[(0, 0, 1920, 1080)],
        ), patch.object(
            player,
            "_build_python_widget_command",
            side_effect=lambda source: ["python", "widget_viewer.py", source],
        ), patch.object(
            player,
            "_widget_popen_kwargs",
            return_value={},
        ), patch.object(
            player,
            "_wait_native_row_processes_for_slot",
            return_value=True,
        ), patch("client.player.subprocess.Popen") as popen:
            first_process = unittest.mock.Mock()
            first_process.poll.return_value = None
            popen.side_effect = [first_process]

            ok = player.play_widget_blocking(
                "",
                20,
                widget_config={
                    "rows": 2,
                    "columns": 1,
                    "widgets": [
                        {"type": "iframe", "url": "http://172.35.10.5/top"},
                        {"type": "iframe", "url": "http://172.35.10.5/bottom"},
                    ],
                },
                target_monitor_index=0,
                clone_to_all_monitors=False,
            )

        self.assertTrue(ok)
        self.assertEqual(popen.call_count, 1)
        first_command = popen.call_args_list[0].args[0]
        self.assertTrue(str(first_command[2]).startswith("file:///"))
        self.assertIn("--monitor-bounds=0,0,1920,1080", first_command)
        self.assertIn("--runtime-ipc", first_command)
        self.assertNotIn("--start-hidden", first_command)

    def test_play_widget_blocking_can_opt_into_hidden_native_row_preload(self):
        player = self._build_player()
        player._python_widget_viewer_supported = True

        with patch.dict(
            "os.environ",
            {
                "WIDGET_NATIVE_URL_ROWS": "1",
                "WIDGET_NATIVE_URL_ROWS_HIDDEN_PRELOAD": "1",
                "WIDGET_NATIVE_URL_ROWS_PRELOAD_SEC": "0",
            },
            clear=False,
        ), patch("client.player.os.name", "nt"), patch.object(
            player,
            "_windows_connected_monitor_bounds",
            return_value=[(0, 0, 1920, 1080)],
        ), patch.object(
            player,
            "_build_python_widget_command",
            side_effect=lambda source: ["python", "widget_viewer.py", source],
        ), patch.object(
            player,
            "_widget_popen_kwargs",
            return_value={},
        ), patch.object(
            player,
            "_wait_native_row_processes_for_slot",
            return_value=True,
        ), patch("client.player.subprocess.Popen") as popen:
            first_process = unittest.mock.Mock()
            second_process = unittest.mock.Mock()
            first_process.poll.return_value = None
            second_process.poll.return_value = None
            popen.side_effect = [first_process, second_process]

            ok = player.play_widget_blocking(
                "",
                20,
                widget_config={
                    "rows": 2,
                    "columns": 1,
                    "widgets": [
                        {"type": "iframe", "url": "http://172.35.10.5/top"},
                        {"type": "iframe", "url": "http://172.35.10.5/bottom"},
                    ],
                },
                target_monitor_index=0,
                clone_to_all_monitors=False,
            )

        self.assertTrue(ok)
        self.assertIn("--start-hidden", popen.call_args_list[0].args[0])
        first_process.stdin.write.assert_called_once_with('{"type": "foreground"}\n')
        second_process.stdin.write.assert_called_once_with('{"type": "foreground"}\n')

    def test_play_widget_blocking_reuses_native_row_windows_for_same_layout(self):
        player = self._build_player()
        player._python_widget_viewer_supported = True
        widget_config = {
            "rows": 2,
            "columns": 1,
            "widgets": [
                {"type": "iframe", "url": "http://172.35.10.5/top"},
                {"type": "iframe", "url": "http://172.35.10.5/bottom"},
            ],
        }

        with patch.dict("os.environ", {"WIDGET_NATIVE_URL_ROWS": "1", "WIDGET_NATIVE_URL_ROWS_PRELOAD_SEC": "0"}, clear=False), patch("client.player.os.name", "nt"), patch.object(
            player,
            "_windows_connected_monitor_bounds",
            return_value=[(0, 0, 1920, 1080)],
        ), patch.object(
            player,
            "_build_python_widget_command",
            side_effect=lambda source: ["python", "widget_viewer.py", source],
        ), patch.object(
            player,
            "_widget_popen_kwargs",
            return_value={},
        ), patch.object(
            player,
            "_wait_native_row_processes_for_slot",
            return_value=True,
        ) as wait_mock, patch.object(player, "_terminate_process") as terminate_mock, patch("client.player.subprocess.Popen") as popen:
            first_process = unittest.mock.Mock()
            second_process = unittest.mock.Mock()
            first_process.poll.return_value = None
            second_process.poll.return_value = None
            popen.side_effect = [first_process, second_process]

            first_ok = player.play_widget_blocking(
                "",
                20,
                widget_config=widget_config,
                target_monitor_index=0,
                clone_to_all_monitors=False,
            )
            second_ok = player.play_widget_blocking(
                "",
                20,
                widget_config=widget_config,
                target_monitor_index=0,
                clone_to_all_monitors=False,
            )

        self.assertTrue(first_ok)
        self.assertTrue(second_ok)
        self.assertEqual(popen.call_count, 2)
        self.assertEqual(wait_mock.call_count, 2)
        terminate_mock.assert_not_called()
        self.assertEqual(player._native_row_runtime_processes, [first_process, second_process])

    def test_stop_widget_engine_can_preserve_native_row_windows(self):
        player = self._build_player()
        first_process = unittest.mock.Mock()
        second_process = unittest.mock.Mock()
        first_process.poll.return_value = None
        second_process.poll.return_value = None
        player._native_row_runtime_processes = [first_process, second_process]
        player._native_row_runtime_signature = (("a", "b"), ((0, 0, 100, 50), (0, 50, 100, 50)))
        player._widget_runtime_processes = [first_process, second_process]
        player._widget_process = first_process

        with patch.object(player, "_terminate_process") as terminate_mock:
            player.stop_widget_engine(include_native_rows=False)

        terminate_mock.assert_not_called()
        self.assertEqual(player._native_row_runtime_processes, [first_process, second_process])
        self.assertEqual(player._widget_runtime_processes, [first_process, second_process])
        self.assertIs(player._widget_process, first_process)

    def test_stop_terminates_native_row_windows_when_runtime_is_kept_warm(self):
        player = BorderlessFullscreenPlayer(keep_widget_runtime_warm=True)
        first_process = unittest.mock.Mock()
        second_process = unittest.mock.Mock()
        first_process.poll.return_value = None
        second_process.poll.return_value = None
        player._native_row_runtime_processes = [first_process, second_process]
        player._native_row_runtime_signature = (("a", "b"), ((0, 0, 100, 50), (0, 50, 100, 50)))
        player._widget_runtime_processes = [first_process, second_process]
        player._widget_process = first_process
        first_process.stdin = unittest.mock.Mock()
        second_process.stdin = unittest.mock.Mock()

        with patch.object(player, "_terminate_process") as terminate_mock:
            player.stop(stop_widget_runtime=False)

        terminate_mock.assert_not_called()
        first_process.stdin.write.assert_called_once_with('{"type":"background"}\n')
        second_process.stdin.write.assert_called_once_with('{"type":"background"}\n')
        self.assertEqual(player._native_row_runtime_processes, [first_process, second_process])
        self.assertEqual(player._widget_runtime_processes, [first_process, second_process])
        self.assertIs(player._widget_process, first_process)

    def test_terminate_process_closes_stdin_before_terminate(self):
        player = self._build_player()
        process = unittest.mock.Mock()
        process.stdin = unittest.mock.Mock()
        process.wait.return_value = 0

        player._terminate_process(process, timeout_sec=1)

        process.stdin.close.assert_called_once_with()
        process.terminate.assert_called_once_with()

    def test_widget_runtime_spawn_failure_terminates_already_spawned_processes(self):
        player = self._build_player()
        player._python_widget_viewer_runtime_enabled = True
        player._widget_runtime_processes = []
        spawned_process = unittest.mock.Mock()
        spawned_process.poll.return_value = None

        with patch.object(
            player,
            "_resolve_widget_runtime_monitor_targets",
            return_value=[(0, None), (1, None)],
        ), patch.object(
            player,
            "_build_widget_runtime_command",
            return_value=["python", "viewer.py", "https://example.com"],
        ), patch.object(
            player,
            "_widget_popen_kwargs",
            return_value={},
        ), patch.object(
            player,
            "_widget_runtime_controller_enabled",
            return_value=True,
        ), patch("client.player.subprocess.Popen", side_effect=[spawned_process, RuntimeError("spawn failed")]), patch.object(
            player,
            "_terminate_process",
        ) as terminate_process:
            started = player.start_widget_engine_if_needed(
                target_monitor_index=0,
                clone_to_all_monitors=False,
            )

        self.assertFalse(started)
        terminate_process.assert_called_once_with(spawned_process, timeout_sec=1, force_tree=True)

    def test_python_widget_viewer_enabled_by_default(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertTrue(BorderlessFullscreenPlayer._prefer_python_widget_viewer())

        with patch.dict("os.environ", {"WIDGET_USE_PYTHON_VIEWER": "0"}, clear=True):
            self.assertFalse(BorderlessFullscreenPlayer._prefer_python_widget_viewer())

    def test_detect_widget_viewer_support_rejects_when_backends_missing(self):
        with patch("client.player.os.name", "nt"), patch.dict(
            "os.environ",
            {"WIDGET_VIEWER_BACKEND": "auto"},
            clear=False,
        ), patch("client.player.getattr", return_value=False), patch.dict(
            "sys.modules",
            {"webview": None},
            clear=False,
        ):
            player = BorderlessFullscreenPlayer()

        self.assertFalse(player._python_widget_viewer_supported)

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


    def test_apply_windows_monitor_position_rewrites_window_origin(self):
        flags = ["--kiosk", "--window-position=0,0", "--start-fullscreen"]

        with patch("client.player.os.name", "nt"), patch.object(
            BorderlessFullscreenPlayer,
            "_windows_active_monitor_bounds",
            return_value=(1920, 0, 1920, 1080),
        ):
            positioned = BorderlessFullscreenPlayer._apply_windows_monitor_position(flags)

        self.assertEqual(positioned, ["--kiosk", "--window-position=1920,0", "--window-size=1920,1080"])

    def test_build_widget_command_targets_active_monitor_for_windows_browser(self):
        player = self._build_player()

        with patch("client.player.os.name", "nt"), patch.object(
            player,
            "_should_use_python_widget_viewer",
            return_value=False,
        ), patch.object(
            player,
            "_resolve_windows_kiosk_browser",
            return_value=("msedge", ["--kiosk", "--window-position=0,0", "--app={widget}"]),
        ), patch.object(
            BorderlessFullscreenPlayer,
            "_windows_active_monitor_bounds",
            return_value=(1280, 0, 1280, 1024),
        ):
            command = player._build_widget_command("https://example.com")

        self.assertEqual(
            command,
            [
                "msedge",
                "--kiosk",
                "--window-position=1280,0",
                "--app=https://example.com",
                "--window-size=1280,1024",
            ],
        )

    def test_build_widget_commands_for_monitors_clones_browser_command_per_screen(self):
        player = self._build_player()

        with patch("client.player.os.name", "nt"), patch.object(
            player,
            "_build_widget_command",
            return_value=["msedge", "--kiosk", "--window-position=0,0", "--app=https://example.com"],
        ), patch.object(
            player,
            "_is_python_widget_command",
            return_value=False,
        ), patch.object(
            player,
            "_windows_connected_monitor_bounds",
            return_value=[(0, 0, 1920, 1080), (1920, 0, 1920, 1080), (3840, 0, 1920, 1080)],
        ):
            commands = player._build_widget_commands_for_monitors("https://example.com")

        self.assertEqual(len(commands), 3)
        self.assertEqual(commands[0][1:], ["--kiosk", "--window-position=0,0", "--app=https://example.com", "--window-size=1920,1080"])
        self.assertEqual(commands[1][1:], ["--kiosk", "--window-position=1920,0", "--app=https://example.com", "--window-size=1920,1080"])
        self.assertEqual(commands[2][1:], ["--kiosk", "--window-position=3840,0", "--app=https://example.com", "--window-size=1920,1080"])

    def test_build_widget_commands_for_monitors_ignores_duplicate_monitor_bounds(self):
        player = self._build_player()

        with patch("client.player.os.name", "nt"), patch.object(
            player,
            "_build_widget_command",
            return_value=["msedge", "--kiosk", "--window-position=0,0", "--app=https://example.com"],
        ), patch.object(
            player,
            "_is_python_widget_command",
            return_value=False,
        ), patch.object(
            player,
            "_windows_connected_monitor_bounds",
            return_value=[
                (0, 0, 1920, 1080),
                (0, 0, 1920, 1080),
                (1920, 0, 1920, 1080),
            ],
        ):
            commands = player._build_widget_commands_for_monitors("https://example.com")

        unique_positions = {tuple(command[1:]) for command in commands}
        self.assertEqual(len(unique_positions), 2)

    def test_windows_connected_monitor_bounds_prioritizes_primary_display(self):
        import types
        import ctypes

        class FakeRect:
            def __init__(self, left: int, top: int, right: int, bottom: int):
                self.left = left
                self.top = top
                self.right = right
                self.bottom = bottom

        class FakeRectPtr:
            def __init__(self, rect: FakeRect):
                self.contents = rect

        class FakeUser32:
            def __init__(self):
                self.monitors = [
                    {"rect": (1920, 0, 3840, 1080), "primary": False},
                    {"rect": (0, 0, 1920, 1080), "primary": True},
                ]

            def EnumDisplayMonitors(self, _hdc, _clip, callback, _data):
                for index, monitor in enumerate(self.monitors):
                    left, top, right, bottom = monitor["rect"]
                    callback(index + 1, 0, FakeRectPtr(FakeRect(left, top, right, bottom)), 0)
                return 1

            def GetMonitorInfoW(self, monitor_handle, monitor_info_ptr):
                monitor = self.monitors[int(monitor_handle) - 1]
                monitor_info = monitor_info_ptr._obj
                monitor_info.dwFlags = 1 if monitor["primary"] else 0
                return 1

        fake_windll = types.SimpleNamespace(user32=FakeUser32())
        with patch("client.player.os.name", "nt"), patch("client.player.ctypes.windll", fake_windll, create=True), patch(
            "client.player.ctypes.WINFUNCTYPE",
            side_effect=lambda *_args, **_kwargs: (lambda func: func),
            create=True,
        ):
            bounds = BorderlessFullscreenPlayer._windows_connected_monitor_bounds()

        self.assertEqual(bounds, [(0, 0, 1920, 1080), (1920, 0, 1920, 1080)])

    def test_build_widget_commands_disables_python_viewer_when_cloning_on_windows(self):
        player = self._build_player()

        with patch("client.player.os.name", "nt"), patch.object(
            player,
            "_build_widget_command",
            return_value=["msedge", "--kiosk", "--window-position=0,0", "--app=https://example.com"],
        ) as build_widget_command, patch.object(
            player,
            "_build_widget_commands_for_monitors",
            return_value=[["msedge", "--kiosk", "--window-position=0,0", "--app=https://example.com"]],
        ) as build_for_monitors:
            commands = player._build_widget_commands("https://example.com", clone_to_all_monitors=True)

        self.assertEqual(commands, [["msedge", "--kiosk", "--window-position=0,0", "--app=https://example.com"]])
        self.assertEqual(build_widget_command.call_args.kwargs.get("allow_python_viewer"), False)
        build_for_monitors.assert_called_once_with("https://example.com", clone_to_all_monitors=True)

    def test_build_widget_commands_for_monitors_clones_when_param_true_even_if_env_disabled(self):
        with patch.dict("os.environ", {"WIDGET_CLONE_TO_ALL_MONITORS": "0"}, clear=False):
            player = self._build_player()

        with patch("client.player.os.name", "nt"), patch.object(
            player,
            "_build_widget_command",
            return_value=["msedge", "--kiosk", "--window-position=0,0", "--app=https://example.com"],
        ), patch.object(
            player,
            "_is_python_widget_command",
            return_value=False,
        ), patch.object(
            player,
            "_windows_connected_monitor_bounds",
            return_value=[(0, 0, 1920, 1080), (1920, 0, 1920, 1080)],
        ):
            commands = player._build_widget_commands_for_monitors(
                "https://example.com",
                clone_to_all_monitors=True,
            )

        self.assertEqual(len(commands), 2)

    def test_build_widget_commands_keeps_python_viewer_enabled_for_target_monitor(self):
        player = self._build_player()

        with patch("client.player.os.name", "nt"), patch.object(
            player,
            "_build_widget_command",
            return_value=["python", "widget_viewer.py", "https://example.com"],
        ) as build_widget_command, patch.object(
            player,
            "_windows_connected_monitor_bounds",
            return_value=[(0, 0, 1920, 1080), (1920, 0, 1920, 1080)],
        ), patch.object(
            player,
            "_apply_windows_monitor_position",
            side_effect=lambda parts, monitor_bounds=None: parts,
        ), patch.object(
            player,
            "_is_python_widget_command",
            return_value=True,
        ):
            commands = player._build_widget_commands("https://example.com", target_monitor_index=1, clone_to_all_monitors=False)

        self.assertEqual(build_widget_command.call_args.kwargs.get("allow_python_viewer"), True)
        self.assertIn("--monitor-bounds=1920,0,1920,1080", commands[0])

    def test_build_widget_commands_uses_indexed_bounds_for_explicit_primary_target(self):
        player = self._build_player()

        with patch("client.player.os.name", "nt"), patch.object(
            player,
            "_build_widget_command",
            return_value=["python", "widget_viewer.py", "https://example.com"],
        ), patch.object(
            player,
            "_windows_connected_monitor_bounds",
            return_value=[(-1920, -65, 1536, 960), (0, 0, 1920, 1080)],
        ), patch.object(
            player,
            "_windows_active_monitor_bounds",
            return_value=(0, 0, 1920, 1080),
        ), patch.object(
            player,
            "_apply_windows_monitor_position",
            side_effect=lambda parts, monitor_bounds=None: parts,
        ), patch.object(
            player,
            "_is_python_widget_command",
            return_value=True,
        ):
            commands = player._build_widget_commands("https://example.com", target_monitor_index=0, clone_to_all_monitors=False)

        self.assertIn("--monitor-bounds=-1920,-65,1536,960", commands[0])


    def test_build_widget_commands_prefers_active_monitor_bounds_for_implicit_primary_target(self):
        player = self._build_player()

        with patch("client.player.os.name", "nt"), patch.object(
            player,
            "_build_widget_command",
            return_value=["python", "widget_viewer.py", "https://example.com"],
        ), patch.object(
            player,
            "_windows_connected_monitor_bounds",
            return_value=[(-1920, -65, 1536, 960), (0, 0, 1920, 1080)],
        ), patch.object(
            player,
            "_windows_active_monitor_bounds",
            return_value=(0, 0, 1920, 1080),
        ), patch.object(
            player,
            "_apply_windows_monitor_position",
            side_effect=lambda parts, monitor_bounds=None: parts,
        ), patch.object(
            player,
            "_is_python_widget_command",
            return_value=True,
        ):
            commands = player._build_widget_commands("https://example.com", target_monitor_index=0)

        self.assertIn("--monitor-bounds=0,0,1920,1080", commands[0])

    def test_build_widget_commands_clamps_target_monitor_when_index_is_out_of_bounds(self):
        player = self._build_player()

        with patch("client.player.os.name", "nt"), patch.object(
            player,
            "_build_widget_command",
            return_value=["python", "widget_viewer.py", "https://example.com"],
        ), patch.object(
            player,
            "_windows_connected_monitor_bounds",
            return_value=[(0, 0, 1920, 1080), (1920, 0, 1920, 1080)],
        ), patch.object(
            player,
            "_apply_windows_monitor_position",
            side_effect=lambda parts, monitor_bounds=None: parts,
        ), patch.object(
            player,
            "_is_python_widget_command",
            return_value=True,
        ):
            commands = player._build_widget_commands("https://example.com", target_monitor_index=2, clone_to_all_monitors=False)

        self.assertIn("--monitor", commands[0])
        self.assertIn("1", commands[0])
        self.assertIn("--monitor-bounds=1920,0,1920,1080", commands[0])

    def test_launch_media_processes_clones_mpv_per_monitor(self):
        player = self._build_player()
        fake_process = unittest.mock.Mock()

        with patch("client.player.os.name", "nt"), patch.object(
            player,
            "_windows_connected_monitor_bounds",
            return_value=[(0, 0, 1920, 1080), (1920, 0, 1920, 1080)],
        ), patch("client.player.subprocess.Popen", return_value=fake_process) as popen:
            processes = player._launch_media_processes(["mpv", "--fs", "file.mp4"])

        self.assertEqual(len(processes), 2)
        self.assertEqual(popen.call_count, 2)
        self.assertEqual(
            popen.call_args_list[1].args[0][:4],
            ["mpv", "--screen=1", "--fs-screen=1", "--fs"],
        )

    def test_launch_media_processes_clamps_mpv_screen_index_when_out_of_bounds(self):
        player = self._build_player()
        fake_process = unittest.mock.Mock()

        with patch("client.player.os.name", "nt"), patch.object(
            player,
            "_windows_connected_monitor_bounds",
            return_value=[(0, 0, 1920, 1080), (1920, 0, 1920, 1080)],
        ), patch("client.player.subprocess.Popen", return_value=fake_process) as popen:
            processes = player._launch_media_processes(
                ["mpv", "--fs", "file.mp4"],
                target_monitor_index=2,
                clone_to_all_monitors=False,
            )

        self.assertEqual(len(processes), 1)
        self.assertEqual(popen.call_count, 1)
        self.assertEqual(
            popen.call_args.args[0][:4],
            ["mpv", "--screen=1", "--fs-screen=1", "--fs"],
        )

    def test_launch_media_processes_uses_active_monitor_for_implicit_primary_target(self):
        player = self._build_player()
        fake_process = unittest.mock.Mock()

        with patch("client.player.os.name", "nt"), patch.object(
            player,
            "_windows_connected_monitor_bounds",
            return_value=[(0, 0, 1920, 1080), (1920, 0, 1920, 1080), (3840, 0, 1920, 1080)],
        ), patch.object(
            player,
            "_windows_active_monitor_bounds",
            return_value=(3840, 0, 1920, 1080),
        ), patch("client.player.subprocess.Popen", return_value=fake_process) as popen:
            processes = player._launch_media_processes(
                ["mpv", "--fs", "file.mp4"],
                target_monitor_index=0,
            )

        self.assertEqual(len(processes), 1)
        self.assertEqual(popen.call_count, 1)
        self.assertEqual(
            popen.call_args.args[0][:4],
            ["mpv", "--screen=2", "--fs-screen=2", "--fs"],
        )

    def test_apply_windows_monitor_position_rewrites_size_and_drops_fullscreen_flags(self):
        flags = ["--kiosk", "--start-maximized", "--window-size=800,600", "--start-fullscreen"]

        with patch("client.player.os.name", "nt"), patch.object(
            BorderlessFullscreenPlayer,
            "_windows_active_monitor_bounds",
            return_value=(2560, 0, 2560, 1440),
        ):
            positioned = BorderlessFullscreenPlayer._apply_windows_monitor_position(flags)

        self.assertEqual(positioned, ["--kiosk", "--window-size=2560,1440", "--window-position=2560,0"])
    def test_build_widget_command_uses_windows_kiosk_browser_for_url(self):
        player = self._build_player()

        with patch("client.player.os.name", "nt"), patch.object(
            player,
            "_should_use_python_widget_viewer",
            return_value=False,
        ), patch.object(
            player,
            "_resolve_windows_kiosk_browser",
            return_value=("msedge", ["--kiosk", "--edge-kiosk-type=fullscreen", "--app={widget}"]),
        ):
            command = player._build_widget_command("https://example.com")

        self.assertEqual(
            command,
            ["msedge", "--kiosk", "--edge-kiosk-type=fullscreen", "--app=https://example.com"],
        )



    def test_build_python_widget_command_uses_current_executable_with_widget_flag_in_frozen_mode(self):
        player = self._build_player()

        with patch("client.player.sys.frozen", True, create=True), patch("client.player.sys.executable", "/tmp/BaylanSignageAgent.exe"):
            command = player._build_python_widget_command("https://example.com")

        self.assertEqual(
            command,
            [
                "/tmp/BaylanSignageAgent.exe",
                "--widget",
                "https://example.com",
                "--parent-pid",
                str(os.getpid()),
            ],
        )

    def test_is_python_widget_command_accepts_single_exe_widget_mode(self):
        player = self._build_player()

        with patch("client.player.sys.executable", "/tmp/BaylanSignageAgent.exe"):
            result = player._is_python_widget_command([
                "/tmp/BaylanSignageAgent.exe",
                "--widget",
                "https://example.com",
            ])

        self.assertTrue(result)

    def test_widget_popen_kwargs_uses_create_no_window_for_python_viewer_on_windows(self):
        player = self._build_player()
        with patch("client.player.os.name", "nt"), patch.object(player, "_is_python_widget_command", return_value=True), patch("client.player.subprocess.CREATE_NO_WINDOW", 134217728, create=True):
            kwargs = player._widget_popen_kwargs(["widget_viewer.exe", "https://example.com"])

        self.assertEqual(kwargs.get("creationflags"), 134217728)
        self.assertEqual(kwargs.get("env", {}).get("PYINSTALLER_RESET_ENVIRONMENT"), "1")

    def test_widget_popen_kwargs_empty_for_non_python_widget_command(self):
        player = self._build_player()
        with patch("client.player.os.name", "nt"), patch.object(player, "_is_python_widget_command", return_value=False), patch("client.player.subprocess.CREATE_NO_WINDOW", 134217728, create=True):
            kwargs = player._widget_popen_kwargs(["msedge", "--kiosk", "https://example.com"])

        self.assertEqual(kwargs, {})

    def test_play_widget_url_waits_until_stop_request(self):
        player = self._build_player()
        process = unittest.mock.Mock()
        process.poll.side_effect = [None, None]
        process.returncode = 0

        with patch.dict("os.environ", {"WIDGET_RUNTIME_CONTROLLER_ENABLED": "0"}, clear=False), patch.object(player, "_build_widget_command", return_value=["msedge", "--kiosk", "https://example.com"]), patch(
            "subprocess.Popen", return_value=process
        ), patch("time.sleep", side_effect=lambda *_args, **_kwargs: setattr(player, "_stop_requested", True)):
            result = player.play_widget_blocking("https://example.com", duration_sec=1)

        self.assertTrue(result)
        process.terminate.assert_called_once()

    def test_play_widget_on_target_monitor_does_not_call_global_stop(self):
        player = self._build_player()
        process = unittest.mock.Mock()
        process.poll.side_effect = [None, None]
        process.returncode = 0

        with patch.dict("os.environ", {"WIDGET_RUNTIME_CONTROLLER_ENABLED": "0"}, clear=False), patch.object(
            player,
            "stop",
        ) as stop_mock, patch.object(
            player,
            "_build_widget_commands",
            return_value=[["msedge", "--kiosk", "https://example.com"]],
        ), patch("subprocess.Popen", return_value=process), patch(
            "time.sleep",
            side_effect=lambda *_args, **_kwargs: setattr(player, "_stop_requested", True),
        ):
            result = player.play_widget_blocking(
                "https://example.com",
                duration_sec=1,
                target_monitor_index=1,
                clone_to_all_monitors=False,
            )

        self.assertTrue(result)
        stop_mock.assert_not_called()


    def test_build_widget_source_uses_existing_module_resource_when_meipass_missing(self):
        player = self._build_player()

        with patch('client.player.sys._MEIPASS', '/tmp/nonexistent-meipass', create=True):
            source = player._build_widget_source('https://example.com', widget_config={'widgets': [{'type': 'iframe', 'url': 'https://example.com'}]})

        self.assertEqual(source, 'https://example.com')
        self.assertNotIn('/tmp/nonexistent-meipass', source)

    def test_terminate_process_prefers_taskkill_for_windows_tree(self):
        process = unittest.mock.Mock()
        process.pid = 1234

        with patch('client.player.os.name', 'nt'), patch('client.player.subprocess.run') as run_mock:
            BorderlessFullscreenPlayer._terminate_process(process, timeout_sec=1, force_tree=True)

        run_mock.assert_called_once()
        process.wait.assert_called_once_with(timeout=1)
        process.terminate.assert_not_called()
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

    def test_play_widget_url_advances_when_duration_expires(self):
        player = self._build_player()
        process = unittest.mock.Mock()
        process.poll.return_value = None
        process.returncode = 0

        monotonic_values = iter([100.0, 100.2, 100.6, 101.1, 101.2, 101.3])

        with patch.dict("os.environ", {"WIDGET_RUNTIME_CONTROLLER_ENABLED": "0"}, clear=False), patch.object(
            player,
            "_build_widget_command",
            return_value=["msedge", "--kiosk", "https://example.com"],
        ), patch("subprocess.Popen", return_value=process), patch(
            "time.monotonic",
            side_effect=lambda: next(monotonic_values),
        ), patch("time.sleep", return_value=None):
            result = player.play_widget_blocking("https://example.com", duration_sec=1)

        self.assertTrue(result)
        process.terminate.assert_called_once()


    def test_normalize_widget_source_adds_https_scheme_for_bare_hostname(self):
        player = self._build_player()

        self.assertEqual(player._normalize_widget_source("example.com/dashboard"), "https://example.com/dashboard")


    def test_normalize_widget_source_keeps_http_for_localhost(self):
        player = self._build_player()

        self.assertEqual(player._normalize_widget_source("localhost:8080/dashboard"), "http://localhost:8080/dashboard")

    def test_normalize_widget_source_remaps_stale_widget_engine_file_uri(self):
        player = self._build_player()
        stale = "file:///C:/ProgramData/BaylanSignage/RuntimeTmp/_MEI12345/client/widget_engine.html?config_b64=abc"
        with tempfile.NamedTemporaryFile(suffix=".html", delete=False) as handle:
            engine_path = Path(handle.name)
            handle.write(b"<html></html>")
        remapped_engine = engine_path.with_name("widget_engine.html")
        engine_path.rename(remapped_engine)

        try:
            with patch("client.player._resolve_runtime_resource", return_value=remapped_engine):
                normalized = player._normalize_widget_source(stale)
        finally:
            remapped_engine.unlink(missing_ok=True)

        self.assertIn("widget_engine.html", normalized)
        self.assertIn("config_b64=abc", normalized)
        self.assertNotIn("/_MEI12345/", normalized)

    def test_normalize_widget_source_remaps_foreign_widget_engine_even_when_file_exists(self):
        player = self._build_player()
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_root = Path(tmpdir)
            foreign_engine = tmp_root / "_MEIforeign" / "client" / "widget_engine.html"
            foreign_engine.parent.mkdir(parents=True, exist_ok=True)
            foreign_engine.write_text("<html>foreign</html>", encoding="utf-8")

            local_engine = tmp_root / "_MEIlocal" / "client" / "widget_engine.html"
            local_engine.parent.mkdir(parents=True, exist_ok=True)
            local_engine.write_text("<html>local</html>", encoding="utf-8")

            stale = f"{foreign_engine.resolve().as_uri()}?config_b64=abc"
            with patch("client.player._resolve_runtime_resource", return_value=local_engine):
                normalized = player._normalize_widget_source(stale)

        self.assertIn(local_engine.resolve().as_uri(), normalized)
        self.assertIn("config_b64=abc", normalized)

    def test_normalize_widget_source_remaps_widget_engine_uri_with_trailing_slash(self):
        player = self._build_player()
        stale = "file:///C:/ProgramData/BaylanSignage/RuntimeTmp/_MEI12345/client/widget_engine.html/?config_b64=abc"
        with tempfile.NamedTemporaryFile(suffix=".html", delete=False) as handle:
            engine_path = Path(handle.name)
            handle.write(b"<html></html>")
        remapped_engine = engine_path.with_name("widget_engine.html")
        engine_path.rename(remapped_engine)

        try:
            with patch("client.player._resolve_runtime_resource", return_value=remapped_engine):
                normalized = player._normalize_widget_source(stale)
        finally:
            remapped_engine.unlink(missing_ok=True)

        self.assertIn(remapped_engine.resolve().as_uri(), normalized)
        self.assertIn("config_b64=abc", normalized)

    def test_normalize_widget_source_remaps_widget_engine_uri_with_windows_backslashes(self):
        player = self._build_player()
        stale = r"file://C:\ProgramData\BaylanSignage\RuntimeTmp\_MEI12345\client\widget_engine.html?config_b64=abc"
        with tempfile.NamedTemporaryFile(suffix=".html", delete=False) as handle:
            engine_path = Path(handle.name)
            handle.write(b"<html></html>")
        remapped_engine = engine_path.with_name("widget_engine.html")
        engine_path.rename(remapped_engine)

        try:
            with patch("client.player._resolve_runtime_resource", return_value=remapped_engine):
                normalized = player._normalize_widget_source(stale)
        finally:
            remapped_engine.unlink(missing_ok=True)

        self.assertIn(remapped_engine.resolve().as_uri(), normalized)
        self.assertIn("config_b64=abc", normalized)

    def test_build_widget_layout_payload_normalizes_widget_url(self):
        player = self._build_player()

        payload = player._build_widget_layout_payload("example.com/dashboard")

        self.assertEqual(payload, {"widgets": [{"type": "iframe", "url": "https://example.com/dashboard"}]})

    def test_build_widget_layout_payload_converts_existing_local_file_to_file_uri(self):
        player = self._build_player()

        with tempfile.NamedTemporaryFile(suffix=".html", delete=False) as handle:
            handle.write(b"<html></html>")
            local_path = handle.name

        try:
            payload = player._build_widget_layout_payload(local_path)
            self.assertEqual(payload, {"widgets": [{"type": "iframe", "url": Path(local_path).resolve().as_uri()}]})
        finally:
            Path(local_path).unlink(missing_ok=True)

    def test_build_widget_layout_payload_normalizes_widget_config_iframe_urls(self):
        player = self._build_player()

        payload = player._build_widget_layout_payload(
            "",
            widget_config={"widgets": [{"type": "iframe", "url": "example.com/panel"}]},
        )

        self.assertEqual(payload, {"widgets": [{"type": "iframe", "url": "https://example.com/panel"}]})

    def test_build_widget_layout_payload_converts_html_widget_to_card(self):
        player = self._build_player()

        payload = player._build_widget_layout_payload(
            "",
            widget_config={"widgets": [{"type": "html", "content": "<b>Merhaba</b>"}]},
        )

        self.assertEqual(payload, {"widgets": [{"type": "card", "content": "<b>Merhaba</b>", "html": "<b>Merhaba</b>"}]})

    def test_build_widget_layout_payload_treats_url_type_as_iframe(self):
        player = self._build_player()

        payload = player._build_widget_layout_payload(
            "",
            widget_config={"widgets": [{"type": "url", "url": "example.com/dashboard"}]},
        )

        self.assertEqual(payload, {"widgets": [{"type": "iframe", "url": "https://example.com/dashboard"}]})

    def test_build_widget_layout_payload_treats_url_content_as_iframe_source(self):
        player = self._build_player()

        payload = player._build_widget_layout_payload(
            "",
            widget_config={"widgets": [{"type": "url", "content": "example.com/dashboard-from-content"}]},
        )

        self.assertEqual(
            payload,
            {
                "widgets": [
                    {
                        "type": "iframe",
                        "content": "example.com/dashboard-from-content",
                        "url": "https://example.com/dashboard-from-content",
                    }
                ]
            },
        )

    def test_build_widget_layout_payload_normalizes_video_widget_source_field(self):
        player = self._build_player()

        payload = player._build_widget_layout_payload(
            "",
            widget_config={"widgets": [{"type": "video", "source": "example.com/media.mp4"}]},
        )

        self.assertEqual(
            payload,
            {"widgets": [{"type": "video", "source": "example.com/media.mp4", "url": "https://example.com/media.mp4"}]},
        )

    def test_build_widget_layout_payload_normalizes_video_widget_path_field(self):
        player = self._build_player()

        payload = player._build_widget_layout_payload(
            "",
            widget_config={"widgets": [{"type": "video", "path": "example.com/from-path.mp4"}]},
        )

        self.assertEqual(
            payload,
            {"widgets": [{"type": "video", "path": "example.com/from-path.mp4", "url": "https://example.com/from-path.mp4"}]},
        )

    def test_build_widget_layout_payload_resolves_absolute_video_path_with_server_url(self):
        player = self._build_player()

        with patch.dict("os.environ", {"SERVER_URL": "http://panel.local:5080"}, clear=False):
            payload = player._build_widget_layout_payload(
                "",
                widget_config={"widgets": [{"type": "video", "path": "/media/from-dashboard.mp4"}]},
            )

        self.assertEqual(
            payload,
            {"widgets": [{"type": "video", "path": "/media/from-dashboard.mp4", "url": "http://panel.local:5080/media/from-dashboard.mp4"}]},
        )

    def test_build_widget_layout_payload_marks_video_widget_empty_when_source_missing(self):
        player = self._build_player()

        payload = player._build_widget_layout_payload(
            "",
            widget_config={"widgets": [{"type": "video", "source": ""}]},
        )

        self.assertEqual(payload, {"widgets": [{"type": "empty", "source": ""}]})

    def test_build_widget_source_uses_direct_source_for_single_url_widget(self):
        player = self._build_player()

        widget_source = player._build_widget_source(
            "https://fallback.example.com",
            widget_config={"widgets": [{"type": "url", "url": "example.com/direct-open"}]},
        )

        self.assertEqual(widget_source, "https://example.com/direct-open")

    def test_build_widget_source_uses_direct_source_for_single_iframe_widget(self):
        player = self._build_player()

        widget_source = player._build_widget_source(
            "https://fallback.example.com",
            widget_config={"widgets": [{"type": "iframe", "url": "https://example.com/direct-iframe"}]},
        )

        self.assertEqual(widget_source, "https://example.com/direct-iframe")

    def test_build_widget_layout_payload_treats_html_embed_as_embed_widget(self):
        player = self._build_player()

        embed_html = '<a href="https://example.com">Widget</a><script>window.__x=1;</script>'
        payload = player._build_widget_layout_payload(
            "",
            widget_config={"widgets": [{"type": "iframe", "url": embed_html}]},
        )

        self.assertEqual(payload, {"widgets": [{"type": "embed", "html": embed_html}]})

    def test_build_widget_layout_payload_inlines_internal_iframe_html(self):
        player = self._build_player()

        class FakeHeaders:
            def get(self, name, default=None):
                if name.lower() == "content-type":
                    return "text/html; charset=utf-8"
                return default

            def get_content_charset(self, _default=None):
                return "utf-8"

        class FakeResponse:
            headers = FakeHeaders()

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _size=-1):
                return b"<!doctype html><html><head></head><body>ok</body></html>"

        with patch.dict(os.environ, {"WIDGET_INLINE_HTTP_IFRAMES": "1"}), patch(
            "client.player.urlopen", return_value=FakeResponse()
        ):
            payload = player._build_widget_layout_payload(
                "",
                widget_config={"widgets": [{"type": "iframe", "url": "http://172.35.10.5:3002/widget"}]},
            )

        widget = payload["widgets"][0]
        self.assertEqual(widget["type"], "embed")
        self.assertEqual(widget["source_url"], "http://172.35.10.5:3002/widget")
        self.assertIn('<base href="http://172.35.10.5:3002/">', widget["html"])

    def test_build_widget_layout_payload_does_not_inline_internal_iframe_html_by_default(self):
        player = self._build_player()

        with patch.dict(os.environ, {}, clear=True), patch("client.player.urlopen") as urlopen_mock:
            payload = player._build_widget_layout_payload(
                "",
                widget_config={"widgets": [{"type": "iframe", "url": "http://172.35.10.5:3002/widget"}]},
            )

        widget = payload["widgets"][0]
        self.assertEqual(widget["type"], "iframe")
        self.assertEqual(widget["url"], "http://172.35.10.5:3002/widget")
        urlopen_mock.assert_not_called()


    def test_build_widget_source_encodes_config_b64(self):
        player = self._build_player()
        config = {
            "widgets": [{"type": "iframe", "url": "https://example.com/a"}],
            "columns": [{"width": 12}],
        }

        widget_source = player._build_widget_source("https://fallback.example.com", widget_config=config)

        parsed = urlparse(widget_source)
        self.assertIn("widget_engine.html", parsed.path)
        encoded = parse_qs(parsed.query)["config_b64"][0]
        payload = json.loads(base64.urlsafe_b64decode(unquote(encoded)).decode("utf-8"))
        self.assertEqual(payload, config)

    def test_build_widget_layout_payload_preserves_numeric_columns(self):
        player = self._build_player()

        payload = player._build_widget_layout_payload(
            "",
            widget_config={
                "widgets": [
                    {"type": "iframe", "url": "https://example.com/first"},
                    {"type": "iframe", "url": "https://example.com/second"},
                ],
                "columns": 2,
            },
        )

        self.assertEqual(payload["columns"], 2)
        self.assertEqual(len(payload["widgets"]), 2)


    def test_update_widget_layout_sends_signature_payload(self):
        player = self._build_player()

        with patch.object(player, "_send_widget_runtime_message", return_value=True) as sender:
            ok = player.update_widget_layout(
                "example.com/dashboard",
                widget_config=None,
                widget_signature="sig-1",
            )

        self.assertTrue(ok)
        sent_message = sender.call_args.args[0]
        self.assertEqual(sent_message["type"], "layout_update")
        self.assertEqual(sent_message["payload"]["signature"], "sig-1")
        self.assertEqual(
            sent_message["payload"]["config"],
            {"widgets": [{"type": "iframe", "url": "https://example.com/dashboard"}]},
        )

    def test_update_widget_layout_without_signature_still_sends_message(self):
        player = self._build_player()

        with patch.object(player, "_send_widget_runtime_message", return_value=True) as sender:
            ok = player.update_widget_layout(
                "https://example.com/no-signature",
                widget_config=None,
                widget_signature=None,
            )

        self.assertTrue(ok)
        sender.assert_called_once()
        sent_message = sender.call_args.args[0]
        self.assertEqual(sent_message["type"], "layout_update")
        self.assertIsNone(sent_message["payload"]["signature"])


    def test_update_widget_layout_resends_same_signature_when_runtime_backgrounded(self):
        player = self._build_player()
        player._active_widget_signature = "sig-1"
        player._last_widget_signature = "sig-1"
        player._widget_runtime_is_backgrounded = True

        with patch.object(player, "_send_widget_runtime_message", return_value=True) as sender:
            ok = player.update_widget_layout(
                "example.com/dashboard",
                widget_config=None,
                widget_signature="sig-1",
            )

        self.assertTrue(ok)
        sender.assert_called_once()
        sent_message = sender.call_args.args[0]
        self.assertEqual(sent_message["type"], "layout_update")
        self.assertEqual(sent_message["payload"]["signature"], "sig-1")
        self.assertFalse(player._widget_runtime_is_backgrounded)


    def test_play_media_in_widget_runtime_clears_stale_stop_flag(self):
        player = self._build_player()
        player._stop_requested = True

        with patch.object(player, "_send_widget_runtime_message", return_value=True), patch.object(
            player,
            "wait_widget_duration",
            return_value=True,
        ) as wait_mock:
            ok = player.play_media_in_widget_runtime_blocking("/tmp/example.mp4", 5)

        self.assertTrue(ok)
        self.assertFalse(player._stop_requested)
        wait_mock.assert_called_once_with(5)


    def test_play_media_in_widget_runtime_video_without_duration_waits_until_interrupted(self):
        player = self._build_player()
        widget_process = unittest.mock.Mock()
        widget_process.poll.return_value = None
        player._widget_process = widget_process

        def _sleep_and_interrupt(_seconds):
            player._stop_requested = True

        with patch.object(player, "_is_video", return_value=True), patch.object(
            player,
            "update_widget_layout",
            return_value=True,
        ) as update_mock, patch("client.player.time.sleep", side_effect=_sleep_and_interrupt):
            ok = player.play_media_in_widget_runtime_blocking(
                "/tmp/example.mp4",
                None,
                start_position_sec=12.5,
            )

        self.assertTrue(ok)
        self.assertTrue(player.last_play_was_interrupted())
        update_mock.assert_called_once()

    def test_play_media_in_widget_runtime_video_without_duration_fails_when_runtime_not_running(self):
        player = self._build_player()
        player._widget_process = None

        with patch.object(player, "_is_video", return_value=True), patch.object(
            player,
            "update_widget_layout",
            return_value=True,
        ):
            ok = player.play_media_in_widget_runtime_blocking("/tmp/example.mp4", None)

        self.assertFalse(ok)

    def test_sync_widget_runtime_playlist_sends_normalized_items(self):
        player = self._build_player()

        with patch.object(player, "_send_widget_runtime_message", return_value=True) as sender:
            ok = player.sync_widget_runtime_playlist(
                [
                    {
                        "signature": "widget-a",
                        "widget_source": "example.com/a",
                        "widget_config": None,
                    },
                    {
                        "signature": "",
                        "widget_source": "example.com/ignored",
                        "widget_config": None,
                    },
                ],
                active_signature="widget-a",
            )

        self.assertTrue(ok)
        sent_message = sender.call_args.args[0]
        self.assertEqual(sent_message["type"], "playlist_sync")
        payload = sent_message["payload"]
        self.assertEqual(payload["active_signature"], "widget-a")
        self.assertEqual(len(payload["items"]), 1)
        self.assertEqual(payload["items"][0]["signature"], "widget-a")
        self.assertEqual(
            payload["items"][0]["payload"],
            {"widgets": [{"type": "iframe", "url": "https://example.com/a"}]},
        )

    def test_play_widget_blocking_prefers_widget_config_source(self):
        player = self._build_player()
        process = unittest.mock.Mock()
        process.poll.return_value = 0
        process.returncode = 0
        captured_sources = []

        def _fake_build_widget_command(source, **_kwargs):
            captured_sources.append(source)
            return ["msedge", source]

        with patch.object(player, "_native_widget_grid_specs", return_value=[]), patch.object(
            player,
            "_native_url_row_sources",
            return_value=[],
        ), patch.object(
            player,
            "_build_widget_command",
            side_effect=_fake_build_widget_command,
        ), patch("subprocess.Popen", return_value=process), patch("time.sleep", return_value=None):
            result = player.play_widget_blocking(
                "https://ignored.example.com",
                duration_sec=1,
                widget_config={
                    "widgets": [
                        {"type": "iframe", "url": "https://config.example.com"},
                        {"type": "iframe", "url": "https://second.example.com"},
                    ]
                },
                clone_to_all_monitors=False,
            )

        self.assertTrue(result)
        self.assertEqual(len(captured_sources), 1)
        self.assertIn("config_b64=", captured_sources[0])

    def test_play_widget_blocking_uses_direct_source_for_single_iframe_widget(self):
        player = self._build_player()
        process = unittest.mock.Mock()
        process.poll.return_value = 0
        process.returncode = 0
        captured_sources = []

        def _fake_build_widget_command(source, **_kwargs):
            captured_sources.append(source)
            return ["msedge", source]

        with patch.object(player, "_build_widget_command", side_effect=_fake_build_widget_command), patch(
            "subprocess.Popen",
            return_value=process,
        ), patch("time.sleep", return_value=None):
            result = player.play_widget_blocking(
                "https://ignored.example.com",
                duration_sec=1,
                widget_config={"widgets": [{"type": "iframe", "url": "https://www.google.com"}]},
                clone_to_all_monitors=False,
            )

        self.assertTrue(result)
        self.assertEqual(len(captured_sources), 1)
        self.assertEqual(captured_sources[0], "https://www.google.com")



    def test_play_widget_blocking_uses_runtime_controller_for_target_monitor(self):
        player = self._build_player()
        player._python_widget_viewer_supported = True

        with patch.object(player, "_build_widget_source", return_value="https://example.com"), patch("client.player.os.name", "nt"), patch.object(player, "_widget_runtime_controller_enabled", return_value=True), patch.object(
            player, "update_widget_layout"
        ) as update_layout, patch.object(
            player,
            "_build_widget_commands",
            return_value=[["msedge", "https://example.com"]],
        ) as build_commands, patch(
            "subprocess.Popen"
        ) as popen_mock, patch.object(player, "_wait_widget_processes_until_stop", return_value=True), patch.object(
            player, "wait_widget_duration", return_value=True
        ) as wait_duration, patch.object(
            player, "stop"
        ):
            process = unittest.mock.Mock()
            process.poll.return_value = 0
            process.returncode = 0
            popen_mock.return_value = process
            update_layout.return_value = True

            result = player.play_widget_blocking(
                "https://example.com",
                duration_sec=1,
                target_monitor_index=1,
                clone_to_all_monitors=False,
            )

        self.assertTrue(result)
        update_layout.assert_called_once()
        wait_duration.assert_called_once_with(1)
        build_commands.assert_not_called()
        popen_mock.assert_not_called()

    def test_play_widget_blocking_holds_duration_when_browser_launcher_exits_immediately(self):
        player = self._build_player()
        process = unittest.mock.Mock()
        process.poll.return_value = 0
        process.returncode = 0

        monotonic_values = iter([100.0, 100.2, 100.3, 100.4, 101.0, 102.5])

        with patch.dict("os.environ", {"WIDGET_RUNTIME_CONTROLLER_ENABLED": "0"}, clear=False), patch.object(
            player,
            "_build_widget_command",
            return_value=["msedge", "--kiosk", "https://example.com"],
        ), patch("subprocess.Popen", return_value=process), patch(
            "time.monotonic",
            side_effect=lambda: next(monotonic_values),
        ), patch("time.sleep", return_value=None) as sleep_mock:
            result = player.play_widget_blocking("https://example.com", duration_sec=2)

        self.assertTrue(result)
        self.assertGreaterEqual(sleep_mock.call_count, 1)

    def test_play_widget_blocking_holds_duration_when_python_widget_launcher_exits_immediately(self):
        player = self._build_player()
        process = unittest.mock.Mock()
        process.poll.return_value = 0
        process.returncode = 0

        monotonic_values = iter([200.0, 200.2, 200.3, 200.4, 201.0, 202.5])

        with patch.dict("os.environ", {"WIDGET_RUNTIME_CONTROLLER_ENABLED": "0"}, clear=False), patch.object(
            player,
            "_build_widget_command",
            return_value=["BaylanSignageAgent.exe", "--widget", "https://example.com"],
        ), patch.object(
            player,
            "_is_python_widget_command",
            return_value=True,
        ), patch("subprocess.Popen", return_value=process), patch(
            "time.monotonic",
            side_effect=lambda: next(monotonic_values),
        ), patch("time.sleep", return_value=None) as sleep_mock:
            result = player.play_widget_blocking("https://example.com", duration_sec=2)

        self.assertTrue(result)
        self.assertGreaterEqual(sleep_mock.call_count, 1)

    def test_play_widget_blocking_holds_duration_when_launcher_exits_before_slot_ends(self):
        player = self._build_player()
        process = unittest.mock.Mock()
        process.poll.return_value = 0
        process.returncode = 0

        monotonic_values = iter([300.0, 300.1, 301.6, 301.7, 302.4, 303.1, 304.2, 305.4])

        with patch.dict("os.environ", {"WIDGET_RUNTIME_CONTROLLER_ENABLED": "0"}, clear=False), patch.object(
            player,
            "_build_widget_command",
            return_value=["BaylanSignageAgent.exe", "--widget", "https://example.com"],
        ), patch("subprocess.Popen", return_value=process), patch(
            "time.monotonic",
            side_effect=lambda: next(monotonic_values),
        ), patch("time.sleep", return_value=None) as sleep_mock:
            result = player.play_widget_blocking("https://example.com", duration_sec=5)

        self.assertTrue(result)
        self.assertGreaterEqual(sleep_mock.call_count, 1)

    def test_play_widget_blocking_holds_duration_when_launcher_exits_early_with_failure(self):
        player = self._build_player()
        process = unittest.mock.Mock()
        process.poll.return_value = 2
        process.returncode = 2

        monotonic_values = iter([400.0, 400.1, 401.6, 401.7])

        with patch.dict("os.environ", {"WIDGET_RUNTIME_CONTROLLER_ENABLED": "0"}, clear=False), patch.object(
            player,
            "_build_widget_command",
            return_value=["BaylanSignageAgent.exe", "--widget", "https://example.com"],
        ), patch("subprocess.Popen", return_value=process), patch.object(
            player,
            "_wait_widget_processes_until_stop",
            return_value=False,
        ), patch.object(
            player,
            "_hold_widget_slot_for_duration",
            return_value=True,
        ) as hold_slot_mock, patch(
            "time.monotonic",
            side_effect=lambda: next(monotonic_values),
        ):
            result = player.play_widget_blocking("https://example.com", duration_sec=5)

        self.assertFalse(result)
        hold_slot_mock.assert_called_once()
        hold_call = hold_slot_mock.call_args
        self.assertEqual(hold_call.args[0], 5)
        self.assertIsInstance(hold_call.kwargs.get("already_elapsed_sec"), float)
        self.assertGreaterEqual(hold_call.kwargs["already_elapsed_sec"], 0.0)

    def test_stop_widget_process_uses_taskkill_tree_on_windows_timeout(self):
        with patch.dict("os.environ", {"WIDGET_KEEP_RUNTIME_WARM": "0"}, clear=False):
            player = self._build_player()
        process = unittest.mock.Mock()
        process.poll.return_value = None
        process.pid = 4242
        player._widget_process = process

        with patch("client.player.os.name", "nt"), patch("subprocess.run") as run_mock:
            player.stop()

        process.wait.assert_called_once_with(timeout=1)
        process.terminate.assert_not_called()
        process.kill.assert_not_called()
        run_mock.assert_called_once()
        self.assertEqual(run_mock.call_args.args[0], ["taskkill", "/PID", "4242", "/T", "/F"])
        self.assertEqual(run_mock.call_args.kwargs["stdout"], subprocess.DEVNULL)
        self.assertEqual(run_mock.call_args.kwargs["stderr"], subprocess.DEVNULL)
        self.assertFalse(run_mock.call_args.kwargs["check"])

    def test_stop_terminates_widget_runtime_by_default(self):
        player = self._build_player()
        widget_process = unittest.mock.Mock()
        widget_process.poll.return_value = None
        widget_process.stdin = unittest.mock.Mock()
        player._widget_process = widget_process

        with patch.object(player, "stop_widget_engine") as stop_widget_engine:
            player.stop()

        stop_widget_engine.assert_called_once()

    def test_stop_does_not_stop_widget_engine_when_background_fails(self):
        player = self._build_player()
        widget_process = unittest.mock.Mock()
        widget_process.poll.return_value = None
        player._widget_process = widget_process

        with patch.object(player, "background_widget_engine", return_value=False), patch.object(
            player,
            "stop_widget_engine",
        ) as stop_widget_engine:
            player.stop(stop_widget_runtime=False)

        stop_widget_engine.assert_not_called()

    def test_stop_widget_runtime_cleans_detached_widget_browser_when_runtime_not_running(self):
        player = self._build_player()
        widget_process = unittest.mock.Mock()
        widget_process.poll.return_value = 0
        player._widget_process = widget_process
        player._last_widget_source = "file:///C:/ProgramData/BaylanSignage/RuntimeTmp/client/widget_engine.html?config_b64=abc"

        with patch("client.player.os.name", "nt"), patch("subprocess.run") as run_mock:
            player.stop(stop_widget_runtime=True)

        run_mock.assert_called_once()
        args = run_mock.call_args.args[0]
        self.assertEqual(args[:3], ["powershell", "-NoProfile", "-Command"])
        self.assertIn("widget_engine", args[3])
        self.assertIn("msedgewebview2.exe", args[3])
        self.assertEqual(args[4], "engine")
        self.assertIn("widget_engine.html", args[5])

    def test_stop_widget_runtime_cleans_detached_direct_url_widget_browser(self):
        player = self._build_player()
        widget_process = unittest.mock.Mock()
        widget_process.poll.return_value = 0
        player._widget_process = widget_process
        player._last_widget_source = "https://widgets.example.com/screen?id=42"

        with patch("client.player.os.name", "nt"), patch("subprocess.run") as run_mock:
            player.stop(stop_widget_runtime=True)

        run_mock.assert_called_once()
        args = run_mock.call_args.args[0]
        self.assertEqual(args[:3], ["powershell", "-NoProfile", "-Command"])
        self.assertIn("msedgewebview2.exe", args[3])
        self.assertEqual(args[4], "source")
        self.assertEqual(args[5], "https://widgets.example.com/screen?id=42")

    def test_stop_widget_runtime_stops_all_running_runtime_processes_when_primary_exited(self):
        player = self._build_player()
        exited_primary = unittest.mock.Mock()
        exited_primary.poll.return_value = 0
        running_secondary = unittest.mock.Mock()
        running_secondary.poll.return_value = None
        player._widget_process = exited_primary
        player._widget_runtime_processes = [exited_primary, running_secondary]

        with patch.object(player, "stop_widget_engine") as stop_widget_engine, patch.object(
            player,
            "_stop_detached_widget_browser_processes",
        ) as cleanup_detached:
            player.stop(stop_widget_runtime=True)

        stop_widget_engine.assert_called_once_with()
        cleanup_detached.assert_not_called()

    def test_background_widget_engine_returns_false_when_not_running(self):
        player = self._build_player()
        self.assertFalse(player.background_widget_engine())

    def test_start_widget_engine_if_needed_is_single_flight(self):
        player = self._build_player()
        started = []

        def _popen_side_effect(*_args, **_kwargs):
            time.sleep(0.05)
            process = unittest.mock.Mock()
            process.poll.return_value = None
            process.stdin = unittest.mock.Mock()
            started.append(process)
            return process

        with patch.object(player, "_widget_runtime_controller_enabled", return_value=True), patch.object(
            player,
            "_build_python_widget_command",
            return_value=["widget_viewer"],
        ), patch.object(player, "_widget_popen_kwargs", return_value={}), patch(
            "subprocess.Popen",
            side_effect=_popen_side_effect,
        ) as popen_mock:
            threads = [threading.Thread(target=player.start_widget_engine_if_needed) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=1)

        self.assertEqual(popen_mock.call_count, 1)
        self.assertEqual(len(started), 1)

    def test_start_widget_engine_uses_engine_sentinel_source(self):
        player = self._build_player()
        process = unittest.mock.Mock()
        process.poll.return_value = None
        process.stdin = unittest.mock.Mock()

        with patch.object(player, "_widget_runtime_controller_enabled", return_value=True), patch.object(
            player,
            "_build_python_widget_command",
            return_value=["widget_viewer"],
        ) as build_command, patch.object(player, "_widget_popen_kwargs", return_value={}), patch(
            "subprocess.Popen",
            return_value=process,
        ) as popen:
            ok = player.start_widget_engine_if_needed()

        self.assertTrue(ok)
        build_command.assert_called_once_with(WIDGET_ENGINE_SENTINEL)
        self.assertNotIn("--start-hidden", popen.call_args.args[0])

    def test_start_widget_engine_can_opt_into_hidden_start(self):
        player = self._build_player()
        process = unittest.mock.Mock()
        process.poll.return_value = None
        process.stdin = unittest.mock.Mock()

        with patch.dict("os.environ", {"WIDGET_RUNTIME_START_HIDDEN": "1"}, clear=False), patch.object(
            player, "_widget_runtime_controller_enabled", return_value=True
        ), patch.object(
            player,
            "_build_python_widget_command",
            return_value=["widget_viewer"],
        ), patch.object(player, "_widget_popen_kwargs", return_value={}), patch(
            "subprocess.Popen",
            return_value=process,
        ) as popen:
            ok = player.start_widget_engine_if_needed()

        self.assertTrue(ok)
        self.assertIn("--start-hidden", popen.call_args.args[0])

    def test_start_widget_engine_keeps_existing_runtime_when_target_not_specified(self):
        player = self._build_player()
        player._keep_widget_runtime_warm = True
        process_a = unittest.mock.Mock()
        process_a.poll.return_value = None
        process_a.stdin = unittest.mock.Mock()
        process_b = unittest.mock.Mock()
        process_b.poll.return_value = None
        process_b.stdin = unittest.mock.Mock()
        player._widget_runtime_processes = [process_a, process_b]
        player._widget_process = process_a
        player._extra_processes = [process_b]

        with patch.object(player, "_widget_runtime_controller_enabled", return_value=True), patch(
            "subprocess.Popen"
        ) as popen_mock:
            ok = player.start_widget_engine_if_needed(
                target_monitor_index=None,
                clone_to_all_monitors=None,
            )

        self.assertTrue(ok)
        self.assertEqual(player._widget_runtime_processes, [process_a, process_b])
        self.assertEqual(player._widget_process, process_a)
        self.assertEqual(player._extra_processes, [process_b])
        popen_mock.assert_not_called()

    def test_start_widget_engine_shrinks_runtime_pool_without_respawn(self):
        player = self._build_player()
        process_a = unittest.mock.Mock()
        process_a.poll.return_value = None
        process_a.stdin = unittest.mock.Mock()
        process_b = unittest.mock.Mock()
        process_b.poll.return_value = None
        process_b.stdin = unittest.mock.Mock()
        process_c = unittest.mock.Mock()
        process_c.poll.return_value = None
        process_c.stdin = unittest.mock.Mock()
        player._widget_runtime_processes = [process_a, process_b, process_c]
        player._widget_process = process_a
        player._extra_processes = [process_b, process_c]

        with patch.object(player, "_widget_runtime_controller_enabled", return_value=True), patch.object(
            player,
            "_resolve_widget_runtime_monitor_targets",
            return_value=[(0, (0, 0, 1920, 1080))],
        ), patch("subprocess.Popen") as popen_mock, patch.object(
            player,
            "_terminate_process",
        ) as terminate_mock:
            ok = player.start_widget_engine_if_needed(
                target_monitor_index=0,
                clone_to_all_monitors=False,
            )

        self.assertTrue(ok)
        self.assertEqual(player._widget_runtime_processes, [process_a])
        self.assertEqual(player._widget_process, process_a)
        self.assertEqual(player._extra_processes, [])
        terminate_mock.assert_any_call(process_b, timeout_sec=2, force_tree=True)
        terminate_mock.assert_any_call(process_c, timeout_sec=2, force_tree=True)
        self.assertEqual(terminate_mock.call_count, 2)
        popen_mock.assert_not_called()

    def test_start_widget_engine_restarts_when_signature_differs_even_if_count_matches(self):
        player = self._build_player()
        running_process = unittest.mock.Mock()
        running_process.poll.return_value = None
        running_process.stdin = unittest.mock.Mock()
        player._widget_runtime_processes = [running_process]
        player._widget_process = running_process
        player._last_runtime_signature = (((0, None),), False, 0)

        spawned_process = unittest.mock.Mock()
        spawned_process.poll.return_value = None
        spawned_process.stdin = unittest.mock.Mock()

        with patch.object(player, "_widget_runtime_controller_enabled", return_value=True), patch.object(
            player,
            "_resolve_widget_runtime_monitor_targets",
            return_value=[(1, (0, 0, 1920, 1080))],
        ), patch.object(
            player,
            "_build_python_widget_command",
            return_value=["widget_viewer"],
        ), patch.object(player, "_widget_popen_kwargs", return_value={}), patch(
            "subprocess.Popen",
            return_value=spawned_process,
        ) as popen_mock, patch.object(
            player,
            "_terminate_process",
        ) as terminate_mock:
            ok = player.start_widget_engine_if_needed(
                target_monitor_index=1,
                clone_to_all_monitors=False,
            )

        self.assertTrue(ok)
        terminate_mock.assert_called_once_with(running_process, timeout_sec=2, force_tree=True)
        popen_mock.assert_called_once()
        self.assertEqual(player._widget_runtime_processes, [spawned_process])
        self.assertEqual(player._widget_process, spawned_process)
        self.assertEqual(player._last_runtime_signature, (((1, (0, 0, 1920, 1080)),), False, 1))

    def test_start_widget_engine_reuses_when_default_and_explicit_target_match(self):
        player = self._build_player()
        running_process = unittest.mock.Mock()
        running_process.poll.return_value = None
        running_process.stdin = unittest.mock.Mock()
        player._widget_runtime_processes = [running_process]
        player._widget_process = running_process
        player._last_runtime_signature = (((0, None),), False, 0)

        with patch.object(player, "_widget_runtime_controller_enabled", return_value=True), patch.object(
            player,
            "_resolve_widget_runtime_monitor_targets",
            return_value=[(0, (0, 0, 1920, 1080))],
        ), patch("subprocess.Popen") as popen_mock, patch.object(
            player,
            "_terminate_process",
        ) as terminate_mock:
            ok = player.start_widget_engine_if_needed(
                target_monitor_index=0,
                clone_to_all_monitors=False,
            )

        self.assertTrue(ok)
        self.assertEqual(player._widget_runtime_processes, [running_process])
        self.assertEqual(player._widget_process, running_process)
        terminate_mock.assert_not_called()
        popen_mock.assert_not_called()

    def test_play_blocking_stops_widget_runtime_even_when_warm(self):
        player = self._build_player()
        widget_process = unittest.mock.Mock()
        widget_process.poll.return_value = None
        player._widget_process = widget_process

        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as handle:
            media_path = handle.name

        media_process = unittest.mock.Mock()
        media_process.wait.return_value = None
        media_process.returncode = 0

        try:
            with patch.object(player, "_build_command", return_value=["mpv", media_path]), patch.object(
                player,
                "_prefer_non_vlc_image_command",
                return_value=["mpv", media_path],
            ), patch.object(player, "_resolve_executable", return_value=True), patch(
                "subprocess.Popen",
                return_value=media_process,
            ), patch.object(player, "stop_widget_engine") as stop_widget_engine:
                ok = player.play_blocking(media_path)

            self.assertTrue(ok)
            stop_widget_engine.assert_called_once()
        finally:
            Path(media_path).unlink(missing_ok=True)

    def test_play_blocking_can_stop_widget_runtime_when_disabled(self):
        with patch.dict("os.environ", {"WIDGET_KEEP_RUNTIME_WARM": "0"}, clear=False):
            player = self._build_player()

        widget_process = unittest.mock.Mock()
        widget_process.poll.return_value = None
        player._widget_process = widget_process

        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as handle:
            media_path = handle.name

        media_process = unittest.mock.Mock()
        media_process.wait.return_value = None
        media_process.returncode = 0

        try:
            with patch.object(player, "_build_command", return_value=["mpv", media_path]), patch.object(
                player,
                "_prefer_non_vlc_image_command",
                return_value=["mpv", media_path],
            ), patch.object(player, "_resolve_executable", return_value=True), patch(
                "subprocess.Popen",
                return_value=media_process,
            ), patch.object(player, "stop_widget_engine") as stop_widget_engine:
                ok = player.play_blocking(media_path)

            self.assertTrue(ok)
            stop_widget_engine.assert_called_once()
        finally:
            Path(media_path).unlink(missing_ok=True)

    def test_wait_widget_until_stop_uses_taskkill_tree_on_windows_timeout(self):
        player = self._build_player()
        process = unittest.mock.Mock()
        process.poll.side_effect = [None, None, None]
        process.pid = 31337
        process.returncode = None

        with patch("client.player.os.name", "nt"), patch("subprocess.run") as run_mock, patch(
            "time.monotonic", side_effect=[100.0, 101.1]
        ), patch("time.sleep", return_value=None):
            result = player._wait_widget_until_stop(process, max_duration_sec=1)

        self.assertTrue(result)
        process.wait.assert_called_once_with(timeout=1)
        process.terminate.assert_not_called()
        process.kill.assert_not_called()
        run_mock.assert_called_once()
        self.assertEqual(run_mock.call_args.args[0], ["taskkill", "/PID", "31337", "/T", "/F"])
        self.assertEqual(run_mock.call_args.kwargs["stdout"], subprocess.DEVNULL)
        self.assertEqual(run_mock.call_args.kwargs["stderr"], subprocess.DEVNULL)
        self.assertFalse(run_mock.call_args.kwargs["check"])



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

    def test_does_not_prewarm_widget_runtime_without_playlist_on_startup(self):
        from client.client import PlaybackController

        fake_player = unittest.mock.Mock()
        with patch("client.client.BorderlessFullscreenPlayer", return_value=fake_player), patch.dict("os.environ", {}, clear=False):
            PlaybackController(_FakeGuiRuntime())

        fake_player.start_widget_engine_if_needed.assert_not_called()

    def test_can_disable_widget_runtime_prewarm_via_env(self):
        from client.client import PlaybackController

        fake_player = unittest.mock.Mock()
        with patch("client.client.BorderlessFullscreenPlayer", return_value=fake_player), patch.dict(
            "os.environ",
            {"WIDGET_PREWARM_ON_STARTUP": "0"},
            clear=False,
        ):
            PlaybackController(_FakeGuiRuntime())

        fake_player.start_widget_engine_if_needed.assert_not_called()

    def test_next_idle_item_is_widget_detects_current_sequential_widget(self):
        from client.client import PlaybackController

        controller = PlaybackController(_FakeGuiRuntime())
        controller._playlist_entries = [
            {
                "item_type": "widget",
                "widget_payload": {"widgets": [{"type": "iframe", "url": "http://172.35.10.5/widget"}]},
                "duration_sec": 15,
            }
        ]
        controller._loop_mode = "sequential"
        controller._playback_state = {}

        self.assertTrue(controller.next_idle_item_is_widget())

    def test_next_idle_item_is_widget_returns_false_for_media(self):
        from client.client import PlaybackController

        controller = PlaybackController(_FakeGuiRuntime())
        controller._playlist_entries = [
            {
                "item_type": "media",
                "media_type": "image",
                "local_path": "C:/media/a.png",
                "duration_sec": 10,
            }
        ]
        controller._loop_mode = "sequential"
        controller._playback_state = {}

        self.assertFalse(controller.next_idle_item_is_widget())

    def test_update_from_config_defers_primary_widget_runtime_until_idle(self):
        from client.client import PlaybackController

        controller = PlaybackController(_FakeGuiRuntime())
        controller.media_manager = unittest.mock.Mock()
        controller.media_manager.sync_playlist_entries.return_value = []
        controller.media_manager.load_last_successful_playlist_entries.return_value = []
        controller.multi_monitor_playback = unittest.mock.Mock()
        controller.multi_monitor_playback.has_active_playlist.return_value = False
        controller.player = unittest.mock.Mock()
        controller.player.is_native_url_row_widget.return_value = False

        controller.update_from_config(
            {
                "enabled": True,
                "videos": [
                    {
                        "item_type": "widget",
                        "widget_url": "https://example.com/w",
                        "duration_sec": 10,
                    }
                ],
                "playlist_version": "v1",
                "monitor_playlists": {},
            }
        )

        controller.player.start_widget_engine_if_needed.assert_not_called()
        controller.player.stop_widget_engine.assert_not_called()

    def test_update_from_config_does_not_prewarm_primary_widget_when_already_visible(self):
        from client.client import PlaybackController

        controller = PlaybackController(_FakeGuiRuntime())
        controller.media_manager = unittest.mock.Mock()
        controller.media_manager.sync_playlist_entries.return_value = []
        controller.media_manager.load_last_successful_playlist_entries.return_value = []
        controller.multi_monitor_playback = unittest.mock.Mock()
        controller.multi_monitor_playback.has_active_playlist.return_value = False
        controller.player = unittest.mock.Mock()
        controller.player.is_native_url_row_widget.return_value = False
        controller.player._active_item = {
            "item_type": "widget",
            "widget_url": "https://example.com/w",
        }
        controller.player.start_widget_engine_if_needed.return_value = True

        controller.update_from_config(
            {
                "enabled": True,
                "videos": [
                    {
                        "item_type": "widget",
                        "widget_url": "https://example.com/w",
                        "duration_sec": 10,
                    }
                ],
                "playlist_version": "v1",
                "monitor_playlists": {},
            }
        )

        controller.player.start_widget_engine_if_needed.assert_not_called()
        controller.player.background_widget_engine.assert_not_called()

    def test_update_from_config_does_not_background_primary_widget_when_secondary_widget_is_visible(self):
        from client.client import PlaybackController

        controller = PlaybackController(_FakeGuiRuntime())
        controller.media_manager = unittest.mock.Mock()
        controller.media_manager.sync_playlist_entries.return_value = []
        controller.media_manager.load_last_successful_playlist_entries.return_value = []
        controller.multi_monitor_playback = unittest.mock.Mock()
        controller.multi_monitor_playback.has_active_playlist.return_value = False
        controller.multi_monitor_playback.has_visible_widget_runtime_content.return_value = True
        controller.player = unittest.mock.Mock()
        controller.player.is_native_url_row_widget.return_value = False
        controller.player._active_item = {
            "item_type": "media",
            "path": "https://example.com/video.mp4",
        }

        controller.update_from_config(
            {
                "enabled": True,
                "videos": [
                    {
                        "item_type": "widget",
                        "widget_url": "https://example.com/w",
                        "duration_sec": 10,
                    }
                ],
                "playlist_version": "v1",
                "monitor_playlists": {},
            }
        )

        controller.player.start_widget_engine_if_needed.assert_not_called()
        controller.player.background_widget_engine.assert_not_called()

    def test_update_from_config_stops_primary_widget_runtime_when_only_video_exists(self):
        from client.client import PlaybackController

        controller = PlaybackController(_FakeGuiRuntime())
        controller.media_manager = unittest.mock.Mock()
        controller.media_manager.sync_playlist_entries.return_value = []
        controller.media_manager.load_last_successful_playlist_entries.return_value = []
        controller.multi_monitor_playback = unittest.mock.Mock()
        controller.multi_monitor_playback.has_active_playlist.return_value = False
        controller.player = unittest.mock.Mock()

        controller.update_from_config(
            {
                "enabled": True,
                "videos": [{"path": "https://example.com/video.mp4", "media_type": "video"}],
                "playlist_version": "v1",
                "monitor_playlists": {},
            }
        )

        controller.player.start_widget_engine_if_needed.assert_not_called()
        controller.player.stop_widget_engine.assert_called_once()

    def test_disables_mpv_playlist_when_image_has_custom_duration(self):
        controller = self._build_controller()
        entries = [
            {"local_path": "/tmp/a.jpg", "duration_sec": 20, "media_type": "image"},
            {"local_path": "/tmp/b.jpg", "duration_sec": 20, "media_type": "image"},
        ]
        self.assertFalse(controller._can_use_mpv_playlist_mode(entries))

    def test_disables_mpv_playlist_even_when_image_duration_matches_default(self):
        controller = self._build_controller()
        entries = [
            {"local_path": "/tmp/a.jpg", "duration_sec": 8, "media_type": "image"},
            {"local_path": "/tmp/b.png", "duration_sec": None, "media_type": "image"},
        ]
        self.assertFalse(controller._can_use_mpv_playlist_mode(entries))

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

    def test_build_widget_playback_spec_uses_columns_from_dashboard_payload(self):
        controller = self._build_controller()

        widget_url, widget_config, widget_signature = controller._build_widget_playback_spec(
            {
                "item_type": "widget",
                "widget_payload": {
                    "columns": 2,
                    "widgets": [
                        {"type": "iframe", "url": "https://example.com/sol"},
                        {"type": "iframe", "url": "https://example.com/sag"},
                    ],
                },
            }
        )

        self.assertEqual(widget_url, "https://example.com/sol")
        self.assertEqual(widget_config["columns"], 2)
        self.assertEqual(len(widget_config["widgets"]), 2)
        self.assertTrue(widget_signature)

    def test_build_widget_playback_spec_parses_dashboard_widget_content_payload(self):
        controller = self._build_controller()

        widget_url, widget_config, widget_signature = controller._build_widget_playback_spec(
            {
                "item_type": "widget",
                "widget_payload": {
                    "name": "Dashboard",
                    "type": "dashboard",
                    "content": json.dumps(
                        {
                            "columns": 2,
                            "widgets": [
                                {"type": "iframe", "url": "https://example.com/left"},
                                {"type": "iframe", "url": "https://example.com/right"},
                            ],
                        }
                    ),
                },
            }
        )

        self.assertEqual(widget_url, "https://example.com/left")
        self.assertEqual(widget_config["columns"], 2)
        self.assertEqual(len(widget_config["widgets"]), 2)
        self.assertEqual(widget_config["widgets"][0]["type"], "iframe")
        self.assertTrue(widget_signature)

    def test_build_widget_playback_spec_parses_stringified_widget_payload(self):
        controller = self._build_controller()

        widget_url, widget_config, widget_signature = controller._build_widget_playback_spec(
            {
                "item_type": "widget",
                "widget_payload": json.dumps(
                    {
                        "type": "dashboard",
                        "content": json.dumps(
                            {
                                "columns": 2,
                                "widgets": [
                                    {"type": "iframe", "url": "https://example.com/left"},
                                    {"type": "iframe", "url": "https://example.com/right"},
                                ],
                            }
                        ),
                    }
                ),
            }
        )

        self.assertEqual(widget_url, "https://example.com/left")
        self.assertEqual(widget_config["columns"], 2)
        self.assertEqual(len(widget_config["widgets"]), 2)
        self.assertTrue(widget_signature)

    def test_build_widget_playback_spec_uses_embed_html_as_fallback_source(self):
        controller = self._build_controller()

        widget_url, widget_config, _ = controller._build_widget_playback_spec(
            {
                "item_type": "widget",
                "widget_payload": {
                    "widgets": [
                        {"type": "embed", "html": "<iframe src=\"https://example.com/embed\"></iframe>"}
                    ],
                },
            }
        )

        self.assertEqual(widget_url, "<iframe src=\"https://example.com/embed\"></iframe>")
        self.assertEqual(widget_config["widgets"][0]["type"], "embed")

    def test_build_widget_playback_spec_returns_none_when_widget_has_no_playable_source(self):
        controller = self._build_controller()

        widget_spec = controller._build_widget_playback_spec(
            {
                "item_type": "widget",
                "widget_payload": {
                    "columns": 2,
                    "widgets": [
                        {"type": "iframe", "url": ""},
                        {"type": "empty"},
                    ],
                },
            }
        )

        self.assertIsNone(widget_spec)

    def test_build_widget_playback_spec_accepts_custom_widget_type_without_url(self):
        controller = self._build_controller()

        widget_spec = controller._build_widget_playback_spec(
            {
                "item_type": "widget",
                "widget_payload": {
                    "widgets": [
                        {"type": "clock", "timezone": "Europe/Istanbul"},
                    ],
                },
            }
        )

        self.assertIsNotNone(widget_spec)

    def test_sequential_widget_playlist_does_not_skip_on_second_loop(self):
        from client.client import PlaybackController

        controller = PlaybackController(_FakeGuiRuntime())
        player = unittest.mock.Mock()
        player.image_duration_sec = 8
        player.is_direct_url_widget.return_value = False
        player.update_widget_layout.return_value = True
        player.last_play_was_interrupted.return_value = False
        player._is_video.return_value = True

        wait_calls = {"count": 0}

        def _wait_widget_duration(_duration):
            wait_calls["count"] += 1
            if wait_calls["count"] >= 4:
                controller._running = False
            return True

        player.wait_widget_duration.side_effect = _wait_widget_duration
        controller.player = player

        playlist_items = [
            {
                "item_type": "widget",
                "duration_sec": 5,
                "widget_payload": {"type": "url", "content": "https://example.com/one"},
            },
            {
                "item_type": "widget",
                "duration_sec": 5,
                "widget_payload": {"type": "url", "content": "https://example.com/two"},
            },
        ]

        with patch.object(controller, "_effective_playlist", return_value=playlist_items), patch.object(
            controller,
            "_restore_or_init_runtime_state",
            return_value={"index": 0, "resume_sec": 0},
        ), patch.object(controller, "_persist_playback_state", return_value=None), patch("time.sleep", return_value=None):
            controller._running = True
            controller._run()

        self.assertEqual(player.wait_widget_duration.call_count, 4)

        played_urls = [
            call.kwargs["widget_config"]["widgets"][0]["content"]
            for call in player.update_widget_layout.call_args_list
            if call.kwargs.get("widget_config")
        ]
        self.assertIn("https://example.com/one", played_urls)
        self.assertIn("https://example.com/two", played_urls)

    def test_sequential_widget_reapplies_layout_on_each_loop(self):
        from client.client import PlaybackController

        controller = PlaybackController(_FakeGuiRuntime())
        player = unittest.mock.Mock()
        player.image_duration_sec = 8
        player.is_direct_url_widget.return_value = False
        player.update_widget_layout.return_value = True
        player.last_play_was_interrupted.return_value = False
        player._is_video.return_value = False

        wait_calls = {"count": 0}

        def _wait_widget_duration(_duration):
            wait_calls["count"] += 1
            if wait_calls["count"] >= 2:
                controller._running = False
            return True

        player.wait_widget_duration.side_effect = _wait_widget_duration
        controller.player = player

        widget_item = {
            "item_type": "widget",
            "duration_sec": 5,
            "widget_payload": {
                "type": "dashboard",
                "content": json.dumps(
                    {
                        "columns": 2,
                        "widgets": [
                            {"type": "iframe", "url": "https://example.com/left"},
                            {"type": "iframe", "url": "https://example.com/right"},
                        ],
                    }
                ),
            },
        }

        with patch.object(controller, "_effective_playlist", return_value=[widget_item]), patch.object(
            controller,
            "_restore_or_init_runtime_state",
            return_value={"index": 0, "resume_sec": 0},
        ), patch.object(controller, "_persist_playback_state", return_value=None), patch.object(
            controller,
            "_prewarm_next_widget",
            return_value=None,
        ), patch("time.sleep", return_value=None):
            controller._running = True
            controller._run()

        self.assertEqual(player.update_widget_layout.call_count, 2)
        self.assertEqual(player.wait_widget_duration.call_count, 2)

    def test_sequential_widget_clears_stop_request_before_wait(self):
        from client.client import PlaybackController

        controller = PlaybackController(_FakeGuiRuntime())
        player = unittest.mock.Mock()
        player.image_duration_sec = 8
        player.is_direct_url_widget.return_value = False
        player.update_widget_layout.return_value = True
        player.last_play_was_interrupted.return_value = False
        player._is_video.return_value = False

        def _wait_widget_duration(_duration):
            controller._running = False
            return True

        player.wait_widget_duration.side_effect = _wait_widget_duration
        controller.player = player

        widget_item = {
            "item_type": "widget",
            "duration_sec": 5,
            "widget_payload": {"type": "url", "content": "https://example.com/one"},
        }

        with patch.object(controller, "_effective_playlist", return_value=[widget_item]), patch.object(
            controller,
            "_restore_or_init_runtime_state",
            return_value={"index": 0, "resume_sec": 0},
        ), patch.object(controller, "_persist_playback_state", return_value=None), patch("time.sleep", return_value=None):
            controller._running = True
            controller._run()

        player.clear_stop_request.assert_called_once_with()
        player.wait_widget_duration.assert_called_once_with(5)

    def test_direct_url_widget_waits_for_configured_duration(self):
        from client.client import PlaybackController

        controller = PlaybackController(_FakeGuiRuntime())
        player = unittest.mock.Mock()
        player.image_duration_sec = 8
        player.is_direct_url_widget.return_value = True
        player.update_widget_layout.return_value = True
        player.has_visible_widget_runtime_content.return_value = True
        player.last_play_was_interrupted.return_value = False
        player._is_video.return_value = False

        def _wait_widget_duration(_duration):
            controller._running = False
            return True

        player.wait_widget_duration.side_effect = _wait_widget_duration
        controller.player = player

        widget_item = {
            "item_type": "widget",
            "duration_sec": 7,
            "widget_payload": {"type": "url", "content": "https://example.com/direct"},
        }

        with patch.object(controller, "_effective_playlist", return_value=[widget_item]), patch.object(
            controller,
            "_restore_or_init_runtime_state",
            return_value={"index": 0, "resume_sec": 0},
        ), patch.object(controller, "_persist_playback_state", return_value=None), patch.object(
            controller,
            "_prewarm_next_widget",
            return_value=None,
        ), patch("time.sleep", return_value=None):
            controller._running = True
            controller._run()

        player.clear_stop_request.assert_called_once_with()
        player.wait_widget_duration.assert_called_once_with(7)

    def test_backgrounded_prewarmed_widget_resends_layout_on_next_idle(self):
        from client.client import PlaybackController

        controller = PlaybackController(_FakeGuiRuntime())
        player = unittest.mock.Mock()
        player.image_duration_sec = 8
        player.is_direct_url_widget.return_value = False
        player.update_widget_layout.return_value = True
        player.has_visible_widget_runtime_content.return_value = False
        player.last_play_was_interrupted.return_value = False
        player._is_video.return_value = False

        def _wait_widget_duration(_duration):
            controller._running = False
            return True

        player.wait_widget_duration.side_effect = _wait_widget_duration
        controller.player = player

        widget_item = {
            "item_type": "widget",
            "duration_sec": 5,
            "widget_payload": {"type": "url", "content": "https://example.com/prewarmed"},
        }
        widget_spec = controller._build_widget_playback_spec(widget_item, playlist_index=0)
        self.assertIsNotNone(widget_spec)
        controller._prewarmed_widget_signature = widget_spec[2]

        with patch.object(controller, "_effective_playlist", return_value=[widget_item]), patch.object(
            controller,
            "_restore_or_init_runtime_state",
            return_value={"index": 0, "resume_sec": 0},
        ), patch.object(controller, "_persist_playback_state", return_value=None), patch.object(
            controller,
            "_prewarm_next_widget",
            return_value=None,
        ), patch("time.sleep", return_value=None):
            controller._running = True
            controller._run()

        player.has_visible_widget_runtime_content.assert_called()
        player.update_widget_layout.assert_called()
        player.wait_widget_duration.assert_called_once_with(5)

    def test_failed_mpv_playlist_falls_back_to_single_playback(self):
        from client.client import PlaybackController

        controller = PlaybackController(_FakeGuiRuntime())
        player = unittest.mock.Mock()
        player.image_duration_sec = 8
        player.is_image.return_value = False
        player.can_play_with_mpv_playlist.return_value = True
        player.play_mpv_playlist_blocking.return_value = False
        player.last_play_was_interrupted.return_value = False
        player._is_video.return_value = True
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

    def test_failed_media_playback_applies_retry_delay_to_prevent_tight_loop(self):
        from client.client import PlaybackController

        controller = PlaybackController(_FakeGuiRuntime())
        player = unittest.mock.Mock()
        player.image_duration_sec = 8
        player.is_image.return_value = False
        player._is_video.return_value = True
        player.play_blocking.return_value = False
        player.last_play_was_interrupted.return_value = False
        controller.player = player

        with patch.object(controller, "_effective_playlist", return_value=[{"local_path": "/tmp/a.mp4", "duration_sec": None, "media_type": "video"}]), patch.object(
            controller,
            "_restore_or_init_runtime_state",
            return_value={"index": 0, "resume_sec": 0},
        ), patch.object(controller, "_persist_playback_state", return_value=None), patch.object(
            controller,
            "_prewarm_next_widget",
            return_value=None,
        ), patch("client.client.PLAYBACK_FAILURE_RETRY_SEC", 0.25), patch("time.sleep", return_value=None) as sleep_mock:

            def _stop_after_retry(seconds):
                if abs(seconds - 0.25) < 1e-9:
                    controller._running = False
                return None

            sleep_mock.side_effect = _stop_after_retry
            controller._running = True
            controller._run()

        self.assertGreaterEqual(player.play_blocking.call_count, 1)
        sleep_mock.assert_any_call(0.25)

    def test_stopped_worker_skips_retry_and_prewarm_after_failed_widget_playback(self):
        from client.client import PlaybackController

        controller = PlaybackController(_FakeGuiRuntime())
        player = unittest.mock.Mock()
        player.image_duration_sec = 8
        player.is_direct_url_widget.return_value = False
        player.update_widget_layout.return_value = False
        player.last_play_was_interrupted.return_value = False
        player._is_video.return_value = False
        controller.player = player

        widget_item = {
            "item_type": "widget",
            "duration_sec": 5,
            "widget_payload": {"type": "url", "content": "https://example.com/one"},
        }

        with patch.object(controller, "_effective_playlist", return_value=[widget_item]), patch.object(
            controller,
            "_restore_or_init_runtime_state",
            return_value={"index": 0, "resume_sec": 0},
        ), patch.object(controller, "_persist_playback_state", return_value=None), patch.object(
            controller,
            "_prewarm_next_widget",
            return_value=None,
        ) as prewarm_mock, patch("client.client.PLAYBACK_FAILURE_RETRY_SEC", 0.25), patch(
            "time.sleep",
            return_value=None,
        ) as sleep_mock:
            controller._running = True

            def _stop_worker_after_first_layout(*_args, **_kwargs):
                controller._running = False
                return False

            player.update_widget_layout.side_effect = _stop_worker_after_first_layout
            controller._run()

        sleep_mock.assert_not_called()
        prewarm_mock.assert_not_called()

    def test_sync_in_background_keeps_playback_running_when_media_exists(self):
        from client.client import PlaybackController

        controller = PlaybackController(_FakeGuiRuntime())
        player = unittest.mock.Mock()
        player.image_duration_sec = 8
        player.is_image.return_value = False
        player._is_video.return_value = True
        player.last_play_was_interrupted.return_value = False

        def _single_playback(*args, **kwargs):
            controller._running = False
            return True

        player.play_blocking.side_effect = _single_playback
        controller.player = player
        controller.overlay = unittest.mock.Mock()
        controller.overlay.is_active.return_value = False
        controller._sync_in_progress = True

        with patch.object(
            controller,
            "_effective_playlist",
            return_value=[{"local_path": "/tmp/a.mp4", "duration_sec": None, "media_type": "video"}],
        ), patch.object(
            controller,
            "_restore_or_init_runtime_state",
            return_value={"index": 0, "resume_sec": 0},
        ), patch.object(controller, "_persist_playback_state", return_value=None), patch("time.sleep", return_value=None):
            controller._running = True
            controller._run()

        player.stop.assert_not_called()
        controller.overlay.show.assert_not_called()

    def test_sync_overlay_shown_when_no_media_available(self):
        from client.client import PlaybackController

        controller = PlaybackController(_FakeGuiRuntime())
        controller.player = unittest.mock.Mock()
        controller.overlay = unittest.mock.Mock()
        controller.overlay.is_active.return_value = False
        controller._sync_in_progress = True

        def _stop_wait(_seconds):
            controller._running = False
            return None

        with patch.object(controller, "_effective_playlist", return_value=[]), patch(
            "time.sleep", side_effect=_stop_wait
        ):
            controller._running = True
            controller._run()

        controller.player.stop.assert_called_once()
        controller.overlay.show.assert_called_once()

    def test_playback_controller_stop_stops_multi_monitor_before_widget_player(self):
        from client.client import PlaybackController

        controller = PlaybackController(_FakeGuiRuntime())
        controller.player = unittest.mock.Mock()
        controller.multi_monitor_playback = unittest.mock.Mock()
        call_order: list[str] = []
        controller.multi_monitor_playback.stop.side_effect = lambda: call_order.append("multi_monitor_stop")
        controller.player.stop.side_effect = lambda **_kwargs: call_order.append("player_stop")

        controller.stop(stop_widget_runtime=False)

        self.assertEqual(call_order, ["multi_monitor_stop", "player_stop"])
        controller.player.stop.assert_called_once_with(stop_widget_runtime=False)

    def test_playback_controller_pause_stops_multi_monitor_before_widget_player(self):
        from client.client import PlaybackController

        controller = PlaybackController(_FakeGuiRuntime())
        controller.player = unittest.mock.Mock()
        controller.multi_monitor_playback = unittest.mock.Mock()
        controller._active_item = None
        controller._active_item_started_at = None
        call_order: list[str] = []
        controller.multi_monitor_playback.pause.side_effect = lambda: call_order.append("multi_monitor_pause")
        controller.player.stop.side_effect = lambda: call_order.append("player_stop")

        controller.pause()

        self.assertEqual(call_order, ["multi_monitor_pause", "player_stop"])
        controller.player.stop.assert_called_once_with()


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

    def test_resolve_widget_launch_options_forces_clone_path_when_requested(self):
        from client.client import _resolve_widget_launch_options

        with patch("client.client.os.name", "nt"):
            target_monitor_index, clone_enabled = _resolve_widget_launch_options(
                clone_requested=True,
                requested_target_monitor_index=0,
                has_multiple_monitors=True,
            )

        self.assertIsNone(target_monitor_index)
        self.assertTrue(clone_enabled)

    def test_resolve_widget_launch_options_keeps_target_when_clone_disabled(self):
        from client.client import _resolve_widget_launch_options

        with patch("client.client.os.name", "nt"):
            target_monitor_index, clone_enabled = _resolve_widget_launch_options(
                clone_requested=False,
                requested_target_monitor_index=1,
                has_multiple_monitors=True,
            )

        self.assertEqual(target_monitor_index, 1)
        self.assertFalse(clone_enabled)

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

    def test_update_from_config_interrupts_transient_fallback_when_playlist_ready(self):
        from client.client import PlaybackController

        controller = PlaybackController(_FakeGuiRuntime())
        player = unittest.mock.Mock()
        player.image_duration_sec = 8
        controller.player = player

        controller._fallback_only_mode = False
        controller._transient_fallback_active = True
        controller._version = "ver-old"
        controller._playlist_entries = []

        with patch.object(
            controller.media_manager,
            "sync_playlist_entries",
            return_value=[{"local_path": "/tmp/a.mp4", "duration_sec": None, "media_type": "video"}],
        ):
            controller.update_from_config(
                {
                    "enabled": True,
                    "videos": [{"path": "https://example.com/a.mp4", "media_type": "video", "duration_sec": None}],
                    "playlist_version": "ver-new",
                    "media_signatures": {},
                    "loop_mode": "sequential",
                }
            )

        player.stop.assert_called_once()
        self.assertFalse(controller._transient_fallback_active)
        self.assertEqual(controller._version, "ver-new")
        self.assertEqual(len(controller._playlist_entries), 1)

    def test_update_from_config_prefers_top_level_payload_when_monitor1_playlist_is_empty(self):
        from client.client import PlaybackController

        controller = PlaybackController(_FakeGuiRuntime())
        controller.multi_monitor_playback = unittest.mock.Mock()
        controller.player = unittest.mock.Mock()

        with patch.object(
            controller.media_manager,
            "sync_playlist_entries",
            return_value=[{"local_path": "/tmp/widget"}],
        ) as sync_mock:
            controller.update_from_config(
                {
                    "enabled": True,
                    "videos": [{"item_type": "widget", "widget_url": "https://example.com/widget"}],
                    "playlist_version": "v-main",
                    "media_signatures": {},
                    "loop_mode": "sequential",
                    "monitor_playlists": {
                        "1": {
                            "enabled": False,
                            "videos": [],
                            "playlist_version": "v-empty",
                            "media_signatures": {},
                            "loop_mode": "sequential",
                        }
                    },
                }
            )

        self.assertFalse(controller._fallback_only_mode)
        self.assertEqual(controller._version, "v-main")
        sync_mock.assert_called_once()
        self.assertEqual(
            sync_mock.call_args.args[1],
            "v-main",
        )

    def test_multi_monitor_playback_detects_active_playlist_on_third_monitor(self):
        from client.client import MultiMonitorPlayback

        media_manager = unittest.mock.Mock()
        media_manager.sync_playlist_entries.return_value = [
            {"local_path": "/tmp/m3.mp4", "duration_sec": None, "media_type": "video"}
        ]
        multi_monitor = MultiMonitorPlayback(media_manager)

        with patch("client.client.os.name", "posix"):
            multi_monitor.update_from_config(
                {
                    "1": {"enabled": True, "videos": []},
                    "3": {
                        "enabled": True,
                        "videos": [{"path": "https://example.com/m3.mp4", "media_type": "video"}],
                        "playlist_version": "v3",
                        "media_signatures": {},
                        "loop_mode": "sequential",
                    },
                }
            )

        self.assertTrue(multi_monitor.has_active_playlist())
        media_manager.sync_playlist_entries.assert_called_once()
        cache_key = media_manager.sync_playlist_entries.call_args.args[1]
        self.assertEqual(cache_key, "monitor3-v3")

    def test_multi_monitor_playback_ignores_unplugged_monitor_playlists_on_windows(self):
        from client.client import MultiMonitorPlayback

        media_manager = unittest.mock.Mock()
        media_manager.sync_playlist_entries.return_value = [
            {"local_path": "/tmp/m3.mp4", "duration_sec": None, "media_type": "video"}
        ]
        multi_monitor = MultiMonitorPlayback(media_manager)

        with patch("client.client.os.name", "nt"), patch.object(
            MultiMonitorPlayback,
            "_connected_monitor_count",
            return_value=2,
        ):
            multi_monitor.update_from_config(
                {
                    "3": {
                        "enabled": True,
                        "videos": [{"path": "https://example.com/m3.mp4", "media_type": "video"}],
                        "playlist_version": "v3",
                        "media_signatures": {},
                        "loop_mode": "sequential",
                    },
                }
            )

        self.assertFalse(multi_monitor.has_active_playlist())
        media_manager.sync_playlist_entries.assert_not_called()

    def test_multi_monitor_playback_ignores_secondary_playlist_when_only_one_monitor_connected(self):
        from client.client import MultiMonitorPlayback

        media_manager = unittest.mock.Mock()
        media_manager.sync_playlist_entries.return_value = [
            {"local_path": "/tmp/m2.mp4", "duration_sec": None, "media_type": "video"}
        ]
        multi_monitor = MultiMonitorPlayback(media_manager)

        with patch("client.client.os.name", "nt"), patch.object(
            MultiMonitorPlayback,
            "_connected_monitor_count",
            return_value=1,
        ):
            multi_monitor.update_from_config(
                {
                    "2": {
                        "enabled": True,
                        "videos": [{"path": "https://example.com/m2.mp4", "media_type": "video"}],
                        "playlist_version": "v2",
                        "media_signatures": {},
                        "loop_mode": "sequential",
                    },
                }
            )

        self.assertFalse(multi_monitor.has_active_playlist())
        media_manager.sync_playlist_entries.assert_not_called()

    def test_multi_monitor_playback_keeps_second_monitor_when_two_monitors_connected(self):
        from client.client import MultiMonitorPlayback

        media_manager = unittest.mock.Mock()
        media_manager.sync_playlist_entries.return_value = [
            {"local_path": "/tmp/m2.mp4", "duration_sec": None, "media_type": "video"}
        ]
        multi_monitor = MultiMonitorPlayback(media_manager)

        with patch("client.client.os.name", "nt"), patch.object(
            MultiMonitorPlayback,
            "_connected_monitor_count",
            return_value=2,
        ):
            multi_monitor.update_from_config(
                {
                    "2": {
                        "enabled": True,
                        "videos": [{"path": "https://example.com/m2.mp4", "media_type": "video"}],
                        "playlist_version": "v2",
                        "media_signatures": {},
                        "loop_mode": "sequential",
                    },
                    "3": {
                        "enabled": True,
                        "videos": [{"path": "https://example.com/m3.mp4", "media_type": "video"}],
                        "playlist_version": "v3",
                        "media_signatures": {},
                        "loop_mode": "sequential",
                    },
                }
            )

        self.assertTrue(multi_monitor.has_active_playlist())
        media_manager.sync_playlist_entries.assert_called_once()
        self.assertEqual(media_manager.sync_playlist_entries.call_args.args[1], "monitor2-v2")

    def test_multi_monitor_playback_windows_uses_windows_display_ids_for_secondary_selection(self):
        from client.client import MultiMonitorPlayback

        media_manager = unittest.mock.Mock()
        media_manager.sync_playlist_entries.return_value = [{"local_path": "/tmp/m1.mp4", "duration_sec": None, "media_type": "video"}]
        multi_monitor = MultiMonitorPlayback(media_manager)

        with patch("client.client.os.name", "nt"), patch.object(
            MultiMonitorPlayback,
            "_windows_monitor_id_to_index_map",
            return_value={1: 1, 2: 0},
        ), patch.object(
            MultiMonitorPlayback,
            "_windows_primary_monitor_id",
            return_value=2,
        ), patch.object(
            MultiMonitorPlayback,
            "_connected_monitor_count",
            return_value=2,
        ):
            multi_monitor.update_from_config(
                {
                    "1": {
                        "enabled": True,
                        "videos": [{"path": "https://example.com/m1.mp4", "media_type": "video"}],
                        "playlist_version": "v1",
                        "media_signatures": {},
                        "loop_mode": "sequential",
                    },
                    "2": {
                        "enabled": True,
                        "videos": [{"path": "https://example.com/m2.mp4", "media_type": "video"}],
                        "playlist_version": "v2",
                        "media_signatures": {},
                        "loop_mode": "sequential",
                    },
                }
            )

        self.assertTrue(multi_monitor.has_active_playlist())
        media_manager.sync_playlist_entries.assert_called_once()
        self.assertEqual(media_manager.sync_playlist_entries.call_args.args[1], "monitor1-v1")


    def test_multi_monitor_playback_target_monitor_index_falls_back_when_windows_map_empty(self):
        from client.client import MultiMonitorPlayback

        with patch("client.client.os.name", "nt"), patch.object(
            MultiMonitorPlayback,
            "_windows_monitor_id_to_index_map",
            return_value={},
        ):
            self.assertEqual(MultiMonitorPlayback._target_monitor_index_for_monitor_no(2), 1)

    def test_multi_monitor_playback_target_monitor_index_uses_windows_monitor_id_mapping(self):
        from client.client import MultiMonitorPlayback

        with patch("client.client.os.name", "nt"), patch.dict("client.client.os.environ", {"MONITOR_PLAYLIST_USE_WINDOWS_DISPLAY_IDS": "1"}, clear=False), patch.object(
            MultiMonitorPlayback,
            "_windows_monitor_id_to_index_map",
            return_value={1: 2, 2: 0, 3: 1},
        ):
            self.assertEqual(MultiMonitorPlayback._target_monitor_index_for_monitor_no(3), 1)

    def test_multi_monitor_playback_target_monitor_index_defaults_to_positional_mapping(self):
        from client.client import MultiMonitorPlayback

        with patch("client.client.os.name", "nt"), patch.dict("client.client.os.environ", {}, clear=True), patch.object(
            MultiMonitorPlayback,
            "_windows_monitor_id_to_index_map",
            return_value={1: 2, 2: 0, 3: 1},
        ):
            self.assertEqual(MultiMonitorPlayback._target_monitor_index_for_monitor_no(3), 2)

    def test_multi_monitor_playback_target_monitor_index_can_disable_windows_monitor_mapping(self):
        from client.client import MultiMonitorPlayback

        with patch("client.client.os.name", "nt"), patch.dict("client.client.os.environ", {"MONITOR_PLAYLIST_USE_WINDOWS_DISPLAY_IDS": "0"}, clear=False), patch.object(
            MultiMonitorPlayback,
            "_windows_monitor_id_to_index_map",
            return_value={1: 2, 2: 0, 3: 1},
        ):
            self.assertEqual(MultiMonitorPlayback._target_monitor_index_for_monitor_no(3), 2)

    def test_windows_monitor_id_map_normalizer_compacts_sparse_ids(self):
        from client.player import BorderlessFullscreenPlayer

        id_to_index = BorderlessFullscreenPlayer._normalize_windows_monitor_id_entries(
            [
                (0, 0, 1920, 1080, 1, True),
                (1920, 0, 1920, 1080, 3, False),
            ]
        )
        self.assertEqual(id_to_index, {1: 0, 2: 1})

    def test_windows_monitor_id_map_normalizer_keeps_dense_ids(self):
        from client.player import BorderlessFullscreenPlayer

        id_to_index = BorderlessFullscreenPlayer._normalize_windows_monitor_id_entries(
            [
                (0, 0, 1920, 1080, 1, True),
                (1920, 0, 1920, 1080, 2, False),
            ]
        )
        self.assertEqual(id_to_index, {1: 0, 2: 1})

    def test_windows_display_id_map_sanitizer_rejects_sparse_or_huge_ids(self):
        from client.player import BorderlessFullscreenPlayer

        self.assertEqual(
            BorderlessFullscreenPlayer._sanitize_windows_display_id_map(
                {"\\\\.\\DISPLAY1": 8388689, "\\\\.\\DISPLAY2": 4146, "\\\\.\\DISPLAY3": 24647}
            ),
            {},
        )
        self.assertEqual(
            BorderlessFullscreenPlayer._sanitize_windows_display_id_map(
                {"\\\\.\\DISPLAY1": 1, "\\\\.\\DISPLAY2": 3}
            ),
            {},
        )

    def test_windows_display_id_map_sanitizer_accepts_compact_ids(self):
        from client.player import BorderlessFullscreenPlayer

        self.assertEqual(
            BorderlessFullscreenPlayer._sanitize_windows_display_id_map(
                {"\\\\.\\DISPLAY1": 1, "\\\\.\\DISPLAY2": 2, "\\\\.\\DISPLAY3": 3}
            ),
            {"\\\\.\\DISPLAY1": 1, "\\\\.\\DISPLAY2": 2, "\\\\.\\DISPLAY3": 3},
        )

    def test_multi_monitor_playback_accepts_widget_only_playlist(self):
        from client.client import MultiMonitorPlayback

        media_manager = unittest.mock.Mock()
        media_manager.sync_playlist_entries.return_value = []
        multi_monitor = MultiMonitorPlayback(media_manager)

        with patch("client.client.os.name", "posix"):
            multi_monitor.update_from_config(
                {
                    "2": {
                        "enabled": True,
                        "videos": [
                            {
                                "item_type": "widget",
                                "widget_url": "https://example.com/widget",
                                "duration_sec": 20,
                            }
                        ],
                        "playlist_version": "widget-v2",
                        "media_signatures": {},
                        "loop_mode": "sequential",
                    }
                }
            )

        self.assertTrue(multi_monitor.has_active_playlist())
        state = multi_monitor._monitor_states[2]
        self.assertEqual(len(state["entries"]), 1)
        self.assertEqual(state["entries"][0]["item_type"], "widget")

    def test_multi_monitor_reconciles_widget_runtime_players_to_widget_monitor_count(self):
        from client.client import MultiMonitorPlayback

        media_manager = unittest.mock.Mock()
        media_manager.sync_playlist_entries.return_value = []

        monitor2_player = unittest.mock.Mock()
        monitor3_player = unittest.mock.Mock()
        created_players = [monitor2_player, monitor3_player]

        with patch("client.client.os.name", "posix"), patch(
            "client.client.BorderlessFullscreenPlayer",
            side_effect=created_players,
        ) as player_cls:
            multi_monitor = MultiMonitorPlayback(media_manager)
            multi_monitor.update_from_config(
                {
                    "2": {
                        "enabled": True,
                        "videos": [{"item_type": "widget", "widget_url": "https://example.com/w2", "duration_sec": 15}],
                        "playlist_version": "w2",
                        "loop_mode": "sequential",
                    },
                    "3": {
                        "enabled": True,
                        "videos": [{"item_type": "widget", "widget_url": "https://example.com/w3", "duration_sec": 15}],
                        "playlist_version": "w3",
                        "loop_mode": "sequential",
                    },
                }
            )

            self.assertEqual(set(multi_monitor._widget_players.keys()), {2, 3})
            self.assertEqual(player_cls.call_count, 2)
            monitor2_player.start_widget_engine_if_needed.assert_not_called()
            monitor3_player.start_widget_engine_if_needed.assert_not_called()

            multi_monitor.update_from_config(
                {
                    "2": {
                        "enabled": True,
                        "videos": [{"path": "https://example.com/v2.mp4", "media_type": "video"}],
                        "playlist_version": "m2",
                        "loop_mode": "sequential",
                    },
                    "3": {
                        "enabled": True,
                        "videos": [{"item_type": "widget", "widget_url": "https://example.com/w3", "duration_sec": 15}],
                        "playlist_version": "w3",
                        "loop_mode": "sequential",
                    },
                }
            )

        self.assertEqual(set(multi_monitor._widget_players.keys()), {3})
        monitor2_player.stop.assert_called_with(stop_widget_runtime=True)

    def test_multi_monitor_reconcile_defers_visible_widget_runtime_until_idle(self):
        from client.client import MultiMonitorPlayback

        media_manager = unittest.mock.Mock()
        media_manager.sync_playlist_entries.return_value = []

        monitor2_player = unittest.mock.Mock()
        monitor2_player._active_item = {"item_type": "widget", "widget_url": "https://example.com/w2"}
        monitor2_player.start_widget_engine_if_needed.return_value = True

        with patch("client.client.os.name", "posix"), patch(
            "client.client.BorderlessFullscreenPlayer",
            return_value=monitor2_player,
        ):
            multi_monitor = MultiMonitorPlayback(media_manager)
            multi_monitor.update_from_config(
                {
                    "2": {
                        "enabled": True,
                        "videos": [{"item_type": "widget", "widget_url": "https://example.com/w2", "duration_sec": 15}],
                        "playlist_version": "w2",
                        "loop_mode": "sequential",
                    },
                }
            )

        monitor2_player.start_widget_engine_if_needed.assert_not_called()
        monitor2_player.background_widget_engine.assert_not_called()

    def test_multi_monitor_playback_defaults_enabled_to_true_when_flag_missing(self):
        from client.client import MultiMonitorPlayback

        media_manager = unittest.mock.Mock()
        media_manager.sync_playlist_entries.return_value = [
            {"local_path": "/tmp/m2.mp4", "duration_sec": None, "media_type": "video"}
        ]
        multi_monitor = MultiMonitorPlayback(media_manager)

        with patch("client.client.os.name", "posix"):
            multi_monitor.update_from_config(
                {
                    "2": {
                        "videos": [{"path": "https://example.com/m2.mp4", "media_type": "video"}],
                        "playlist_version": "v2",
                        "media_signatures": {},
                        "loop_mode": "sequential",
                    }
                }
            )

        self.assertTrue(multi_monitor.has_active_playlist())
        media_manager.sync_playlist_entries.assert_called_once()

    def test_multi_monitor_worker_creates_secondary_player_without_warm_runtime(self):
        from client.client import MultiMonitorPlayback

        media_manager = unittest.mock.Mock()
        multi_monitor = MultiMonitorPlayback(media_manager)
        multi_monitor._running_monitors[2] = True
        multi_monitor._monitor_states[2] = {
            "enabled": True,
            "entries": [
                {
                    "item_type": "widget",
                    "widget_url": "https://example.com/widget",
                    "duration_sec": 12,
                }
            ],
            "loop_mode": "sequential",
        }

        mock_player = unittest.mock.Mock()

        def _stop_after_first_play(*_args, **_kwargs):
            multi_monitor._running_monitors[2] = False
            return True

        mock_player.play_widget_blocking.side_effect = _stop_after_first_play

        with patch("client.client.BorderlessFullscreenPlayer", return_value=mock_player) as player_cls, patch("time.sleep", return_value=None):
            multi_monitor._run(2)

        player_cls.assert_called_once_with(keep_widget_runtime_warm=False)
        mock_player.play_widget_blocking.assert_called_once()

    def test_multi_monitor_worker_uses_injected_widget_player_without_spawning_new_one(self):
        from client.client import MultiMonitorPlayback

        media_manager = unittest.mock.Mock()
        injected_widget_player = unittest.mock.Mock()
        multi_monitor = MultiMonitorPlayback(media_manager, widget_player=injected_widget_player)
        multi_monitor._running_monitors[2] = True
        multi_monitor._monitor_states[2] = {
            "enabled": True,
            "entries": [
                {
                    "item_type": "widget",
                    "widget_url": "https://example.com/widget",
                    "duration_sec": 12,
                }
            ],
            "loop_mode": "sequential",
        }

        def _stop_after_first_play(*_args, **_kwargs):
            multi_monitor._running_monitors[2] = False
            return True

        injected_widget_player.play_widget_blocking.side_effect = _stop_after_first_play
        with patch("client.client.BorderlessFullscreenPlayer") as player_cls, patch("time.sleep", return_value=None):
            multi_monitor._run(2)

        player_cls.assert_not_called()
        injected_widget_player.play_widget_blocking.assert_called_once()

    def test_multi_monitor_worker_plays_widget_on_target_monitor(self):
        from client.client import MultiMonitorPlayback

        media_manager = unittest.mock.Mock()
        mock_player = unittest.mock.Mock()
        multi_monitor = MultiMonitorPlayback(media_manager, widget_player=mock_player)
        multi_monitor._running_monitors[2] = True
        multi_monitor._monitor_states[2] = {
            "enabled": True,
            "entries": [
                {
                    "item_type": "widget",
                    "widget_url": "https://example.com/widget",
                    "duration_sec": 12,
                }
            ],
            "loop_mode": "sequential",
        }

        def _stop_after_first_play(*_args, **_kwargs):
            multi_monitor._running_monitors[2] = False
            return True

        mock_player.play_widget_blocking.side_effect = _stop_after_first_play
        with patch("time.sleep", return_value=None):
            multi_monitor._run(2)

        mock_player.play_widget_blocking.assert_called_once_with(
            "https://example.com/widget",
            12,
            widget_config=None,
            target_monitor_index=1,
            clone_to_all_monitors=False,
        )

    def test_multi_monitor_worker_normalizes_list_widget_payload(self):
        from client.client import MultiMonitorPlayback

        media_manager = unittest.mock.Mock()
        mock_player = unittest.mock.Mock()
        multi_monitor = MultiMonitorPlayback(media_manager, widget_player=mock_player)
        multi_monitor._running_monitors[2] = True
        multi_monitor._monitor_states[2] = {
            "enabled": True,
            "entries": [
                {
                    "item_type": "widget",
                    "widget_url": "",
                    "widget_payload": [{"type": "iframe", "url": "https://example.com/widget"}],
                    "duration_sec": 12,
                }
            ],
            "loop_mode": "sequential",
        }

        def _stop_after_first_play(*_args, **_kwargs):
            multi_monitor._running_monitors[2] = False
            return True

        mock_player.play_widget_blocking.side_effect = _stop_after_first_play
        with patch("time.sleep", return_value=None):
            multi_monitor._run(2)

        mock_player.play_widget_blocking.assert_called_once_with(
            "",
            12,
            widget_config={"widgets": [{"type": "iframe", "url": "https://example.com/widget"}]},
            target_monitor_index=1,
            clone_to_all_monitors=False,
        )

    def test_multi_monitor_worker_wraps_single_widget_payload_dict(self):
        from client.client import MultiMonitorPlayback

        media_manager = unittest.mock.Mock()
        mock_player = unittest.mock.Mock()
        multi_monitor = MultiMonitorPlayback(media_manager, widget_player=mock_player)
        multi_monitor._running_monitors[2] = True
        multi_monitor._monitor_states[2] = {
            "enabled": True,
            "entries": [
                {
                    "item_type": "widget",
                    "widget_url": "",
                    "widget_payload": {"type": "html", "content": "<b>duyuru</b>"},
                    "duration_sec": 12,
                }
            ],
            "loop_mode": "sequential",
        }

        def _stop_after_first_play(*_args, **_kwargs):
            multi_monitor._running_monitors[2] = False
            return True

        mock_player.play_widget_blocking.side_effect = _stop_after_first_play
        with patch("time.sleep", return_value=None):
            multi_monitor._run(2)

        mock_player.play_widget_blocking.assert_called_once_with(
            "",
            12,
            widget_config={"widgets": [{"type": "html", "content": "<b>duyuru</b>"}]},
            target_monitor_index=1,
            clone_to_all_monitors=False,
        )

    def test_multi_monitor_pause_terminates_widget_runtime(self):
        from client.client import MultiMonitorPlayback

        media_manager = unittest.mock.Mock()
        multi_monitor = MultiMonitorPlayback(media_manager)
        media_player = unittest.mock.Mock()
        widget_player = unittest.mock.Mock()

        multi_monitor._players[1] = media_player
        multi_monitor._widget_players[2] = widget_player

        multi_monitor.pause()

        media_player.stop.assert_called_once_with()
        widget_player.stop.assert_called_once_with(stop_widget_runtime=True)

    def test_multi_monitor_stop_terminates_widget_runtime(self):
        from client.client import MultiMonitorPlayback

        media_manager = unittest.mock.Mock()
        multi_monitor = MultiMonitorPlayback(media_manager)
        media_player = unittest.mock.Mock()
        widget_player = unittest.mock.Mock()

        multi_monitor._running_monitors[1] = True
        multi_monitor._players[1] = media_player
        multi_monitor._widget_players[1] = widget_player

        multi_monitor.stop()

        media_player.stop.assert_called_once_with()
        widget_player.stop.assert_called_once_with(stop_widget_runtime=True)

    def test_multi_monitor_stop_visits_widget_only_monitors(self):
        from client.client import MultiMonitorPlayback

        media_manager = unittest.mock.Mock()
        multi_monitor = MultiMonitorPlayback(media_manager)
        widget_player = unittest.mock.Mock()

        multi_monitor._widget_players[3] = widget_player

        multi_monitor.stop()

        widget_player.stop.assert_called_once_with(stop_widget_runtime=True)

    def test_reconcile_widget_players_stops_stale_widget_runtimes_for_non_widget_monitor(self):
        from client.client import MultiMonitorPlayback

        media_manager = unittest.mock.Mock()
        multi_monitor = MultiMonitorPlayback(media_manager)
        stale_widget_player = unittest.mock.Mock()
        stale_widget_player.stop = unittest.mock.Mock()
        multi_monitor._widget_players[2] = stale_widget_player

        # Monitör konfigürasyonda var ama widget içermiyor -> stale sayılmalı.
        multi_monitor._reconcile_widget_players_for_states(
            {
                2: {
                    "enabled": True,
                    "entries": [{"item_type": "media", "local_path": "/tmp/demo.mp4"}],
                    "target_monitor_index": 1,
                }
            }
        )

        stale_widget_player.stop.assert_called_once_with(stop_widget_runtime=True)
        self.assertNotIn(2, multi_monitor._widget_players)

    def test_reconcile_widget_players_keeps_runtime_when_monitor_temporarily_missing(self):
        from client.client import MultiMonitorPlayback

        media_manager = unittest.mock.Mock()
        multi_monitor = MultiMonitorPlayback(media_manager)
        widget_player = unittest.mock.Mock()
        widget_player.stop = unittest.mock.Mock()
        multi_monitor._widget_players[2] = widget_player

        # Konfigürasyon geçici olarak boş gelirse runtime zorla kapatılmamalı.
        multi_monitor._reconcile_widget_players_for_states({})

        widget_player.stop.assert_not_called()
        self.assertIn(2, multi_monitor._widget_players)

    def test_update_from_config_disables_clone_when_monitor_four_has_playlist(self):
        from client.client import PlaybackController

        controller = PlaybackController(_FakeGuiRuntime())
        controller.media_manager = unittest.mock.Mock()
        controller.media_manager.sync_playlist_entries.return_value = []
        controller.media_manager.load_last_successful_playlist_entries.return_value = []
        controller.multi_monitor_playback = unittest.mock.Mock()
        controller.multi_monitor_playback.has_active_playlist.return_value = True

        controller.update_from_config(
            {
                "enabled": True,
                "videos": [],
                "playlist_version": "v1",
                "monitor_playlists": {
                    "1": {"enabled": True, "videos": []},
                    "4": {
                        "enabled": True,
                        "videos": [{"path": "https://example.com/m4.mp4", "media_type": "video"}],
                        "playlist_version": "v4",
                    },
                },
            }
        )

        controller.multi_monitor_playback.update_from_config.assert_called_once()
        self.assertFalse(controller._clone_to_all_monitors)

    def test_update_from_config_keeps_clone_disabled_when_no_monitor_playlist(self):
        from client.client import PlaybackController

        controller = PlaybackController(_FakeGuiRuntime())
        controller.media_manager = unittest.mock.Mock()
        controller.media_manager.sync_playlist_entries.return_value = []
        controller.media_manager.load_last_successful_playlist_entries.return_value = []
        controller.multi_monitor_playback = unittest.mock.Mock()
        controller.multi_monitor_playback.has_active_playlist.return_value = False

        controller.update_from_config(
            {
                "enabled": True,
                "videos": [],
                "playlist_version": "v1",
                "monitor_playlists": {},
            }
        )

        controller.multi_monitor_playback.update_from_config.assert_called_once()
        self.assertFalse(controller._clone_to_all_monitors)


if __name__ == "__main__":
    unittest.main()
