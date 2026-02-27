import sys
import tkinter as tk
from pathlib import Path


def _fit_size(img_w: int, img_h: int, max_w: int, max_h: int) -> tuple[int, int]:
    if img_w <= 0 or img_h <= 0:
        return max_w, max_h

    scale = min(max_w / img_w, max_h / img_h)
    scale = max(scale, 0.01)
    return max(1, int(img_w * scale)), max(1, int(img_h * scale))


def main() -> int:
    if len(sys.argv) < 3:
        print("Usage: python image_viewer.py <image_path> <duration_sec>")
        return 2

    image_path = Path(sys.argv[1])
    duration_sec = int(float(sys.argv[2]))

    if not image_path.exists():
        print(f"⚠️ image bulunamadı: {image_path}")
        return 2

    try:
        from PIL import Image, ImageTk
    except Exception as exc:
        print(f"⚠️ Pillow import edilemedi: {exc}")
        return 3

    root = tk.Tk()
    root.configure(bg="black")
    root.attributes("-fullscreen", True)
    root.attributes("-topmost", True)
    root.title("Baylan Dijital Bilgi")

    try:
        image = Image.open(image_path).convert("RGB")
    except Exception as exc:
        print(f"⚠️ image açılamadı: {exc}")
        root.destroy()
        return 4

    screen_w = root.winfo_screenwidth()
    screen_h = root.winfo_screenheight()
    target_w, target_h = _fit_size(image.width, image.height, screen_w, screen_h)

    if (target_w, target_h) != (image.width, image.height):
        image = image.resize((target_w, target_h), Image.Resampling.LANCZOS)

    tk_image = ImageTk.PhotoImage(image)

    label = tk.Label(root, image=tk_image, bg="black")
    label.place(relx=0.5, rely=0.5, anchor="center")

    def close(*_):
        if root.winfo_exists():
            root.destroy()

    root.bind("<Escape>", close)
    root.after(max(1, duration_sec) * 1000, close)

    try:
        root.mainloop()
    except KeyboardInterrupt:
        close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
