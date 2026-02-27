import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path


def _safe_print(message: str) -> None:
    try:
        print(message)
    except OSError:
        pass


class BorderlessFullscreenPlayer:
    VIDEO_EXTENSIONS = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v"}
    IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp", ".svg"}
    SLIDESHOW_EXTENSIONS = {".json"}
    VLC_VIDEO_TEMPLATE = (
        "{player} --intf dummy --dummy-quiet --fullscreen --play-and-exit "
        "--no-video-title-show --no-osd --no-mouse-events --no-keyboard-events "
        "--video-on-top --no-interact --quiet {media}"
    )
    VLC_IMAGE_TEMPLATE = (
        "{player} --intf dummy --dummy-quiet --fullscreen "
        "--no-video-title-show --no-osd --no-mouse-events --no-keyboard-events "
        "--no-interact --quiet --loop --image-duration={duration} "
        "--avcodec-hw=none {media}"
    )
    MPV_COMMON_FLAGS = (
        "--fs --border=no --force-window=immediate --ontop --quiet "
        "--background-color=0/0/0 --osc=no --osd-level=0 --input-cursor=no"
    )
    MPV_VIDEO_TEMPLATE = "{player} " + MPV_COMMON_FLAGS + " {media}"
    MPV_IMAGE_TEMPLATE = (
        "{player} " + MPV_COMMON_FLAGS + " "
        "--image-display-duration={duration} {media}"
    )
    PYTHON_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp", ".json"}

    def __init__(self):
        self.image_duration_sec = int(os.getenv("IMAGE_DURATION_SEC", "8"))
        self.static_image_duration_sec = int(os.getenv("STATIC_IMAGE_DURATION_SEC", "86400"))
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
        self._stop_requested = False
        self._python_image_viewer_supported = self._detect_python_image_viewer_support()
        self._python_image_viewer_runtime_enabled = True
        self._last_interrupted = False

    def _detect_python_image_viewer_support(self) -> bool:
        if os.getenv("PYTHON_IMAGE_VIEWER_ENABLED", "1").strip().lower() in {"0", "false", "no"}:
            return False

        if os.name != "nt" and not (os.getenv("DISPLAY") or os.getenv("WAYLAND_DISPLAY")):
            _safe_print("⚠️ Python image viewer pasif: DISPLAY/WAYLAND_DISPLAY bulunamadı")
            return False

        try:
            import pygame

            pygame.display.init()
            pygame.display.quit()
            return True
        except Exception as exc:
            _safe_print(f"⚠️ Python image viewer pasif: pygame kullanılamıyor ({exc})")
            return False

    @staticmethod
    def _is_vlc_command(command: list[str]) -> bool:
        if not command:
            return False
        executable_name = Path(BorderlessFullscreenPlayer._strip_outer_quotes(command[0])).name.lower()
        return "vlc" in executable_name

    def _should_use_python_image_viewer(self, media_path: str) -> bool:
        if not self._python_image_viewer_supported:
            return False
        if not self._python_image_viewer_runtime_enabled:
            return False
        return Path(media_path).suffix.lower() in self.PYTHON_IMAGE_EXTENSIONS

    def _build_mpv_image_command(self, media_path: str, image_duration_sec: int | None = None) -> list[str]:
        duration = self.image_duration_sec if image_duration_sec is None else image_duration_sec
        command_text = self.MPV_IMAGE_TEMPLATE.format(player="mpv", duration=duration, media="{media}")
        parts = shlex.split(command_text, posix=os.name != "nt")
        command = [media_path if part == "{media}" else part for part in parts]
        return [self._strip_outer_quotes(part) for part in command]

    def _build_python_image_command(self, media_path: str, image_duration_sec: int | None = None) -> list[str]:
        duration = self.image_duration_sec if image_duration_sec is None else image_duration_sec
        viewer_path = Path(__file__).with_name("image_viewer.py")
        return [sys.executable, str(viewer_path), media_path, str(duration)]

    @staticmethod
    def _is_vlc_image_fallback_allowed() -> bool:
        return os.getenv("ALLOW_VLC_IMAGE_FALLBACK", "0").strip().lower() in {
            "1",
            "true",
            "yes",
        }

    def _prefer_non_vlc_image_command(
        self,
        media_path: str,
        command: list[str],
        image_duration_sec: int | None = None,
    ) -> list[str] | None:
        if not self.is_image(media_path):
            return command

        if not self._is_vlc_command(command):
            return command

        if self._is_vlc_image_fallback_allowed():
            return command

        mpv_fallback = self._build_mpv_image_command(
            media_path,
            image_duration_sec=image_duration_sec,
        )
        if self._resolve_executable(mpv_fallback):
            _safe_print("ℹ️ image oynatımında VLC yerine mpv kullanılacak")
            return mpv_fallback

        _safe_print("⚠️ VLC image fallback devre dışı ve mpv bulunamadı, medya atlandı")
        return None

    def _pick_default_player_commands(self) -> tuple[str, str]:
        # Windows'ta VLC her içerik geçişinde pencereyi kapatıp yeniden açarken
        # masaüstünü kısa süre görünür bırakabiliyor. mpv bu geçişi daha stabil
        # yönettiği için varsayılan seçimde önceliği mpv'ye veriyoruz.
        player_candidates = ["mpv"]
        if os.name == "nt":
            player_candidates.extend(
                [
                    r"C:\Program Files\mpv\mpv.exe",
                    "mpv.exe",
                    r"C:\Program Files\VideoLAN\VLC\vlc.exe",
                    "vlc",
                    "vlc.exe",
                ]
            )
        else:
            player_candidates.append("vlc")

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

    def is_image(self, media_path: str) -> bool:
        return Path(media_path).suffix.lower() in self.IMAGE_EXTENSIONS

    def supports_media(self, media_path: str) -> bool:
        ext = Path(media_path).suffix.lower()
        return ext in self.VIDEO_EXTENSIONS or ext in self.IMAGE_EXTENSIONS or ext in self.SLIDESHOW_EXTENSIONS

    def _build_command(self, media_path: str, image_duration_sec: int | None = None) -> list[str]:
        template = self.video_command if self._is_video(media_path) else self.image_command
        duration = self.image_duration_sec if image_duration_sec is None else image_duration_sec
        command_text = template.format(media="{media}", duration=duration)
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

    def play_blocking(
        self,
        media_path: str,
        image_duration_sec: int | None = None,
        start_position_sec: float | None = None,
    ) -> bool:
        if not Path(media_path).exists():
            self._last_interrupted = False
            _safe_print(f"⚠️ medya bulunamadı: {media_path}")
            return False

        if not self.supports_media(media_path):
            self._last_interrupted = False
            _safe_print(f"⚠️ desteklenmeyen medya formatı atlandı: {media_path}")
            return False

        self.stop()

        process = None

        try:
            command = self._build_command(media_path, image_duration_sec=image_duration_sec)
            if start_position_sec and self._is_video(media_path):
                exe_name = Path(self._strip_outer_quotes(command[0])).name.lower() if command else ""
                if "vlc" in exe_name:
                    command.insert(1, f"--start-time={max(0, float(start_position_sec)):.3f}")
                elif "mpv" in exe_name:
                    command.insert(1, f"--start={max(0, float(start_position_sec)):.3f}")
            used_python_image_viewer = False
            if self._should_use_python_image_viewer(media_path):
                command = self._build_python_image_command(
                    media_path,
                    image_duration_sec=image_duration_sec,
                )
                used_python_image_viewer = True
            else:
                command = self._prefer_non_vlc_image_command(
                    media_path,
                    command,
                    image_duration_sec=image_duration_sec,
                )
                if not command:
                    return False

            if not self._resolve_executable(command):
                _safe_print(f"⚠️ player executable bulunamadı: {command[0] if command else 'unknown'}")
                return False

            self._stop_requested = False
            process = subprocess.Popen(command)
            self._process = process
            process.wait()

            if process.returncode != 0 and used_python_image_viewer:
                _safe_print(
                    "⚠️ Python image viewer başarısız oldu "
                    f"(exit={process.returncode}, media={media_path}), medya player fallback deneniyor"
                )
                self._python_image_viewer_runtime_enabled = False
                fallback_command = self._build_command(media_path, image_duration_sec=image_duration_sec)
                fallback_command = self._prefer_non_vlc_image_command(
                    media_path,
                    fallback_command,
                    image_duration_sec=image_duration_sec,
                )
                if fallback_command and self._resolve_executable(fallback_command):
                    process = subprocess.Popen(fallback_command)
                    self._process = process
                    process.wait()

            interrupted = self._stop_requested
            self._last_interrupted = interrupted
            return process.returncode == 0 or interrupted
        except Exception as exc:
            self._last_interrupted = False
            _safe_print(f"⚠️ medya oynatma hatası: {exc}")
            return False
        finally:
            if self._process is process:
                self._process = None

    def stop(self):
        if self._process and self._process.poll() is None:
            self._stop_requested = True
            self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()
        self._process = None


    def last_play_was_interrupted(self) -> bool:
        return self._last_interrupted
