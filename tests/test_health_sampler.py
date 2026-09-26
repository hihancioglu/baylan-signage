import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from client import client


class TestHealthSampler(unittest.TestCase):
    def setUp(self):
        client._stop_health_sampler()
        self.saved_state = (
            client._health_high_cpu_seconds,
            client._health_last_sample_at,
            client._health_started_at,
            dict(client._last_health_payload),
            client._health_bootstrap_last_logged_remaining_sec,
            client._health_bootstrap_completed_logged,
        )
        client._health_high_cpu_seconds = 0.0
        client._health_last_sample_at = None
        client._health_started_at = -1000.0
        client._last_health_payload = {"health": "OK"}
        client._health_bootstrap_last_logged_remaining_sec = None
        client._health_bootstrap_completed_logged = False
        client._health_sampler_stop_event.clear()

    def tearDown(self):
        client._stop_health_sampler()
        (
            client._health_high_cpu_seconds,
            client._health_last_sample_at,
            client._health_started_at,
            payload,
            client._health_bootstrap_last_logged_remaining_sec,
            client._health_bootstrap_completed_logged,
        ) = self.saved_state
        client._last_health_payload = payload

    @staticmethod
    def _psutil(cpu_values, memory=40):
        values = iter(cpu_values)
        return SimpleNamespace(
            cpu_percent=lambda interval=None: next(values),
            virtual_memory=lambda: SimpleNamespace(percent=memory),
        )

    def _sample_at(self, psutil_module, sampled_at):
        # responsiveness starts one second before the intended sample time.
        monotonic_values = [sampled_at - 1.0, sampled_at, sampled_at]
        with patch.object(client.importlib, "import_module", return_value=psutil_module), patch.object(
            client.time, "monotonic", side_effect=monotonic_values
        ), patch.object(client._health_sampler_stop_event, "wait", return_value=False):
            return client._sample_health_metrics()

    def test_health_snapshot_read_is_non_blocking_and_returns_copy(self):
        client._last_health_payload = {"health": "OK", "cpu_load_percent": 12.5}
        with patch.object(client.time, "sleep") as sleep_mock, patch.object(
            client.importlib, "import_module"
        ) as import_mock:
            payload = client._get_cpu_temperature_payload()

        sleep_mock.assert_not_called()
        import_mock.assert_not_called()
        payload["health"] = "changed"
        self.assertEqual(client._last_health_payload["health"], "OK")

    def test_sample_updates_cached_cpu_memory_and_health(self):
        payload = self._sample_at(self._psutil([25]), 0.0)

        self.assertEqual(payload["cpu_load_percent"], 25.0)
        self.assertEqual(payload["memory_pressure_percent"], 40.0)
        self.assertEqual(payload["health"], "OK")

    def test_sustained_cpu_uses_elapsed_seconds_and_recovers(self):
        psutil_module = self._psutil([90, 90, 90, 90, 30])
        for sampled_at in (0.0, 10.0, 20.0, 30.0):
            payload = self._sample_at(psutil_module, sampled_at)

        self.assertEqual(payload["sustained_high_cpu_seconds"], 30)
        self.assertTrue(payload["sustained_high_cpu"])

        recovered = self._sample_at(psutil_module, 40.0)
        self.assertEqual(recovered["sustained_high_cpu_seconds"], 0)
        self.assertFalse(recovered["sustained_high_cpu"])

    def test_failed_sample_preserves_last_successful_payload(self):
        expected = self._sample_at(self._psutil([25]), 0.0)
        failing_psutil = SimpleNamespace(
            cpu_percent=lambda interval=None: 30,
            virtual_memory=Mock(side_effect=RuntimeError("memory unavailable")),
        )

        with patch.object(client.importlib, "import_module", return_value=failing_psutil):
            with self.assertRaisesRegex(RuntimeError, "memory unavailable"):
                client._sample_health_metrics()

        self.assertEqual(client._get_cpu_temperature_payload(), expected)

    def test_sampler_loop_survives_sample_exception(self):
        fake_psutil = SimpleNamespace(cpu_percent=lambda interval=None: 0)
        calls = []

        def sample():
            calls.append(True)
            if len(calls) == 1:
                raise RuntimeError("transient")
            client._health_sampler_stop_event.set()
            return {"health": "OK"}

        with patch.object(client, "HEALTH_SAMPLE_INTERVAL_SEC", 0.01), patch.object(
            client.importlib, "import_module", return_value=fake_psutil
        ), patch.object(client, "_sample_health_metrics", side_effect=sample):
            worker = threading.Thread(target=client._health_sampler_loop)
            worker.start()
            worker.join(timeout=1)

        self.assertFalse(worker.is_alive())
        self.assertEqual(len(calls), 2)

    def test_start_is_idempotent(self):
        with patch.object(client, "IS_WIDGET_VIEWER_PROCESS", False), patch.object(
            client, "_health_sampler_loop", side_effect=lambda: client._health_sampler_stop_event.wait()
        ):
            client._start_health_sampler()
            first_worker = client._health_sampler_thread
            client._start_health_sampler()
            self.assertIs(client._health_sampler_thread, first_worker)
            self.assertTrue(first_worker.is_alive())

    def test_widget_viewer_does_not_start_sampler(self):
        with patch.object(client, "IS_WIDGET_VIEWER_PROCESS", True), patch.object(
            client.threading, "Thread"
        ) as thread_mock:
            client._start_health_sampler()

        thread_mock.assert_not_called()
        self.assertIsNone(client._health_sampler_thread)

    def test_all_widget_viewer_markers_are_detected(self):
        for marker in ("--widget", "--runtime-ipc", "--baylan-widget-runtime"):
            with self.subTest(marker=marker):
                self.assertTrue(client._is_widget_viewer_process([marker]))


if __name__ == "__main__":
    unittest.main()
