import os
import shlex
import shutil
import subprocess
from pathlib import Path


class BorderlessFullscreenPlayer:
    VIDEO_EXTENSIONS = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v"}
    WINDOWS_START_TEMPLATE = 'cmd /c start "" /wait {media}'

    def __init__(self):
        self.image_duration_sec = int(os.getenv("IMAGE_DURATION_SEC", "8"))
        default_video_command = "mpv --fs --border=no --force-window=yes --quiet {media}"
        default_image_command = "mpv --fs --border=no --force-window=yes --quiet --image-display-duration={duration} {media}"

        if os.name == "nt":
            # Fallback to default Windows file association when mpv is not installed.
            default_video_command = self.WINDOWS_START_TEMPLATE
            default_image_command = self.WINDOWS_START_TEMPLATE

        self.video_command = os.getenv(
            "PLAYER_VIDEO_COMMAND",
            default_video_command,
        )
        self.image_command = os.getenv(
            "PLAYER_IMAGE_COMMAND",
            default_image_command,
        )
        self._process = None

    def _is_video(self, media_path: str) -> bool:
        return Path(media_path).suffix.lower() in self.VIDEO_EXTENSIONS

    def _build_command(self, media_path: str) -> list[str]:
        template = self.video_command if self._is_video(media_path) else self.image_command
        command_text = template.format(media="{media}", duration=self.image_duration_sec)
        parts = shlex.split(command_text, posix=os.name != "nt")
        return [media_path if part == "{media}" else part for part in parts]

    def _uses_windows_shell_fallback(self, media_path: str) -> bool:
        template = self.video_command if self._is_video(media_path) else self.image_command
        return os.name == "nt" and template.strip().lower() == self.WINDOWS_START_TEMPLATE.lower()

    @staticmethod
    def _play_with_windows_association(media_path: str) -> bool:
        shell_exe = shutil.which("powershell") or shutil.which("pwsh")
        if not shell_exe:
            return False

        escaped_path = media_path.replace("'", "''")
        result = subprocess.run(
            [
                shell_exe,
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                f"Start-Process -FilePath '{escaped_path}' -Wait",
            ],
            check=False,
        )
        return result.returncode == 0

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
            if self._uses_windows_shell_fallback(media_path):
                ok = self._play_with_windows_association(media_path)
                if not ok:
                    print("⚠️ windows association ile oynatma başarısız")
                return ok

            command = self._build_command(media_path)
            if not self._resolve_executable(command):
                print(f"⚠️ player executable bulunamadı: {command[0] if command else 'unknown'}")
                return False

            process = subprocess.Popen(command)
            self._process = process
            process.wait()
            return process.returncode == 0
        except Exception as exc:
            print(f"⚠️ medya oynatma hatası: {exc}")
            return False
        finally:
            if self._process is process:
                self._process = None

    def stop(self):
        if self._process and self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()
        self._process = None
