"""Render the wheel to a PNG without installing the Alt+Tab hook.

    python -m fun_tab.preview            # use the real window list
    python -m fun_tab.preview --demo     # use fake apps
    python -m fun_tab.preview --scene    # fake desktop + wheel, safe for docs
    python -m fun_tab.preview --light    # force the light theme
    python -m fun_tab.preview --scene --out=assets/fun-tab-open.png
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

from PIL import Image, ImageDraw

from .config import Config
from .overlay import Overlay, treat_plate
from .windows_enum import AppWindow, capture_thumbnail, enumerate_windows

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


class ScenePreview:
    """Renders "what the screen will look like" for a fixed desktop shot.

    Built from a real screenshot and put through the same plate treatment the
    overlay uses, so the settings window shows what a change actually does
    rather than describing it. Shrinking the screenshot costs more than
    everything else put together and only depends on ``dim_scale``, so it is
    kept between renders — the settings sliders redraw this constantly.
    """

    def __init__(
        self,
        desktop: Image.Image,
        apps: list[AppWindow],
        *,
        dpi: int = 96,
        selected: int = 1,
        origin: tuple[int, int] | None = None,
    ) -> None:
        self.size = desktop.size
        self.apps = list(apps)
        self.dpi = dpi
        self.selected = max(0, min(selected, len(self.apps) - 1))
        self.origin = origin
        self._desktop = desktop.convert("RGB")
        self._shrunk: dict[int, Image.Image] = {}
        # A real capture of the window the preview card is showing. Without it
        # the card falls back to its "loading preview…" placeholder, which
        # looks like the settings window is broken rather than like the card.
        self._thumb: Image.Image | None = None
        if self.apps:
            chosen = self.apps[self.selected]
            # Demo hwnds are fake integers; capturing them can grab a real window.
            if chosen.class_name != "Demo":
                try:
                    self._thumb = capture_thumbnail(chosen.hwnd)
                except Exception:
                    self._thumb = None

    def render(self, cfg: Config, fit: tuple[int, int] | None = None) -> Image.Image:
        width, height = self.size
        # Laid out in screen pixels and shrunk on the way in, so the wheel
        # keeps its true proportion to the screen however small the panel is.
        shrink = 1.0
        if fit is not None:
            shrink = min(fit[0] / width, fit[1] / height, 1.0)
        out = (max(1, round(width * shrink)), max(1, round(height * shrink)))

        overlay = Overlay(cfg)
        overlay._apps = list(self.apps)
        overlay._all_apps = list(self.apps)
        overlay._selected = max(0, min(self.selected, len(self.apps) - 1))
        overlay._apply_metrics(self.dpi)
        overlay._rebuild_layers()

        scene = self._backdrop(cfg, out)

        def place(img: Image.Image, x: float, y: float) -> None:
            if shrink != 1.0:
                img = img.resize(
                    (max(1, round(img.width * shrink)), max(1, round(img.height * shrink))),
                    Image.Resampling.LANCZOS,
                )
            scene.alpha_composite(img, (round(x * shrink), round(y * shrink)))

        if cfg.aim_origin and self.origin is not None:
            marker = overlay._origin_plate(True)
            place(
                marker,
                self.origin[0] - marker.width / 2,
                self.origin[1] - marker.height / 2,
            )

        wheel = overlay._compose(time.perf_counter())
        if cfg.aim_needle and overlay._layers is not None:
            # Aimed down the middle of the selected slice, so the needle agrees
            # with the highlight instead of looking like a stray mark.
            wheel = wheel.copy()
            overlay._draw_needle(
                wheel, overlay._layers.layout.slices[overlay._selected].mid
            )
        place(wheel, (width - wheel.width) / 2, (height - wheel.height) / 2)

        if cfg.preview_enabled and self.apps:
            chosen = self.apps[overlay._selected]
            if self._thumb is not None:
                overlay._thumbs[chosen.hwnd] = self._thumb
            card = overlay._build_card(chosen, 1.0)
            place(card, *overlay._preview_position(0, 0, width, height))

        return scene

    def _backdrop(self, cfg: Config, out: tuple[int, int]) -> Image.Image:
        """The desktop with the configured backdrop applied, at the output size."""
        if cfg.backdrop == "none":
            return self._desktop.convert("RGBA").resize(out, Image.Resampling.LANCZOS)

        scale = max(1, cfg.dim_scale)
        small = self._shrunk.get(scale)
        if small is None:
            width, height = self.size
            small = self._desktop.resize(
                (max(8, width // scale), max(8, height // scale)),
                Image.Resampling.BILINEAR,
            )
            self._shrunk[scale] = small
        # Stretched straight to the output size the way the backdrop window
        # blows it up, so the softness of the low-res capture shows here too.
        return treat_plate(small, cfg).resize(out, Image.Resampling.BILINEAR)


def demo_desktop(size: tuple[int, int] = (1600, 900)) -> Image.Image:
    """A fake wallpaper with no personal content, for docs screenshots."""
    width, height = size
    img = Image.new("RGB", (width, height), (18, 22, 32))
    draw = ImageDraw.Draw(img)
    for y in range(height):
        mix = y / max(height - 1, 1)
        colour = (
            int(18 + 10 * mix),
            int(22 + 8 * mix),
            int(32 + 14 * mix),
        )
        draw.line((0, y, width, y), fill=colour)
    windows = (
        ((80, 70, 720, 520), (42, 48, 64)),
        ((760, 90, 1520, 560), (36, 58, 72)),
        ((120, 560, 640, 820), (58, 44, 70)),
        ((680, 590, 1180, 840), (40, 52, 48)),
    )
    for box, fill in windows:
        draw.rounded_rectangle(box, 16, fill=fill)
        x0, y0, x1, _y1 = box
        draw.rectangle((x0, y0, x1, y0 + 28), fill=tuple(max(0, c - 12) for c in fill))
    draw.rectangle((0, height - 48, width, height), fill=(28, 32, 42))
    return img


def demo_thumb(size: tuple[int, int] = (800, 450)) -> Image.Image:
    """A fake window preview so docs shots never PrintWindow a real hwnd."""
    width, height = size
    img = Image.new("RGB", (width, height), (30, 32, 38))
    draw = ImageDraw.Draw(img)
    draw.rectangle((0, 0, 56, height), fill=(24, 26, 32))
    for i, y in enumerate(range(24, height - 24, 18)):
        length = 180 + (i * 37) % 220
        draw.rounded_rectangle((80, y, 80 + length, y + 10), 3, fill=(70, 78, 96))
    return img


def demo_scene(*, dpi: int = 96) -> ScenePreview:
    """Wheel + card over a fake desktop — safe to commit and to show on GitHub."""
    width, height = 1600, 900
    scene = ScenePreview(
        demo_desktop((width, height)),
        _demo_apps(6),
        dpi=dpi,
        selected=1,
        origin=(width * 3 // 4, height * 2 // 3),
    )
    scene._thumb = demo_thumb()
    return scene


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

    # Keep the outside area transparent so the PNG does not ship with a
    # black background behind the wheel/card.
    canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    backdrop = ImageDraw.Draw(canvas)
    top = (34, 38, 52) if overlay.theme.is_dark else (208, 214, 228)
    bottom = (14, 16, 22) if overlay.theme.is_dark else (240, 243, 250)
    # Note: we only draw the decorative gradient if you are exporting without
    # transparency. For the current user-facing export we keep the background
    # transparent and only draw the actual UI elements (card and wheel).

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

    out = Path(__file__).resolve().parent.parent / "preview_wheel.png"
    for arg in sys.argv[1:]:
        if arg.startswith("--out="):
            out = Path(arg.split("=", 1)[1])

    if "--scene" in args:
        image = demo_scene().render(cfg)
        image.save(out)
        print(f"Wrote {out} (demo scene)")
        return 0

    apps: list[AppWindow] = []
    if "--demo" not in args:
        try:
            apps = enumerate_windows()
        except Exception:
            apps = []
    if not apps:
        apps = _demo_apps()

    image = render(apps[:12], cfg)
    # Preserve alpha so the preview wheel PNG has no black background.
    image.save(out)
    print(f"Wrote {out} ({len(apps[:12])} apps)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
