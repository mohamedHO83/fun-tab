"""Render a preview of the wheel without installing the Alt+Tab hook."""

from __future__ import annotations

import time
from pathlib import Path

from PIL import Image, ImageDraw

from .overlay import Overlay
from .windows_enum import AppWindow, enumerate_windows


def main() -> int:
    apps = enumerate_windows()
    if not apps:
        icon = Image.new("RGBA", (48, 48), (0, 0, 0, 0))
        d = ImageDraw.Draw(icon)
        d.rounded_rectangle((4, 4, 44, 44), 8, fill=(200, 200, 210, 255))
        apps = [
            AppWindow(1, "Preview App", "Demo", 0, icon),
            AppWindow(2, "Another - Game", "Demo", 0, icon),
            AppWindow(3, "Chat", "Demo", 0, icon),
            AppWindow(4, "Browser", "Demo", 0, icon),
            AppWindow(5, "Music", "Demo", 0, icon),
            AppWindow(6, "Notes", "Demo", 0, icon),
        ]

    apps = apps[:6]
    ov = Overlay()
    ov._apps = apps
    ov._selected = 1 if len(apps) > 1 else 0
    ov._appear_t = time.perf_counter() - 1.0
    ov._visible = True
    ov._monitor = (0, 0, ov.CANVAS_W, ov.CANVAS_H)
    ov._wheel_origin = (0, 0)

    saved = []

    def capture(hwnd, img, left, top, dest_size=None):
        saved.append(img.copy())

    ov._blit_to = capture  # type: ignore
    ov._paint(force=True)

    out = Path(__file__).resolve().parent.parent / "preview_wheel.png"
    if saved:
        saved[0].save(out)
        print(f"Wrote {out} ({len(apps)} apps)")
        return 0
    print("Nothing rendered")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
