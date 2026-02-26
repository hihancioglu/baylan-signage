import os
import shlex
import shutil
import subprocess
from pathlib import Path


class BorderlessFullscreenPlayer:
    VIDEO_EXTENSIONS = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v"}
    VLC_VIDEO_TEMPLATE = (
        "{player} --fullscreen --play-and-exit --no-video-title-show "
        "--no-qt-fs-controller --quiet {media}"
    )
    VLC_IMAGE_TEMPLATE = (
        "{player} --fullscreen --play-and-exit --no-video-title-show "
        "--no-qt-fs-controller --image-duration={duration} --quiet {media}"
    )
    MPV_VIDEO_TEMPLATE = "{player} --fs --border=no --force-window=yes --quiet {media}"
    MPV_IMAGE_TEMPLATE = (
        "{player} --fs --border=no --force-window=yes --quiet "
        "--image-display-duration={duration} {media}"
    )

    def __init__(self):
        self.image_duration_sec = int(os.getenv("IMAGE_DURATION_SEC", "8"))
        default_video_command, default_image_command = self._pick_default_player_commands()

        self.video_command = os.getenv(
            "PLAYER_VIDEO_COMMAND",
            default_video_command,
        )
        self.image_command = os.getenv(
            "PLAYER_IMAGE_COMMAND",
            default_image_command,
        )
        self._process = None

    def _pick_default_player_commands(self) -> tuple[str, str]:
        player_candidates = []
        if os.name == "nt":
            player_candidates.extend(
                [
                    r"C:\Program Files\VideoLAN\VLC\vlc.exe",
                    "vlc",
                    "vlc.exe",
                ]
            )
        player_candidates.extend(["vlc", "mpv"])

        for player in player_candidates:
            resolved = shutil.which(player)
            if not resolved:
                continue

            player_quoted = shlex.quote(resolved)
            if "vlc" in Path(resolved).name.lower():
                return (
                    self.VLC_VIDEO_TEMPLATE.format(player=player_quoted, media="{media}"),
                    self.VLC_IMAGE_TEMPLATE.format(
                        player=player_quoted,
                        duration="{duration}",
                        media="{media}",
                    ),
                )

            return (
                self.MPV_VIDEO_TEMPLATE.format(player=player_quoted, media="{media}"),
                self.MPV_IMAGE_TEMPLATE.format(
                    player=player_quoted,
                    duration="{duration}",
                    media="{media}",
                ),
            )

        # Keep player unresolved when nothing exists in PATH.
        return (
            self.VLC_VIDEO_TEMPLATE.format(player="vlc", media="{media}"),
            self.VLC_IMAGE_TEMPLATE.format(player="vlc", duration="{duration}", media="{media}"),
        )

    def _is_video(self, media_path: str) -> bool:
        return Path(media_path).suffix.lower() in self.VIDEO_EXTENSIONS

    def _build_command(self, media_path: str) -> list[str]:
        template = self.video_command if self._is_video(media_path) else self.image_command
        command_text = template.format(media="{media}", duration=self.image_duration_sec)
        parts = shlex.split(command_text, posix=os.name != "nt")
        command = [media_path if part == "{media}" else part for part in parts]

        # Windows'ta shlex ile ayrıştırılan quoted executable path'ler ("C:\\...\\vlc.exe")
        # doğrudan subprocess'e gönderildiğinde bulunamıyor. Dış quote'ları temizle.
        return [self._strip_outer_quotes(part) for part in command]

    @staticmethod
    def _strip_outer_quotes(value: str) -> str:
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            return value[1:-1]
        return value

    @staticmethod
    def _resolve_executable(command: list[str]) -> bool:
        if not command:
            return False

        executable = BorderlessFullscreenPlayer._strip_outer_quotes(command[0])
        if Path(executable).is_file():
            return True

        return shutil.which(executable) is not None

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
