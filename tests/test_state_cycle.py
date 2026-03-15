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

    def tearDown(self):
        main.current_state = self.orig_state
        main.idle_mode_enabled = self.orig_idle_mode
        main.content_enabled = self.orig_content_enabled
        main.idle_timeout_sec = self.orig_idle_timeout
        main.playing_started_at = self.orig_playing_started_at
        main.emergency_active = self.orig_emergency_active

    def _configure_common(self):
        main.idle_mode_enabled = True
        main.content_enabled = True
        main.emergency_active = False
        main.idle_timeout_sec = 60

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


if __name__ == "__main__":
    unittest.main()
