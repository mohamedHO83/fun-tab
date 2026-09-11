"""User settings and colour theming.

Settings live in ``%APPDATA%\\fun-tab\\config.json``. The file is written with
defaults on first run so it is easy to discover and hand-edit; unknown keys are
preserved, missing keys fall back to the defaults below.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

from . import win32_types as w

RGBA = tuple[int, int, int, int]


def config_dir() -> Path:
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return Path(base) / "fun-tab"


def config_path() -> Path:
    return config_dir() / "config.json"


@dataclass
class Config:
    # --- appearance -------------------------------------------------------
    theme: str = "auto"  # auto | dark | light
    accent: str = "system"  # "system" or "#RRGGBB"
    scale: float = 1.0  # extra size multiplier on top of monitor DPI
    outer_radius: int = 165
    inner_radius: int = 52
    icon_scale: float = 1.0

    # blur - freeze and blur the desktop behind the wheel (recommended)
    # dim  - the same frozen desktop, darkened but not blurred
    # none - leave the desktop alone
    #
    # There is deliberately no DWM-blur option. Both the accent-policy and
    # system-backdrop APIs report success on Windows 11 and then composite a
    # flat tint with no trace of the windows behind, which is where the
    # black backdrop came from.
    backdrop: str = "blur"

    # --- motion -----------------------------------------------------------
    appear_duration: float = 0.11
    transition_duration: float = 0.13
    max_fps: int = 144

    # --- backdrop ---------------------------------------------------------
    dim_blur: float = 1.0  # 0 = sharp desktop, 1 = fully soft
    dim_veil: int = 40  # extra darkening, 0-255
    dim_scale: int = 6  # capture/blur at 1/N resolution
    backdrop_ttl: float = 2.0  # reuse a captured plate for this long

    # --- preview card -----------------------------------------------------
    preview_enabled: bool = True
    preview_position: str = "top-left"  # top-left|top-right|bottom-left|bottom-right|bottom-center
    preview_width: int = 400
    preview_height: int = 225
    preview_margin: int = 48
    prefetch_previews: bool = True
    capture_minimized: bool = True
    thumb_ttl: float = 4.0  # seconds a captured thumbnail is reused before refresh

    # --- aiming -----------------------------------------------------------
    aim_needle: bool = True  # the needle in the hub, showing the aim direction
    aim_origin: bool = True  # a ring marking the point aiming is measured from

    # --- labels -----------------------------------------------------------
    show_counter: bool = True
    show_subtitle: bool = True
    show_hints: bool = True

    # --- behaviour --------------------------------------------------------
    mru_order: bool = True
    search_enabled: bool = True
    digit_jump: bool = True
    wrap_navigation: bool = True
    close_key_enabled: bool = True
    minimized_last: bool = False
    exclude_exes: list[str] = field(default_factory=list)
    exclude_titles: list[str] = field(default_factory=list)

    # off     - always take over Alt+Tab
    # always  - never take over Alt+Tab; open with Ctrl+Alt+Tab
    # auto    - the same as always, but only while a game is in front
    game_compat: str = "auto"
    game_exes: list[str] = field(default_factory=list)  # extra names auto-mode treats as games

    # How to open the wheel. Keyboard chords (`alt+tab`, `ctrl+shift+a`) or
    # mouse side-buttons (`mouse4`, `ctrl+mouse5`). Game-compat leaves plain
    # Alt+Tab to Windows; set a custom chord or mouse button for those games.
    open_hotkey: str = "alt+tab"
    # When True, the wheel stays up after the open shortcut is released.
    # When False, releasing Alt (or the mouse button / key) commits, like Alt+Tab.
    open_sticky: bool = False
    # When True, Fun Tab uninstalls its input hooks while a game is in front.
    # That is the strongest anti-cheat-friendly option short of quitting.
    pause_in_games: bool = True

    # --- raw colour overrides (theme key -> "#RRGGBB" or [r,g,b,a]) -------
    colors: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path | None = None) -> "Config":
        path = path or config_path()
        cfg = cls()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            cfg.save(path)  # first run (or corrupt file): write a fresh template
            return cfg
        if not isinstance(raw, dict):
            return cfg
        known = {f.name: f for f in fields(cls)}
        for key, value in raw.items():
            spec = known.get(key)
            if spec is None:
                continue
            try:
                setattr(cfg, key, _coerce(spec.type, value))
            except (TypeError, ValueError):
                continue
        cfg.clamp()
        return cfg

    def save(self, path: Path | None = None) -> bool:
        path = path or config_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(asdict(self), indent=2, sort_keys=False), encoding="utf-8"
            )
            return True
        except OSError:
            return False

    def clamp(self) -> None:
        self.scale = _clampf(self.scale, 0.5, 3.0)
        self.icon_scale = _clampf(self.icon_scale, 0.5, 2.0)
        self.outer_radius = int(_clampf(self.outer_radius, 80, 480))
        self.inner_radius = int(_clampf(self.inner_radius, 20, self.outer_radius - 24))
        self.appear_duration = _clampf(self.appear_duration, 0.0, 1.0)
        self.transition_duration = _clampf(self.transition_duration, 0.0, 1.0)
        self.max_fps = int(_clampf(self.max_fps, 30, 240))
        self.dim_blur = _clampf(self.dim_blur, 0.0, 1.0)
        self.dim_veil = int(_clampf(self.dim_veil, 0, 220))
        self.dim_scale = int(_clampf(self.dim_scale, 1, 16))
        self.backdrop_ttl = _clampf(self.backdrop_ttl, 0.0, 60.0)
        self.preview_width = int(_clampf(self.preview_width, 160, 1200))
        self.preview_height = int(_clampf(self.preview_height, 90, 800))
        self.preview_margin = int(_clampf(self.preview_margin, 0, 400))
        self.thumb_ttl = _clampf(self.thumb_ttl, 0.0, 3600.0)
        if self.theme not in ("auto", "dark", "light"):
            self.theme = "auto"
        # Names from older configs, kept working so an upgrade does not land
        # the user back on a backdrop that renders black.
        self.backdrop = {
            "acrylic": "blur",
            "snapshot": "blur",
            "solid": "dim",
            "off": "none",
        }.get(self.backdrop, self.backdrop)
        if self.backdrop not in ("blur", "dim", "none"):
            self.backdrop = "blur"
        if self.game_compat not in ("auto", "always", "off"):
            self.game_compat = "auto"
        from .hotkey import parse_hotkey

        self.open_hotkey = parse_hotkey(self.open_hotkey).text()


def _coerce(annotation: Any, value: Any) -> Any:
    text = annotation if isinstance(annotation, str) else getattr(annotation, "__name__", "")
    if text.startswith("bool"):
        return bool(value)
    if text.startswith("int"):
        return int(value)
    if text.startswith("float"):
        return float(value)
    if text.startswith("str"):
        return str(value)
    if text.startswith("list"):
        return [str(v) for v in value]
    if text.startswith("dict"):
        return dict(value)
    return value


def _clampf(value: float, low: float, high: float) -> float:
    return max(low, min(high, float(value)))


# ---------------------------------------------------------------------------
# Theme
# ---------------------------------------------------------------------------


def _registry_accent() -> tuple[int, int, int] | None:
    """The user's accent colour, as stored by DWM (ABGR).

    ``DwmGetColorizationColor`` returns a blended colourisation value that is
    often nothing like the accent swatch shown in Settings, so the registry is
    the reliable source.
    """
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\DWM") as key:
            value, _ = winreg.QueryValueEx(key, "AccentColor")
    except OSError:
        return None
    value = int(value)
    return (value & 0xFF, (value >> 8) & 0xFF, (value >> 16) & 0xFF)


def _system_prefers_light() -> bool:
    try:
        import winreg

        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
        )
        with key:
            value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
            return bool(value)
    except OSError:
        return False


def _parse_color(value: Any, fallback: RGBA) -> RGBA:
    if isinstance(value, (list, tuple)) and len(value) in (3, 4):
        r, g, b = (int(value[0]), int(value[1]), int(value[2]))
        a = int(value[3]) if len(value) == 4 else 255
        return (r & 255, g & 255, b & 255, a & 255)
    if isinstance(value, str):
        text = value.strip().lstrip("#")
        if len(text) in (6, 8):
            try:
                r = int(text[0:2], 16)
                g = int(text[2:4], 16)
                b = int(text[4:6], 16)
                a = int(text[6:8], 16) if len(text) == 8 else 255
                return (r, g, b, a)
            except ValueError:
                return fallback
    return fallback


def mix(a: RGBA, b: RGBA, t: float) -> RGBA:
    t = max(0.0, min(1.0, t))
    return tuple(int(round(a[i] + (b[i] - a[i]) * t)) for i in range(4))  # type: ignore[return-value]


def alpha(color: RGBA, value: int) -> RGBA:
    return (color[0], color[1], color[2], max(0, min(255, int(value))))


@dataclass(frozen=True)
class Theme:
    accent: RGBA
    ring_fill: RGBA
    ring_edge: RGBA
    slice_idle: RGBA
    slice_hover: RGBA
    divider: RGBA
    hub_fill: RGBA
    hub_edge: RGBA
    text: RGBA
    text_dim: RGBA
    card_fill: RGBA
    card_edge: RGBA
    shadow: RGBA
    is_dark: bool

    @classmethod
    def build(cls, cfg: Config) -> "Theme":
        dark = cfg.theme == "dark" or (cfg.theme == "auto" and not _system_prefers_light())

        accent: RGBA = (74, 158, 255, 255)
        if cfg.accent == "system":
            rgb = _registry_accent() or w.accent_rgb()
            if rgb:
                accent = _ensure_readable(rgb, dark)
        else:
            accent = _parse_color(cfg.accent, accent)

        if dark:
            base = dict(
                ring_fill=(26, 28, 34, 246),
                ring_edge=(58, 62, 74, 255),
                slice_idle=(32, 35, 42, 246),
                divider=(70, 74, 88, 190),
                hub_fill=(17, 19, 25, 255),
                hub_edge=(66, 71, 84, 255),
                text=(238, 241, 248, 255),
                text_dim=(146, 154, 170, 255),
                card_fill=(16, 18, 24, 240),
                card_edge=(64, 70, 84, 220),
                shadow=(0, 0, 0, 255),
            )
        else:
            base = dict(
                ring_fill=(247, 248, 251, 246),
                ring_edge=(206, 212, 224, 255),
                slice_idle=(238, 240, 246, 246),
                divider=(198, 204, 218, 200),
                hub_fill=(255, 255, 255, 255),
                hub_edge=(206, 212, 224, 255),
                text=(24, 27, 34, 255),
                text_dim=(104, 112, 128, 255),
                card_fill=(252, 253, 255, 244),
                card_edge=(200, 208, 222, 230),
                shadow=(20, 26, 40, 255),
            )

        hover = mix(base["slice_idle"], accent, 0.40 if dark else 0.26)
        values = dict(base, accent=accent, slice_hover=hover, is_dark=dark)

        for key, override in (cfg.colors or {}).items():
            if key in values and key != "is_dark":
                values[key] = _parse_color(override, values[key])  # type: ignore[arg-type]

        return cls(**values)  # type: ignore[arg-type]


def _ensure_readable(rgb: tuple[int, int, int], dark: bool) -> RGBA:
    """Nudge the system accent until it reads clearly against the wheel."""
    r, g, b = rgb
    luma = (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255.0
    if dark and luma < 0.35:
        t = (0.35 - luma) / 0.35
        r, g, b = (int(c + (255 - c) * t * 0.75) for c in (r, g, b))
    elif not dark and luma > 0.72:
        t = (luma - 0.72) / 0.28
        r, g, b = (int(c * (1 - t * 0.5)) for c in (r, g, b))
    return (r, g, b, 255)
