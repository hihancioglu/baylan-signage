import os
import shlex
import shutil
import subprocess
import threading
from pathlib import Path


class BorderlessFullscreenPlayer:
    VIDEO_EXTENSIONS = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v"}

    def __init__(self):
        self.image_duration_sec = int(os.getenv("IMAGE_DURATION_SEC", "8"))
        self.video_command = os.getenv(
            "PLAYER_VIDEO_COMMAND",
            "mpv --fs --border=no --force-window=yes --quiet {media}",
        )
        self.image_command = os.getenv(
            "PLAYER_IMAGE_COMMAND",
            "mpv --fs --border=no --force-window=yes --quiet --image-display-duration={duration} {media}",
        )
        self._process = None
        self._lock = threading.Lock()

    def _is_video(self, media_path: str) -> bool:
        return Path(media_path).suffix.lower() in self.VIDEO_EXTENSIONS

    def _build_command(self, media_path: str) -> list[str]:
        template = self.video_command if self._is_video(media_path) else self.image_command
        command_text = template.format(media="{media}", duration=self.image_duration_sec)
        parts = shlex.split(command_text, posix=os.name != "nt")
        return [media_path if part == "{media}" else part for part in parts]

    @staticmethod
    def _resolve_executable(command: list[str]) -> bool:
        if not command:
            return False
        return shutil.which(command[0]) is not None

    def play_blocking(self, media_path: str) -> bool:
        if not Path(media_path).exists():
            print(f"⚠️ medya bulunamadı: {media_path}")
            return False

        self.stop()
        process = None

        try:
            command = self._build_command(media_path)
            if not self._resolve_executable(command):
                print(f"⚠️ player executable bulunamadı: {command[0] if command else 'unknown'}")
                return False

            process = subprocess.Popen(command)
            with self._lock:
                self._process = process
            process.wait()
            return process.returncode == 0
        except Exception as exc:
            print(f"⚠️ medya oynatma hatası: {exc}")
            return False
        finally:
            with self._lock:
                if self._process is process:
                    self._process = None

    def stop(self):
        with self._lock:
            process = self._process
            self._process = None

        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
