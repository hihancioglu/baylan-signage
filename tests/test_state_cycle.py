import unittest
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

    def _configure_common(self):
        main.idle_mode_enabled = True
        main.content_enabled = True
        main.emergency_active = False
        main.idle_timeout_sec = 60
        main._last_observed_idle_sec = None
        main._activity_drop_streak = 0
        main._low_idle_streak = 0

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
        fake_idle_background.hide.assert_called_once()
        self.assertEqual(main.current_state, main.ClientState.ACTIVE)


    def test_playing_transitions_to_returning_when_idle_drops_even_above_resume_threshold(self):
        self._configure_common()
        main.current_state = main.ClientState.PLAYING
        main.playing_started_at = 0.0
        main._last_observed_idle_sec = 5.0

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

    def test_active_state_keeps_widget_runtime_warm(self):
        self._configure_common()
        main.current_state = main.ClientState.ACTIVE

        fake_playback = Mock()
        fake_playback.current_content_name.return_value = ""
        fake_playback._active_item = None

        with patch.object(main, "playback", fake_playback), patch.object(main, "idle_background", Mock()), patch.object(
            main, "get_idle_seconds", return_value=10.0
        ):
            main.run_state_cycle()

        fake_playback.stop.assert_called_once_with(stop_widget_runtime=False)


if __name__ == "__main__":
    unittest.main()
