import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
import os
import tempfile
from unittest.mock import Mock, patch

import client.client as main


class TestRunStateCycle(unittest.TestCase):
    def setUp(self):
        self.orig_state = main.current_state
        self.orig_idle_mode = main.idle_mode_enabled
        self.orig_content_enabled = main.content_enabled
        self.orig_idle_timeout = main.idle_timeout_sec
        self.orig_playing_started_at = main.playing_started_at
        self.orig_emergency_active = main.emergency_active
        self.orig_last_observed_idle_sec = main._last_observed_idle_sec
        self.orig_activity_drop_streak = main._activity_drop_streak
        self.orig_low_idle_streak = main._low_idle_streak
        self.orig_low_idle_activity_enabled = main.LOW_IDLE_ACTIVITY_ENABLED
        self.orig_connection_outage_active = main.connection_outage_active

    def tearDown(self):
        main.current_state = self.orig_state
        main.idle_mode_enabled = self.orig_idle_mode
        main.content_enabled = self.orig_content_enabled
        main.idle_timeout_sec = self.orig_idle_timeout
        main.playing_started_at = self.orig_playing_started_at
        main.emergency_active = self.orig_emergency_active
        main._last_observed_idle_sec = self.orig_last_observed_idle_sec
        main._activity_drop_streak = self.orig_activity_drop_streak
        main._low_idle_streak = self.orig_low_idle_streak
        main.LOW_IDLE_ACTIVITY_ENABLED = self.orig_low_idle_activity_enabled
        main.connection_outage_active = self.orig_connection_outage_active

    def _configure_common(self):
        main.idle_mode_enabled = True
        main.content_enabled = True
        main.emergency_active = False
        main.idle_timeout_sec = 60
        main._last_observed_idle_sec = None
        main._activity_drop_streak = 0
        main._low_idle_streak = 0
        main.LOW_IDLE_ACTIVITY_ENABLED = True
        main.connection_outage_active = False

    def test_idle_pending_waits_for_selected_content_before_playing_state(self):
        self._configure_common()
        main.current_state = main.ClientState.IDLE_PENDING

        fake_playback = Mock()
        fake_playback.current_content_name.return_value = ""
        fake_playback._active_item = None

        with patch.object(main, "playback", fake_playback), patch.object(main, "idle_background", Mock()), patch.object(
            main, "get_idle_seconds", return_value=65.0
        ):
            main.run_state_cycle()

        fake_playback.start.assert_called_once()
        self.assertEqual(main.current_state, main.ClientState.IDLE_PENDING)

    def test_idle_pending_transitions_to_playing_when_content_selected(self):
        self._configure_common()
        main.current_state = main.ClientState.IDLE_PENDING

        fake_playback = Mock()
        fake_playback.current_content_name.return_value = "video.mp4"
        fake_playback._active_item = None

        with patch.object(main, "playback", fake_playback), patch.object(main, "idle_background", Mock()), patch.object(
            main, "get_idle_seconds", return_value=65.0
        ), patch.object(main.time, "monotonic", return_value=100.0):
            main.run_state_cycle()

        fake_playback.start.assert_called_once()
        self.assertEqual(main.current_state, main.ClientState.PLAYING)

    def test_idle_pending_transitions_to_playing_when_widget_selected_without_display_name(self):
        self._configure_common()
        main.current_state = main.ClientState.IDLE_PENDING

        fake_playback = Mock()
        fake_playback.current_content_name.return_value = "https://example.com/widget"
        fake_playback._active_item = {"item_type": "widget", "widget_url": "https://example.com/widget"}

        with patch.object(main, "playback", fake_playback), patch.object(main, "idle_background", Mock()), patch.object(
            main, "get_idle_seconds", return_value=65.0
        ), patch.object(main.time, "monotonic", return_value=100.0):
            main.run_state_cycle()

        fake_playback.start.assert_called_once()
        self.assertEqual(main.current_state, main.ClientState.PLAYING)

    def test_idle_pending_transitions_to_playing_when_widget_selected_without_resolved_name(self):
        self._configure_common()
        main.current_state = main.ClientState.IDLE_PENDING

        fake_playback = Mock()
        fake_playback.current_content_name.return_value = ""
        fake_playback._active_item = {
            "item_type": "widget",
            "widget_payload": {"widgets": [{"type": "iframe", "url": "https://example.com"}]},
        }

        with patch.object(main, "playback", fake_playback), patch.object(main, "idle_background", Mock()), patch.object(
            main, "get_idle_seconds", return_value=65.0
        ), patch.object(main.time, "monotonic", return_value=100.0):
            main.run_state_cycle()

        fake_playback.start.assert_called_once()
        self.assertEqual(main.current_state, main.ClientState.PLAYING)

    def test_idle_pending_returns_active_when_activity_detected_before_content_selected(self):
        self._configure_common()
        main.current_state = main.ClientState.IDLE_PENDING
        main._last_observed_idle_sec = 80.0
        main._activity_drop_streak = max(main.ACTIVITY_DROP_CONFIRM_COUNT - 1, 0)

        fake_playback = Mock()
        fake_playback.current_content_name.return_value = ""
        fake_playback._active_item = None

        fake_idle_background = Mock()
        with patch.object(main, "playback", fake_playback), patch.object(main, "idle_background", fake_idle_background), patch.object(
            main, "get_idle_seconds", return_value=10.0
        ):
            main.run_state_cycle()

        fake_playback.start.assert_called_once()
        fake_playback.stop.assert_called_once_with(stop_widget_runtime=False)
        fake_playback.background_all_widget_viewers.assert_called_once()
        fake_idle_background.hide.assert_called_once()
        self.assertEqual(main.current_state, main.ClientState.ACTIVE)


    def test_playing_transitions_to_returning_when_idle_drops_even_above_resume_threshold(self):
        self._configure_common()
        main.current_state = main.ClientState.PLAYING
        main.playing_started_at = 0.0
        main._last_observed_idle_sec = 5.0
        main._activity_drop_streak = max(main.ACTIVITY_DROP_CONFIRM_COUNT - 1, 0)

        fake_playback = Mock()
        fake_playback.current_content_name.return_value = "video.mp4"
        fake_playback._active_item = {"item_type": "media"}

        with patch.object(main, "playback", fake_playback), patch.object(main, "idle_background", Mock()), patch.object(
            main, "get_idle_seconds", return_value=3.8
        ), patch.object(main.time, "monotonic", return_value=100.0), patch.object(main, "return_to_erp_window") as return_mock:
            main.run_state_cycle()

        return_mock.assert_called_once()
        self.assertGreaterEqual(fake_playback.stop.call_count, 1)
        self.assertEqual(fake_playback.stop.call_args_list[-1].kwargs.get("stop_widget_runtime"), False)
        fake_playback.background_all_widget_viewers.assert_called_once()
        self.assertEqual(main.current_state, main.ClientState.ACTIVE)

    def test_playing_hides_idle_overlay_when_selected_content_has_no_resolved_name(self):
        self._configure_common()
        main.current_state = main.ClientState.PLAYING
        main.playing_started_at = 90.0

        fake_playback = Mock()
        fake_playback.current_content_name.return_value = ""
        fake_playback._active_item = {"item_type": "media", "local_path": "C:/media/dashboard.mp4"}

        fake_idle_background = Mock()
        with patch.object(main, "playback", fake_playback), patch.object(main, "idle_background", fake_idle_background), patch.object(
            main, "get_idle_seconds", return_value=80.0
        ), patch.object(main.time, "monotonic", return_value=100.0):
            main.run_state_cycle()

        fake_idle_background.hide.assert_called_once()

    def test_playing_widget_returns_when_erp_not_foreground_and_activity_detected(self):
        self._configure_common()
        main.current_state = main.ClientState.PLAYING
        main.playing_started_at = 0.0
        main._last_observed_idle_sec = 20.0
        main._activity_drop_streak = max(main.ACTIVITY_DROP_CONFIRM_COUNT - 1, 0)

        fake_playback = Mock()
        fake_playback.current_content_name.return_value = "widget"
        fake_playback._active_item = {"item_type": "widget", "widget_url": "https://example.com"}

        with patch.object(main, "playback", fake_playback), patch.object(main, "idle_background", Mock()), patch.object(
            main, "get_idle_seconds", return_value=0.0
        ), patch.object(main.time, "monotonic", return_value=100.0), patch.object(
            main.window_manager, "is_window_foreground", return_value=False
        ), patch.object(main, "return_to_erp_window") as return_mock:
            main.run_state_cycle()

        return_mock.assert_called_once()
        self.assertGreaterEqual(fake_playback.stop.call_count, 1)
        self.assertEqual(fake_playback.stop.call_args_list[-1].kwargs.get("stop_widget_runtime"), False)
        self.assertEqual(main.current_state, main.ClientState.ACTIVE)

    def test_playing_widget_returns_when_erp_foreground_and_activity_detected(self):
        self._configure_common()
        main.current_state = main.ClientState.PLAYING
        main.playing_started_at = 0.0
        main._last_observed_idle_sec = 20.0
        main._activity_drop_streak = max(main.ACTIVITY_DROP_CONFIRM_COUNT - 1, 0)

        fake_playback = Mock()
        fake_playback.current_content_name.return_value = "widget"
        fake_playback._active_item = {"item_type": "widget", "widget_url": "https://example.com"}

        with patch.object(main, "playback", fake_playback), patch.object(main, "idle_background", Mock()), patch.object(
            main, "get_idle_seconds", return_value=0.0
        ), patch.object(main.time, "monotonic", return_value=100.0), patch.object(
            main.window_manager, "is_window_foreground", return_value=True
        ), patch.object(main, "return_to_erp_window") as return_mock:
            main.run_state_cycle()

        return_mock.assert_called_once()
        self.assertGreaterEqual(fake_playback.stop.call_count, 1)
        self.assertEqual(fake_playback.stop.call_args_list[-1].kwargs.get("stop_widget_runtime"), False)
        self.assertEqual(main.current_state, main.ClientState.ACTIVE)

    def test_playing_widget_does_not_return_during_activity_grace_window(self):
        self._configure_common()
        main.current_state = main.ClientState.PLAYING
        main.playing_started_at = 100.0
        main._last_observed_idle_sec = 20.0
        main._activity_drop_streak = max(main.ACTIVITY_DROP_CONFIRM_COUNT - 1, 0)

        fake_playback = Mock()
        fake_playback.current_content_name.return_value = "widget"
        fake_playback._active_item = {"item_type": "widget", "widget_url": "https://example.com"}

        with patch.object(main, "playback", fake_playback), patch.object(main, "idle_background", Mock()), patch.object(
            main, "get_idle_seconds", return_value=0.0
        ), patch.object(main.time, "monotonic", return_value=100.5), patch.object(
            main.window_manager, "is_window_foreground", return_value=False
        ), patch.object(main, "return_to_erp_window") as return_mock:
            main.run_state_cycle()

        return_mock.assert_not_called()
        fake_playback.stop.assert_not_called()
        self.assertEqual(main.current_state, main.ClientState.PLAYING)

    def test_playing_widget_does_not_return_on_low_idle_when_disabled(self):
        self._configure_common()
        main.current_state = main.ClientState.PLAYING
        main.playing_started_at = 0.0
        main._last_observed_idle_sec = 0.2
        main.LOW_IDLE_ACTIVITY_ENABLED = False

        fake_playback = Mock()
        fake_playback.current_content_name.return_value = "widget"
        fake_playback._active_item = {"item_type": "widget", "widget_url": "https://example.com"}

        with patch.object(main, "playback", fake_playback), patch.object(main, "idle_background", Mock()), patch.object(
            main, "get_idle_seconds", return_value=0.0
        ), patch.object(main.time, "monotonic", return_value=100.0), patch.object(
            main.window_manager, "is_window_foreground", return_value=False
        ), patch.object(main, "return_to_erp_window") as return_mock:
            main.run_state_cycle()

        return_mock.assert_not_called()
        fake_playback.stop.assert_not_called()
        self.assertEqual(main.current_state, main.ClientState.PLAYING)

    def test_playing_widget_ignores_low_idle_only_signal_without_drop_confirmation(self):
        self._configure_common()
        main.current_state = main.ClientState.PLAYING
        main.playing_started_at = 0.0
        main._last_observed_idle_sec = 0.01
        main._activity_drop_streak = 0
        main._low_idle_streak = 2

        fake_playback = Mock()
        fake_playback.current_content_name.return_value = "widget"
        fake_playback._active_item = {"item_type": "widget", "widget_url": "https://example.com"}

        with patch.object(main, "playback", fake_playback), patch.object(main, "idle_background", Mock()), patch.object(
            main, "get_idle_seconds", return_value=0.0
        ), patch.object(main.time, "monotonic", return_value=100.0), patch.object(
            main.window_manager, "is_window_foreground", return_value=False
        ), patch.object(main, "return_to_erp_window") as return_mock:
            main.run_state_cycle()

        return_mock.assert_not_called()
        fake_playback.stop.assert_not_called()
        self.assertEqual(main.current_state, main.ClientState.PLAYING)

    def test_playing_media_ignores_low_idle_only_signal_without_drop_confirmation(self):
        self._configure_common()
        main.current_state = main.ClientState.PLAYING
        main.playing_started_at = 0.0
        main._last_observed_idle_sec = 0.01
        main._activity_drop_streak = 0
        main._low_idle_streak = 2

        fake_playback = Mock()
        fake_playback.current_content_name.return_value = "video"
        fake_playback._active_item = {"item_type": "media", "local_path": "/tmp/video.mp4"}

        with patch.object(main, "playback", fake_playback), patch.object(main, "idle_background", Mock()), patch.object(
            main, "get_idle_seconds", return_value=0.0
        ), patch.object(main.time, "monotonic", return_value=100.0), patch.object(
            main, "return_to_erp_window"
        ) as return_mock:
            main.run_state_cycle()

        return_mock.assert_not_called()
        fake_playback.stop.assert_not_called()
        self.assertEqual(main.current_state, main.ClientState.PLAYING)

    def test_playing_widget_keeps_idle_overlay_for_warmup_window(self):
        self._configure_common()
        main.current_state = main.ClientState.PLAYING
        main.playing_started_at = 100.0

        fake_playback = Mock()
        fake_playback.current_content_name.return_value = "widget"
        fake_playback._active_item = {"item_type": "widget", "widget_url": "https://example.com"}

        fake_idle_background = Mock()
        with patch.object(main, "playback", fake_playback), patch.object(main, "idle_background", fake_idle_background), patch.object(
            main, "get_idle_seconds", return_value=80.0
        ), patch.object(main.time, "monotonic", return_value=100.3):
            main.run_state_cycle()
        fake_idle_background.hide.assert_not_called()

    def test_prewarm_all_monitors_disabled_by_default(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(main._prewarm_all_monitors_enabled())

    def test_prewarm_all_monitors_reads_env_toggle(self):
        with patch.dict(os.environ, {"WIDGET_PREWARM_ALL_MONITORS": "1"}, clear=True):
            self.assertTrue(main._prewarm_all_monitors_enabled())

    def test_active_state_skips_redundant_stop_when_nothing_is_playing(self):
        self._configure_common()
        main.current_state = main.ClientState.ACTIVE

        fake_playback = Mock()
        fake_playback.current_content_name.return_value = ""
        fake_playback._active_item = None
        fake_playback._process = None
        fake_playback._widget_process = None
        fake_playback._extra_processes = []

        with patch.object(main, "playback", fake_playback), patch.object(main, "idle_background", Mock()), patch.object(
            main, "get_idle_seconds", return_value=10.0
        ):
            main.run_state_cycle()

        fake_playback.stop.assert_not_called()

    def test_active_state_does_not_issue_redundant_stop_when_widget_process_is_running(self):
        self._configure_common()
        main.current_state = main.ClientState.ACTIVE

        fake_playback = Mock()
        fake_playback.current_content_name.return_value = ""
        fake_playback._active_item = None
        fake_playback._process = None
        fake_playback._widget_process = Mock()
        fake_playback._widget_process.poll.return_value = None
        fake_playback._extra_processes = []

        with patch.object(main, "playback", fake_playback), patch.object(main, "idle_background", Mock()), patch.object(
            main, "get_idle_seconds", return_value=10.0
        ):
            main.run_state_cycle()

        fake_playback.stop.assert_not_called()

    def test_outage_caps_idle_threshold_and_transitions_to_idle_pending(self):
        self._configure_common()
        main.current_state = main.ClientState.ACTIVE
        main.idle_timeout_sec = 120
        main.connection_outage_active = True

        fake_playback = Mock()
        fake_playback.current_content_name.return_value = ""
        fake_playback._active_item = None
        fake_idle_background = Mock()

        with patch.object(main, "playback", fake_playback), patch.object(main, "idle_background", fake_idle_background), patch.object(
            main, "get_idle_seconds", return_value=12.0
        ):
            main.run_state_cycle()

        self.assertEqual(main.current_state, main.ClientState.IDLE_PENDING)
        fake_idle_background.show.assert_called_once()

    def test_without_outage_uses_full_idle_threshold(self):
        self._configure_common()
        main.current_state = main.ClientState.ACTIVE
        main.idle_timeout_sec = 120
        main.connection_outage_active = False

        fake_playback = Mock()
        fake_playback.current_content_name.return_value = ""
        fake_playback._active_item = None
        fake_idle_background = Mock()

        with patch.object(main, "playback", fake_playback), patch.object(main, "idle_background", fake_idle_background), patch.object(
            main, "get_idle_seconds", return_value=12.0
        ):
            main.run_state_cycle()

        self.assertEqual(main.current_state, main.ClientState.ACTIVE)
        fake_idle_background.show.assert_not_called()


class TestRuntimeTmpCleanup(unittest.TestCase):
    def test_cleanup_runtime_tmp_dir_removes_only_stale_entries(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            stale_dir = root / "stale-dir"
            stale_dir.mkdir()
            stale_file = root / "stale-file.tmp"
            stale_file.write_text("x", encoding="utf-8")
            fresh_dir = root / "fresh-dir"
            fresh_dir.mkdir()

            now = datetime(2026, 3, 23, 12, 0, tzinfo=timezone.utc)
            stale_ts = (now - timedelta(hours=30)).timestamp()
            fresh_ts = (now - timedelta(hours=2)).timestamp()
            for entry in (stale_dir, stale_file):
                os.utime(entry, (stale_ts, stale_ts))
            os.utime(fresh_dir, (fresh_ts, fresh_ts))

            with patch.object(main, "log_info"), patch.object(main, "log_warning"):
                main.cleanup_runtime_tmp_dir(runtime_tmp_dir=root, max_age_hours=24, now_utc=now)

            self.assertFalse(stale_dir.exists())
            self.assertFalse(stale_file.exists())
            self.assertTrue(fresh_dir.exists())

    def test_cleanup_runtime_tmp_dir_keeps_active_runtime_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            active_dir = root / "active"
            active_dir.mkdir()
            stale_dir = root / "stale"
            stale_dir.mkdir()

            now = datetime(2026, 3, 23, 12, 0, tzinfo=timezone.utc)
            stale_ts = (now - timedelta(hours=48)).timestamp()
            os.utime(active_dir, (stale_ts, stale_ts))
            os.utime(stale_dir, (stale_ts, stale_ts))

            with patch.object(main, "log_info"), patch.object(main, "log_warning"), patch.object(
                main.sys, "frozen", True, create=True
            ), patch.object(main.sys, "_MEIPASS", str(active_dir), create=True):
                main.cleanup_runtime_tmp_dir(runtime_tmp_dir=root, max_age_hours=24, now_utc=now)

            self.assertTrue(active_dir.exists())
            self.assertFalse(stale_dir.exists())


if __name__ == "__main__":
    unittest.main()
