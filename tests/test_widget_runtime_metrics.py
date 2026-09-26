import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from client.client import _extract_cli_option, _widget_runtime_telemetry


MB = 1024 * 1024


class FakeProcess:
    def __init__(self, pid, cmdline=None, *, parent_pid=0, name="agent.exe", rss_mb=1, children=None):
        self.pid = pid
        self.info = {"pid": pid, "cmdline": cmdline or []}
        self._parent_pid = parent_pid
        self._name = name
        self._rss = rss_mb * MB
        self._children = children or []

    def ppid(self):
        return self._parent_pid

    def name(self):
        return self._name

    def memory_info(self):
        return SimpleNamespace(rss=self._rss)

    def children(self, recursive=False):
        if not recursive:
            return list(self._children)
        result = []
        pending = list(self._children)
        while pending:
            child = pending.pop(0)
            result.append(child)
            pending.extend(child.children())
        return result


def viewer_command(parent=100, monitor=0, bounds="0,0,1536,960"):
    command = ["agent.exe", "--widget", "__BAYLAN_WIDGET_ENGINE__", "--runtime-ipc", "--baylan-widget-runtime"]
    if parent is not None:
        command += ["--parent-pid", str(parent)]
    if monitor is not None:
        command += ["--monitor", str(monitor)]
    if bounds is not None:
        command += [f"--monitor-bounds={bounds}"]
    return command


class TestWidgetRuntimeTelemetry(unittest.TestCase):
    def telemetry(self, processes):
        psutil_module = Mock()
        psutil_module.process_iter.return_value = processes
        with patch("client.client.platform.system", return_value="Windows"):
            return _widget_runtime_telemetry(psutil_module)

    def test_onefile_pair_is_one_runtime_and_webviews_are_unique(self):
        webviews = [FakeProcess(30 + index, name="msedgewebview2.exe", rss_mb=10) for index in range(6)]
        child = FakeProcess(20, viewer_command(), parent_pid=10, rss_mb=20, children=webviews)
        bootloader = FakeProcess(10, viewer_command(), parent_pid=100, rss_mb=30, children=[child])

        telemetry = self.telemetry([bootloader, child])

        self.assertEqual(telemetry["runtime_instance_count"], 1)
        self.assertEqual(telemetry["viewer_process_count"], 2)
        self.assertEqual(telemetry["webview2_process_count"], 6)
        self.assertEqual(telemetry["viewer_ram_mb"], 50.0)
        self.assertEqual(telemetry["webview2_ram_mb"], 60.0)

    def test_two_monitors_are_two_runtimes_with_four_physical_processes(self):
        child_0 = FakeProcess(11, viewer_command(monitor=0), parent_pid=10)
        boot_0 = FakeProcess(10, viewer_command(monitor=0), parent_pid=100, children=[child_0])
        child_1 = FakeProcess(21, viewer_command(monitor=1), parent_pid=20)
        boot_1 = FakeProcess(20, viewer_command(monitor=1), parent_pid=100, children=[child_1])

        telemetry = self.telemetry([boot_0, child_0, boot_1, child_1])

        self.assertEqual(telemetry["runtime_instance_count"], 2)
        self.assertEqual(telemetry["viewer_process_count"], 4)

    def test_independent_same_identity_trees_remain_duplicate_runtimes(self):
        webview_a = FakeProcess(31, name="msedgewebview2.exe")
        child_a = FakeProcess(11, viewer_command(), parent_pid=10, children=[webview_a])
        boot_a = FakeProcess(10, viewer_command(), parent_pid=100, children=[child_a])
        webview_b = FakeProcess(41, name="msedgewebview2.exe")
        child_b = FakeProcess(21, viewer_command(), parent_pid=20, children=[webview_b])
        boot_b = FakeProcess(20, viewer_command(), parent_pid=100, children=[child_b])

        telemetry = self.telemetry([boot_a, child_a, boot_b, child_b])

        self.assertEqual(telemetry["runtime_instance_count"], 2)
        self.assertEqual(telemetry["webview2_process_count"], 2)

    def test_unrelated_webview_is_excluded_and_shared_descendant_is_counted_once(self):
        webview = FakeProcess(30, name="msedgewebview2.exe", rss_mb=10)
        child = FakeProcess(20, viewer_command(), parent_pid=10, children=[webview])
        bootloader = FakeProcess(10, viewer_command(), parent_pid=100, children=[child])
        unrelated = FakeProcess(99, name="msedgewebview2.exe", rss_mb=50)

        telemetry = self.telemetry([bootloader, child, unrelated])

        self.assertEqual(telemetry["webview2_process_count"], 1)
        self.assertEqual(telemetry["webview2_ram_mb"], 10.0)

    def test_invalid_or_missing_options_do_not_break_telemetry(self):
        malformed = viewer_command(parent=None, monitor=None, bounds=None) + ["--parent-pid", "--monitor=bad"]
        child = FakeProcess(20, malformed, parent_pid=10)
        bootloader = FakeProcess(10, malformed, parent_pid=100, children=[child])

        telemetry = self.telemetry([bootloader, child])

        self.assertEqual(telemetry["runtime_instance_count"], 1)
        self.assertEqual(telemetry["viewer_process_count"], 2)

    def test_extract_cli_option_supports_split_and_equals_forms(self):
        self.assertEqual(_extract_cli_option(["--parent-pid", "20752"], "--parent-pid"), "20752")
        self.assertEqual(_extract_cli_option(["--monitor=0"], "--monitor"), "0")
        self.assertIsNone(_extract_cli_option(["--parent-pid"], "--parent-pid"))


if __name__ == "__main__":
    unittest.main()
