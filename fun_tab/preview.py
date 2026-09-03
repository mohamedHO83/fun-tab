"""Render the wheel to a PNG without installing the Alt+Tab hook.

    python -m fun_tab.preview            # use the real window list
    python -m fun_tab.preview --demo     # use fake apps
    python -m fun_tab.preview --light    # force the light theme
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

from PIL import Image, ImageDraw

from .config import Config
from .overlay import Overlay
from .windows_enum import AppWindow, enumerate_windows

DEMO_COLORS = [
    (86, 156, 255),
    (255, 128, 92),
    (120, 210, 140),
    (238, 190, 90),
    (196, 128, 240),
    (96, 214, 214),
    (240, 118, 160),
]


def _demo_apps(count: int = 7) -> list[AppWindow]:
    apps = []
    names = [
        ("Fun Tab — radial switcher", "Visual Studio Code"),
        ("Inbox (12)", "Google Chrome"),
        ("team-standup", "Slack"),
        ("Untitled Session", "Ableton Live"),
        ("notes.md", "Obsidian"),
        ("PowerShell", "Windows Terminal"),
        ("Now Playing", "Spotify"),
    ]
    for index in range(count):
        icon = Image.new("RGBA", (96, 96), (0, 0, 0, 0))
        draw = ImageDraw.Draw(icon)
        colour = DEMO_COLORS[index % len(DEMO_COLORS)]
        draw.rounded_rectangle((6, 6, 90, 90), 22, fill=colour + (255,))
        draw.ellipse((30, 30, 66, 66), fill=(255, 255, 255, 210))
        title, app_name = names[index % len(names)]
        apps.append(
            AppWindow(
                hwnd=index + 1,
                title=title,
                class_name="Demo",
                pid=0,
                icon=icon,
                exe_path=f"C:\\demo\\{app_name}.exe",
                app_name=app_name,
                minimized=index == 3,
            )
        )
    return apps


def render(apps: list[AppWindow], cfg: Config, selected: int = 1) -> Image.Image:
    overlay = Overlay(cfg)
    overlay._apps = list(apps)
    overlay._all_apps = list(apps)
    overlay._selected = max(0, min(selected, len(apps) - 1))
    overlay._apply_metrics(96)
    overlay._rebuild_layers()
    wheel = overlay._compose(time.perf_counter())
    card = overlay._build_card(apps[overlay._selected], 1.0)

    pad = 40
    width = wheel.width + card.width + pad * 3
    height = max(wheel.height, card.height) + pad * 2

    canvas = Image.new("RGBA", (width, height), (0, 0, 0, 255))
    backdrop = ImageDraw.Draw(canvas)
    top = (34, 38, 52) if overlay.theme.is_dark else (208, 214, 228)
    bottom = (14, 16, 22) if overlay.theme.is_dark else (240, 243, 250)
    for y in range(height):
        t = y / max(1, height - 1)
        backdrop.line(
            (0, y, width, y),
            fill=tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3)) + (255,),
        )

    canvas.alpha_composite(card, (pad, pad))
    canvas.alpha_composite(wheel, (card.width + pad * 2, pad))
    return canvas


def main() -> int:
    args = set(sys.argv[1:])
    cfg = Config()
    if "--light" in args:
        cfg.theme = "light"
    if "--dark" in args:
        cfg.theme = "dark"

    apps: list[AppWindow] = []
    if "--demo" not in args:
        try:
            apps = enumerate_windows()
        except Exception:
            apps = []
    if not apps:
        apps = _demo_apps()

    image = render(apps[:12], cfg)
    out = Path(__file__).resolve().parent.parent / "preview_wheel.png"
    image.convert("RGB").save(out)
    print(f"Wrote {out} ({len(apps[:12])} apps)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
