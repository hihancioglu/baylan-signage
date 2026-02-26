import os
import shlex
import subprocess
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

    def _is_video(self, media_path: str) -> bool:
        return Path(media_path).suffix.lower() in self.VIDEO_EXTENSIONS

    def _build_command(self, media_path: str) -> list[str]:
        template = self.video_command if self._is_video(media_path) else self.image_command
        cmd = template.format(media=media_path, duration=self.image_duration_sec)
        return shlex.split(cmd)

    def play_blocking(self, media_path: str) -> bool:
        if not Path(media_path).exists():
            return False

        self.stop()

        try:
            self._process = subprocess.Popen(self._build_command(media_path))
            self._process.wait()
            return self._process.returncode == 0
        except Exception:
            return False
        finally:
            self._process = None

    def stop(self):
        if self._process and self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()
        self._process = None
