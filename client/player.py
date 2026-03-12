import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import ctypes
import time
from ctypes import wintypes
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
    _WINDOWS_BROWSER_KIOSK_FLAGS = [
        "--kiosk",
        "--edge-kiosk-type=fullscreen",
        "--app={widget}",
        "--start-fullscreen",
        "--start-maximized",
        "--window-position=0,0",
        "--disable-features=Translate,TranslateUI,msUndersideButton",
        "--disable-translate",
        "--translate=0",
        "--disable-infobars",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    _WINDOWS_BROWSER_KIOSK_FLAGS_CHROME = [
        "--kiosk",
        "--app={widget}",
        "--start-fullscreen",
        "--start-maximized",
        "--window-position=0,0",
        "--disable-features=Translate,TranslateUI",
        "--disable-translate",
        "--translate=0",
        "--disable-infobars",
        "--no-first-run",
        "--no-default-browser-check",
    ]

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
        self._widget_process = None
        self._python_image_viewer_supported = self._detect_python_image_viewer_support()
        self._python_image_viewer_runtime_enabled = True
        self._python_widget_viewer_supported = self._detect_python_widget_viewer_support()
        self._python_widget_viewer_runtime_enabled = True
        self._last_interrupted = False

    @staticmethod
    def _python_widget_viewer_enabled() -> bool:
        return os.getenv("PYTHON_WIDGET_VIEWER_ENABLED", "1").strip().lower() not in {"0", "false", "no"}

    @staticmethod
    def _prefer_python_widget_viewer() -> bool:
        return os.getenv("WIDGET_USE_PYTHON_VIEWER", "1").strip().lower() in {"1", "true", "yes"}

    @staticmethod
    def _find_frozen_widget_viewer_executable() -> str | None:
        if not getattr(sys, "frozen", False):
            return None

        executable_dir = Path(sys.executable).resolve().parent
        for candidate in ("widget_viewer.exe", "widget_viewer"):
            viewer_path = executable_dir / candidate
            if viewer_path.exists() and viewer_path.is_file():
                return str(viewer_path)
        return None

    def _detect_python_widget_viewer_support(self) -> bool:
        if not self._python_widget_viewer_enabled():
            return False

        if os.name != "nt":
            _safe_print("⚠️ Python widget viewer pasif: sadece Windows'ta destekleniyor")
            return False

        if getattr(sys, "frozen", False) and self._find_frozen_widget_viewer_executable() is None:
            _safe_print(
                "⚠️ Python widget viewer pasif: frozen build içinde bağımsız widget_viewer executable bulunamadı"
            )
            return False

        try:
            import webview

            return bool(webview)
        except Exception as exc:
            _safe_print(f"⚠️ Python widget viewer pasif: pywebview kullanılamıyor ({exc})")
            return False

    def _should_use_python_widget_viewer(self, widget_source: str) -> bool:
        if not self._is_widget_url(widget_source):
            return False
        if not self._prefer_python_widget_viewer():
            return False
        if not self._python_widget_viewer_supported:
            return False
        if not self._python_widget_viewer_runtime_enabled:
            return False
        return True

    def _build_python_widget_command(self, widget_source: str) -> list[str]:
        viewer_path = Path(__file__).with_name("widget_viewer.py")
        frozen_viewer = self._find_frozen_widget_viewer_executable()
        if frozen_viewer:
            return [frozen_viewer, widget_source]
        return [sys.executable, str(viewer_path), widget_source]

    def _is_python_widget_command(self, command: list[str]) -> bool:
        if not command:
            return False
        script_path = str(Path(__file__).with_name("widget_viewer.py"))
        if len(command) >= 2 and command[1] == script_path:
            return True
        frozen_viewer = self._find_frozen_widget_viewer_executable()
        if frozen_viewer and command[0] == frozen_viewer:
            return True
        return Path(command[0]).name.lower() in {"widget_viewer.exe", "widget_viewer"}


    @staticmethod
    def _find_frozen_image_viewer_executable() -> str | None:
        if not getattr(sys, "frozen", False):
            return None

        executable_dir = Path(sys.executable).resolve().parent
        for candidate in ("image_viewer.exe", "image_viewer"):
            viewer_path = executable_dir / candidate
            if viewer_path.exists() and viewer_path.is_file():
                return str(viewer_path)
        return None

    def _detect_python_image_viewer_support(self) -> bool:
        if os.getenv("PYTHON_IMAGE_VIEWER_ENABLED", "1").strip().lower() in {"0", "false", "no"}:
            return False

        if getattr(sys, "frozen", False) and self._find_frozen_image_viewer_executable() is None:
            _safe_print(
                "⚠️ Python image viewer pasif: frozen build içinde bağımsız image_viewer executable bulunamadı"
            )
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
        parts = self._split_command_text(command_text)
        command = [media_path if part == "{media}" else part for part in parts]
        return [self._strip_outer_quotes(part) for part in command]

    @staticmethod
    def _split_windows_command(command_text: str) -> list[str]:
        if not isinstance(command_text, str):
            command_text = str(command_text)

        if not command_text:
            return []

        command_line_to_argv = ctypes.windll.shell32.CommandLineToArgvW
        command_line_to_argv.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
        command_line_to_argv.restype = ctypes.POINTER(ctypes.c_wchar_p)

        local_free = ctypes.windll.kernel32.LocalFree
        local_free.argtypes = [wintypes.HLOCAL]
        local_free.restype = wintypes.HLOCAL

        argc = ctypes.c_int(0)
        argv = command_line_to_argv(command_text, ctypes.byref(argc))
        if not argv:
            return []

        try:
            return [argv[index] for index in range(argc.value)]
        finally:
            local_free(argv)

    def _split_command_text(self, command_text: str) -> list[str]:
        try:
            if os.name == "nt":
                return self._split_windows_command(command_text)
            return shlex.split(command_text, posix=True)
        except Exception as exc:
            _safe_print(f"⚠️ komut ayrıştırılamadı, basit ayrıştırma kullanılacak ({exc})")
            return command_text.split()

    def _build_python_image_command(self, media_path: str, image_duration_sec: int | None = None) -> list[str]:
        duration = self.image_duration_sec if image_duration_sec is None else image_duration_sec
        viewer_path = Path(__file__).with_name("image_viewer.py")
        frozen_viewer = self._find_frozen_image_viewer_executable()
        if frozen_viewer:
            return [frozen_viewer, media_path, str(duration)]
        return [sys.executable, str(viewer_path), media_path, str(duration)]

    @staticmethod
    def _is_mpv_command(command: list[str]) -> bool:
        if not command:
            return False
        executable_name = Path(BorderlessFullscreenPlayer._strip_outer_quotes(command[0])).name.lower()
        return "mpv" in executable_name

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

    @staticmethod
    def _is_slideshow_manifest(media_path: str) -> bool:
        return Path(media_path).suffix.lower() in BorderlessFullscreenPlayer.SLIDESHOW_EXTENSIONS


    def _build_widget_command(self, widget_source: str) -> list[str]:
        command_template = os.getenv("WIDGET_PLAYER_COMMAND", "")
        if command_template.strip():
            parts = self._split_command_text(command_template)
            command = [widget_source if part == "{widget}" else part for part in parts]
            command = [self._strip_outer_quotes(part) for part in command]
            if command:
                return command

        if self._should_use_python_widget_viewer(widget_source):
            return self._build_python_widget_command(widget_source)

        if self._is_widget_url(widget_source):
            windows_browser = self._resolve_windows_kiosk_browser()
            if windows_browser:
                executable, extra_flags = windows_browser
                command = [executable]
                widget_arg_included = False
                for flag in extra_flags:
                    if "{widget}" in flag:
                        widget_arg_included = True
                        command.append(flag.replace("{widget}", widget_source))
                    else:
                        command.append(flag)

                if not widget_arg_included:
                    command.append(widget_source)
                return command

        if os.name == "nt":
            return ["cmd", "/c", "start", "", widget_source]

        opener = shutil.which("xdg-open") or shutil.which("open")
        if opener:
            return [opener, widget_source]

        return []

    @staticmethod
    def _is_widget_url(widget_source: str) -> bool:
        normalized = str(widget_source or "").strip().lower()
        return normalized.startswith("http://") or normalized.startswith("https://")

    @staticmethod
    def _resolve_windows_kiosk_browser() -> tuple[str, list[str]] | None:
        if os.name != "nt":
            return None

        candidates: list[tuple[str, list[str]]] = [
            (
                "msedge",
                BorderlessFullscreenPlayer._WINDOWS_BROWSER_KIOSK_FLAGS,
            ),
            (
                "chrome",
                BorderlessFullscreenPlayer._WINDOWS_BROWSER_KIOSK_FLAGS_CHROME,
            ),
        ]
        absolute_candidates = [
            (
                r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                BorderlessFullscreenPlayer._WINDOWS_BROWSER_KIOSK_FLAGS,
            ),
            (
                r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
                BorderlessFullscreenPlayer._WINDOWS_BROWSER_KIOSK_FLAGS,
            ),
            (
                r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                BorderlessFullscreenPlayer._WINDOWS_BROWSER_KIOSK_FLAGS_CHROME,
            ),
        ]

        for candidate, flags in candidates:
            resolved = shutil.which(candidate)
            if resolved:
                return resolved, flags

        for candidate, flags in absolute_candidates:
            if Path(candidate).is_file():
                return candidate, flags

        return None

    def _wait_widget_until_stop(self, process: subprocess.Popen) -> bool:
        while True:
            if self._stop_requested:
                break
            if process.poll() is not None:
                break
            time.sleep(0.2)

        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()

        interrupted = self._stop_requested
        self._last_interrupted = interrupted
        return (process.returncode in (0, None)) or interrupted

    def play_widget_blocking(self, widget_source: str, duration_sec: int) -> bool:
        source = str(widget_source or "").strip()
        if not source:
            self._last_interrupted = False
            _safe_print("⚠️ widget kaynağı boş")
            return False

        self.stop()

        command = self._build_widget_command(source)
        if not command:
            self._last_interrupted = False
            _safe_print("⚠️ widget oynatılamıyor: browser/webview komutu bulunamadı")
            return False

        process = None
        using_python_widget_viewer = self._is_python_widget_command(command)
        try:
            self._stop_requested = False
            process = subprocess.Popen(command)
            self._widget_process = process

            if self._is_widget_url(source):
                result = self._wait_widget_until_stop(process)
                if not result and using_python_widget_viewer and not self._stop_requested:
                    self._python_widget_viewer_runtime_enabled = False
                return result

            deadline = time.monotonic() + max(1, int(duration_sec))
            while time.monotonic() < deadline:
                if self._stop_requested:
                    break
                if process.poll() is not None:
                    break
                time.sleep(0.2)

            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()

            interrupted = self._stop_requested
            self._last_interrupted = interrupted
            return (process.returncode in (0, None)) or interrupted
        except Exception as exc:
            self._last_interrupted = False
            if using_python_widget_viewer:
                self._python_widget_viewer_runtime_enabled = False
            _safe_print(f"⚠️ widget oynatma hatası: {exc}")
            return False
        finally:
            if self._widget_process is process:
                self._widget_process = None

    def _build_command(self, media_path: str, image_duration_sec: int | None = None) -> list[str]:
        template = self.video_command if self._is_video(media_path) else self.image_command
        duration = self.image_duration_sec if image_duration_sec is None else image_duration_sec
        command_text = template.format(media="{media}", duration=duration)
        parts = self._split_command_text(command_text)
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

        if self._is_slideshow_manifest(media_path) and not self._should_use_python_image_viewer(media_path):
            self._last_interrupted = False
            _safe_print(
                "⚠️ slayt manifesti için Python image viewer kullanılamıyor, medya atlandı: "
                f"{media_path}"
            )
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
                if self._is_slideshow_manifest(media_path):
                    _safe_print(
                        "⚠️ slayt manifesti medya player ile oynatılamaz, fallback atlandı"
                    )
                    interrupted = self._stop_requested
                    self._last_interrupted = interrupted
                    return interrupted
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

    def can_play_with_mpv_playlist(self, media_paths: list[str]) -> bool:
        if len(media_paths) < 2:
            return False

        if any(self._is_slideshow_manifest(path) for path in media_paths):
            return False

        for media_path in media_paths:
            if not Path(media_path).exists() or not self.supports_media(media_path):
                return False

        probe_path = media_paths[0]
        probe_command = self._build_command(probe_path)
        probe_command = self._prefer_non_vlc_image_command(probe_path, probe_command)
        if not probe_command:
            return False
        return self._is_mpv_command(probe_command)

    def play_mpv_playlist_blocking(self, media_paths: list[str], image_duration_sec: int | None = None) -> bool:
        if not self.can_play_with_mpv_playlist(media_paths):
            self._last_interrupted = False
            return False

        self.stop()

        duration = self.image_duration_sec if image_duration_sec is None else image_duration_sec
        playlist_file = None
        process = None

        try:
            first_command = self._build_command(media_paths[0])
            executable = self._strip_outer_quotes(first_command[0])
            if not self._resolve_executable([executable]):
                self._last_interrupted = False
                _safe_print(f"⚠️ player executable bulunamadı: {executable}")
                return False

            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", delete=False, suffix=".m3u") as handle:
                for media_path in media_paths:
                    handle.write(f"{media_path}\n")
                playlist_file = handle.name

            command = [
                executable,
                "--fs",
                "--border=no",
                "--force-window=immediate",
                "--ontop",
                "--quiet",
                "--background-color=0/0/0",
                "--osc=no",
                "--osd-level=0",
                "--input-cursor=no",
                f"--image-display-duration={duration}",
                "--loop-playlist=inf",
                f"--playlist={playlist_file}",
            ]

            self._stop_requested = False
            process = subprocess.Popen(command)
            self._process = process
            process.wait()

            interrupted = self._stop_requested
            self._last_interrupted = interrupted
            return process.returncode == 0 or interrupted
        except Exception as exc:
            self._last_interrupted = False
            _safe_print(f"⚠️ mpv playlist oynatma hatası: {exc}")
            return False
        finally:
            if self._process is process:
                self._process = None
            if playlist_file:
                Path(playlist_file).unlink(missing_ok=True)

    def stop(self):
        self._stop_requested = True

        if self._process and self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()

        if self._widget_process and self._widget_process.poll() is None:
            self._widget_process.terminate()
            try:
                self._widget_process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._widget_process.kill()

        self._process = None
        self._widget_process = None


    def last_play_was_interrupted(self) -> bool:
        return self._last_interrupted
