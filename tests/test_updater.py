import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from client import updater


class TestUpdaterMainFlow(unittest.TestCase):
    def _args(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        base = Path(temp_dir.name)
        src = base / "downloaded.exe"
        dst = base / "installed.exe"
        work = base / "work"
        log = base / "updater.log"
        src.write_bytes(b"MZ")
        dst.write_bytes(b"MZ")
        work.mkdir(parents=True, exist_ok=True)
        return [
            "updater.py",
            "--src",
            str(src),
            "--dst",
            str(dst),
            "--old-pid",
            "123",
            "--work-dir",
            str(work),
            "--log-file",
            str(log),
        ], src, dst, work, log

    def test_swap_failure_restarts_installed_binary_first(self):
        argv, src, dst, work, log = self._args()
        with patch("sys.argv", argv), patch.object(updater, "_wait_for_pid_exit") as wait_mock, patch.object(
            updater,
            "_copy_with_retry",
            return_value=False,
        ), patch.object(updater, "_start_process", return_value=True) as start_mock:
            rc = updater.main()

        self.assertEqual(rc, 0)
        wait_mock.assert_called_once_with(123, attempts=90, delay_sec=1.0, log_file=log.resolve())
        start_mock.assert_called_once_with(dst.resolve(), work_dir=work.resolve(), log_file=log.resolve())

    def test_fallback_to_downloaded_binary_only_after_installed_restart_fails(self):
        argv, src, dst, work, log = self._args()
        with patch("sys.argv", argv), patch.object(updater, "_wait_for_pid_exit"), patch.object(updater, "_copy_with_retry", return_value=False), patch.object(
            updater,
            "_start_process",
            side_effect=[False, True],
        ) as start_mock:
            rc = updater.main()

        self.assertEqual(rc, 0)
        self.assertEqual(start_mock.call_count, 2)
        first_call = start_mock.call_args_list[0]
        second_call = start_mock.call_args_list[1]
        self.assertEqual(first_call.args[0], dst.resolve())
        self.assertEqual(second_call.args[0], src.resolve())


if __name__ == "__main__":
    unittest.main()
