import json
import sys
import time
from pathlib import Path

import pygame
from PIL import Image


BACKGROUND_COLOR = (0, 0, 0)


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


def main() -> int:
    if len(sys.argv) < 3:
        print("Usage: python image_viewer.py <image_or_manifest_path> <duration_sec>")
        return 2

    source_path = Path(sys.argv[1]).resolve()
    default_duration_sec = int(float(sys.argv[2]))

    if not source_path.exists():
        print(f"⚠️ dosya bulunamadı: {source_path}")
        return 2

    try:
        slides = _load_slides(source_path, default_duration_sec)
    except Exception as exc:
        print(f"⚠️ slayt manifest okunamadı: {exc}")
        return 3

    if not slides:
        print("⚠️ gösterilecek slayt bulunamadı")
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
        return 0
    finally:
        pygame.quit()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
