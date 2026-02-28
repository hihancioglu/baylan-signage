import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path


def _log(log_file: Path, message: str) -> None:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    with open(log_file, "a", encoding="utf-8") as fh:
        fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} | {message}\n")


def _wait_for_pid_exit(old_pid: int, attempts: int, delay_sec: float, log_file: Path) -> None:
    if old_pid <= 0:
        return

    for idx in range(attempts):
        alive = subprocess.call(
            ["tasklist", "/FI", f"PID eq {old_pid}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ) == 0
        if not alive:
            _log(log_file, f"old process exited: pid={old_pid}")
            return
        time.sleep(delay_sec)
        if idx and idx % 10 == 0:
            _log(log_file, f"waiting old process to exit: pid={old_pid} attempt={idx}")

    _log(log_file, f"old process still alive after timeout: pid={old_pid}")


def _copy_with_retry(src: Path, dst: Path, attempts: int, delay_sec: float, log_file: Path) -> bool:
    for idx in range(1, attempts + 1):
        try:
            shutil.copy2(src, dst)
            _log(log_file, f"swap success: src={src} dst={dst}")
            return True
        except OSError as exc:
            _log(log_file, f"swap failed attempt={idx}: {exc}")
            time.sleep(delay_sec)
    return False


def _start_process(target: Path, work_dir: Path, log_file: Path) -> bool:
    try:
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        subprocess.Popen([str(target)], cwd=str(work_dir), creationflags=creation_flags)
        _log(log_file, f"restart success: target={target}")
        return True
    except OSError as exc:
        _log(log_file, f"restart failed for target={target}: {exc}")
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Baylan client updater")
    parser.add_argument("--src", required=True, help="new binary path")
    parser.add_argument("--dst", required=True, help="installed binary path")
    parser.add_argument("--old-pid", type=int, default=0, help="running client pid")
    parser.add_argument("--work-dir", required=True, help="working directory for restart")
    parser.add_argument("--log-file", required=True, help="log file path")
    args = parser.parse_args()

    src = Path(args.src).resolve()
    dst = Path(args.dst).resolve()
    work_dir = Path(args.work_dir).resolve()
    log_file = Path(args.log_file).resolve()

    _log(log_file, "=== updater start ===")
    _log(log_file, f"src={src}")
    _log(log_file, f"dst={dst}")
    _wait_for_pid_exit(args.old_pid, attempts=90, delay_sec=1.0, log_file=log_file)

    launch_target = dst
    if not _copy_with_retry(src, dst, attempts=30, delay_sec=1.0, log_file=log_file):
        launch_target = src
        _log(log_file, "swap failed after retries, fallback to downloaded binary")

    started = _start_process(launch_target, work_dir=work_dir, log_file=log_file)
    if started and launch_target != src:
        try:
            src.unlink(missing_ok=True)
            _log(log_file, f"cleanup downloaded binary: {src}")
        except OSError as exc:
            _log(log_file, f"cleanup failed: {exc}")

    _log(log_file, "=== updater done ===")
    return 0 if started else 1


if __name__ == "__main__":
    if os.name != "nt":
        print("This updater is intended for Windows.")
        sys.exit(1)
    sys.exit(main())
