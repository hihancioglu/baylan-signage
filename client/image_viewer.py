import json
import importlib.util
import traceback
import os
import subprocess
import sys
import time
from pathlib import Path

import pygame
from PIL import Image


BACKGROUND_COLOR = (0, 0, 0)


def _debug_log(message: str):
    log_target = os.getenv("BAYLAN_DEBUG_LOG", "")
    if log_target.strip():
        log_path = Path(log_target).expanduser()
    elif getattr(sys, "frozen", False):
        log_path = Path(sys.executable).resolve().with_name("baylan_agent_debug.log")
    else:
        log_path = Path(__file__).resolve().with_name("baylan_agent_debug.log")

    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(f"[{timestamp}] [image_viewer] {message}\n")
    except Exception:
        pass


def _fit_size(img_w: int, img_h: int, max_w: int, max_h: int) -> tuple[int, int]:
    if img_w <= 0 or img_h <= 0:
        return max_w, max_h

    scale = min(max_w / img_w, max_h / img_h)
    scale = max(scale, 0.01)
    return max(1, int(img_w * scale)), max(1, int(img_h * scale))


def _load_slide_manifest(manifest_path: Path, default_duration_sec: int) -> list[tuple[Path, int]]:
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    slides = []
    for raw in payload.get("slides", []):
        image_value = str((raw or {}).get("image", "")).strip()
        if not image_value:
            continue

        image_path = (manifest_path.parent / image_value).resolve()
        if not image_path.exists():
            continue

        duration_sec = int((raw or {}).get("duration_sec", default_duration_sec) or default_duration_sec)
        slides.append((image_path, max(1, duration_sec)))
    return slides


def _load_slides(source: Path, default_duration_sec: int) -> list[tuple[Path, int]]:
    if source.suffix.lower() == ".json":
        return _load_slide_manifest(source, default_duration_sec)

    if not source.exists():
        return []

    return [(source, max(1, default_duration_sec))]


def _draw_image(screen, image_surface):
    screen.fill(BACKGROUND_COLOR)
    screen_rect = screen.get_rect()
    img_rect = image_surface.get_rect(center=screen_rect.center)
    screen.blit(image_surface, img_rect)
    pygame.display.flip()


def _load_image_surface(image_path: Path):
    try:
        img = Image.open(image_path)
        img = img.convert("RGB")
        mode = img.mode
        size = img.size
        data = img.tobytes()
        return pygame.image.fromstring(data, size, mode).convert()
    except Exception as pil_exc:
        print(f"⚠️ PIL ile image açılamadı, pygame fallback deneniyor: {image_path} | {pil_exc}")

    return pygame.image.load(str(image_path)).convert()


def _run_agent_fallback() -> int:
    """Fallback: when built with wrong entrypoint, run the real agent instead of exiting."""
    _debug_log(f"fallback başladı | argv={sys.argv!r} | frozen={getattr(sys, 'frozen', False)}")
    agent_main = None

    client_py = Path(__file__).with_name("client.py")
    if client_py.exists():
        try:
            spec = importlib.util.spec_from_file_location("baylan_agent_entry", client_py)
            if spec and spec.loader:
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                candidate = getattr(module, "main", None)
                if callable(candidate):
                    agent_main = candidate
        except Exception:
            _debug_log(f"client.py üzerinden import başarısız: {traceback.format_exc().strip()}")
            agent_main = None

    if agent_main is None:
        for module_name in ("client", "client.client"):
            try:
                imported = __import__(module_name, fromlist=["main"])
                candidate = getattr(imported, "main", None)
                if callable(candidate):
                    agent_main = candidate
                    break
            except Exception:
                _debug_log(f"{module_name} import başarısız: {traceback.format_exc().strip()}")
                continue

    if agent_main is None and getattr(sys, "frozen", False):
        executable_dir = Path(sys.executable).resolve().parent
        viewer_executable = Path(sys.executable).resolve()
        env = dict(os.environ)
        env.setdefault("BAYLAN_IMAGE_VIEWER_FALLBACK", "1")

        fallback_commands: list[list[str]] = []
        sidecar_client_py = executable_dir / "client.py"
        if sidecar_client_py.exists():
            fallback_commands.append([sys.executable, str(sidecar_client_py)])

        for candidate_name in (
            "BaylanSignageAgent.exe",
            "agent.exe",
            "BaylanSignageAgent",
            "agent",
        ):
            candidate = executable_dir / candidate_name
            if not candidate.exists() or candidate.resolve() == viewer_executable:
                continue
            fallback_commands.append([str(candidate)])

        for command in fallback_commands:
            try:
                _debug_log(f"sidecar fallback çalıştırılıyor: {command}")
                return subprocess.call(command, env=env)
            except Exception:
                _debug_log(f"sidecar çağrısı başarısız ({command}): {traceback.format_exc().strip()}")

    try:
        run_agent = agent_main
        if run_agent is None:
            raise RuntimeError("client.main bulunamadı")
    except Exception as exc:
        print("Usage: python image_viewer.py <image_or_manifest_path> <duration_sec>")
        print(f"⚠️ agent fallback başlatılamadı: {exc}")
        _debug_log(f"agent fallback başlatılamadı: {traceback.format_exc().strip()}")
        return 2

    print("⚠️ image_viewer argümanı verilmedi, agent başlatılıyor...")
    _debug_log("agent_main() çağrılıyor")
    run_agent()
    _debug_log("agent_main() tamamlandı")
    return 0


def main() -> int:
    _debug_log(f"main başladı | argv={sys.argv!r}")
    if len(sys.argv) < 3:
        return _run_agent_fallback()

    source_path = Path(sys.argv[1]).resolve()
    default_duration_sec = int(float(sys.argv[2]))

    if not source_path.exists():
        print(f"⚠️ dosya bulunamadı: {source_path}")
        _debug_log(f"source bulunamadı: {source_path}")
        return 2

    try:
        slides = _load_slides(source_path, default_duration_sec)
    except Exception as exc:
        print(f"⚠️ slayt manifest okunamadı: {exc}")
        _debug_log(f"slayt manifest okunamadı: {traceback.format_exc().strip()}")
        return 3

    if not slides:
        print("⚠️ gösterilecek slayt bulunamadı")
        _debug_log("gösterilecek slayt bulunamadı")
        return 3

    pygame.init()
    pygame.display.set_caption("Baylan Dijital Bilgi")

    flags = pygame.FULLSCREEN
    screen = pygame.display.set_mode((0, 0), flags)
    screen_w, screen_h = screen.get_size()
    clock = pygame.time.Clock()

    try:
        for image_path, duration_sec in slides:
            try:
                image_surface = _load_image_surface(image_path)
            except Exception as exc:
                print(f"⚠️ image açılamadı: {image_path} | {exc}")
                _debug_log(f"image açılamadı: {image_path} | {traceback.format_exc().strip()}")
                continue

            target_w, target_h = _fit_size(
                image_surface.get_width(),
                image_surface.get_height(),
                screen_w,
                screen_h,
            )
            image_surface = pygame.transform.smoothscale(image_surface, (target_w, target_h))
            _draw_image(screen, image_surface)

            deadline = time.monotonic() + max(1, duration_sec)
            while time.monotonic() < deadline:
                for event in pygame.event.get():
                    if event.type == pygame.QUIT:
                        return 0
                    if event.type == pygame.KEYDOWN and event.key == pygame.K_ESCAPE:
                        return 0
                clock.tick(30)
    except KeyboardInterrupt:
        _debug_log("keyboard interrupt")
        return 0
    finally:
        pygame.quit()
        _debug_log("pygame quit")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
