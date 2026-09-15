"""Layered overlay: dimmed backdrop, radial wheel, and a live preview card.

Rendering strategy
------------------
The wheel is split into cached layers instead of being repainted from scratch:

* ``base``  — shadow, ring, dividers, icons, drawn once per window set
* ``hl``    — the highlighted slice, one small bitmap per index
* ``hub``   — the centre disc with the ``n/total`` counter, per index
* ``label`` — the title / subtitle / hint band, per index

Changing selection therefore costs one copy plus three small composites, and
re-opening the wheel on an unchanged window set costs nothing at all. Every
layer is authored at 2x and downscaled, so edges stay clean.
"""

from __future__ import annotations

import ctypes
import math
import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from typing import Callable, Optional

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from . import win32_types as w
from .config import Config, Theme, alpha
from .config import mix as _mix
from .gdi import LayeredSurface, ScreenGrabber
from .wheel import WheelLayout, build_layout, pie_points
from .windows_enum import AppWindow, capture_thumbnail

RENDER_SCALE = 2
PREVIEW_FADE = 0.13
# Aim angles are snapped to this, so a jittering pointer cannot cost a repaint
# per frame while still tracking smoothly by eye.
AIM_STEP = math.radians(1.5)

TITLE_FONTS = (
    r"C:\Windows\Fonts\seguisb.ttf",
    r"C:\Windows\Fonts\segoeui.ttf",
    r"C:\Windows\Fonts\arial.ttf",
)
BODY_FONTS = (
    r"C:\Windows\Fonts\segoeui.ttf",
    r"C:\Windows\Fonts\arial.ttf",
)

_FONT_CACHE: dict[tuple[str, int], ImageFont.ImageFont] = {}


def _font(paths: tuple[str, ...], size: float) -> ImageFont.ImageFont:
    px = max(8, int(round(size)))
    key = (paths[0], px)
    cached = _FONT_CACHE.get(key)
    if cached is not None:
        return cached
    for path in paths:
        try:
            loaded = ImageFont.truetype(path, px)
            break
        except OSError:
            continue
    else:
        loaded = ImageFont.load_default()
    _FONT_CACHE[key] = loaded
    return loaded


def _ease_out_cubic(t: float) -> float:
    return 1 - (1 - t) ** 3


def treat_plate(plate: Image.Image, cfg: Config) -> Image.Image:
    """Blur and darken a captured desktop plate as the backdrop settings ask.

    Shared with the settings window so its preview is the same pixels the
    overlay will show, rather than an approximation that drifts.
    """
    # "dim" is the same frozen plate with the blur skipped: cheaper and
    # predictable. "blur" aims for a frosted/acrylic-ish look by combining
    # a modest blur radius with a translucent tint blend.
    amount = 0.0 if cfg.backdrop == "dim" else cfg.dim_blur

    if amount > 0.01:
        # Keep the blur radius smaller than the old implementation. The
        # previous mapping made the blur feel "smudgy" instead of glass.
        # Slightly larger blur radius to hide the low-resolution capture
        # (dim_scale) when stretching the plate back up.
        radius = max(1.0, min(plate.size) / 20.0) * amount
        soft = plate.filter(ImageFilter.BoxBlur(radius))
        plate = soft if amount > 0.99 else Image.blend(plate, soft, amount)

    if cfg.dim_veil > 0:
        if cfg.backdrop == "dim":
            # Plain darken for the "dim" mode.
            if plate.mode != "RGB":
                plate = plate.convert("RGB")
            factor = max(0.0, 1.0 - cfg.dim_veil / 255.0)
            plate = plate.point([int(v * factor) for v in range(256)] * 3)
        else:
            # Acrylic-ish tint blend: translucent uniform veil rather than a
            # multiplicative darken, so the backdrop still reads as colour.
            theme = Theme.build(cfg)
            if plate.mode != "RGBA":
                plate = plate.convert("RGBA")
            tint_rgb = theme.card_fill[:3]
            tint = Image.new("RGBA", plate.size, tint_rgb + (255,))
            # dim_veil is 0..220-ish; map it to a softer 0..~0.52 tint.
            strength = max(0.0, min(1.0, cfg.dim_veil / 220.0)) * 0.52
            plate = Image.blend(plate, tint, strength)

    return plate.convert("RGBA")


def _text_width(draw: ImageDraw.ImageDraw, text: str, font) -> float:
    if not text:
        return 0.0
    box = draw.textbbox((0, 0), text, font=font)
    return box[2] - box[0]


def _ellipsize(draw: ImageDraw.ImageDraw, text: str, font, max_width: float) -> str:
    if not text or _text_width(draw, text, font) <= max_width:
        return text
    low, high = 0, len(text)
    while low < high:
        mid = (low + high + 1) // 2
        if _text_width(draw, text[:mid] + "…", font) <= max_width:
            low = mid
        else:
            high = mid - 1
    return (text[:low] + "…") if low else "…"


def _centered(draw: ImageDraw.ImageDraw, text: str, cx: float, cy: float, font, fill) -> None:
    if not text:
        return
    try:
        draw.text((cx, cy), text, font=font, fill=fill, anchor="mm")
    except (ValueError, AttributeError):
        box = draw.textbbox((0, 0), text, font=font)
        draw.text(
            (cx - (box[2] - box[0]) / 2, cy - (box[3] - box[1]) / 2),
            text,
            font=font,
            fill=fill,
        )


def _scale_alpha(img: Image.Image, factor: float) -> Image.Image:
    """Uniformly fade an image.

    Pillow applies `point` through a 256-entry lookup table in C. Going via
    NumPy instead means `np.array` packing the image to bytes, a float
    round-trip and a repack -- four passes and three allocations for what is
    one pass over a single channel.
    """
    if factor >= 0.999:
        return img
    if factor <= 0.001:
        return Image.new("RGBA", img.size, (0, 0, 0, 0))
    faded = img.copy()
    faded.putalpha(img.getchannel("A").point(lambda value: int(value * factor)))
    return faded


@dataclass
class Layer:
    """A pre-rendered bitmap and where it sits on the 1x canvas."""

    img: Image.Image
    pos: tuple[int, int]


@dataclass
class WheelLayers:
    base: Image.Image
    layout: WheelLayout
    hl: dict[int, Layer] = field(default_factory=dict)
    hub: dict[int, Layer] = field(default_factory=dict)
    label: dict[int, Layer] = field(default_factory=dict)
    frames: dict[int, Image.Image] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Background preview capture
# ---------------------------------------------------------------------------


class PreviewWorker:
    """Serialised off-thread window captures, newest selection first.

    PrintWindow can take tens of milliseconds and minimised windows need an
    off-screen restore, so none of it may touch the UI thread. One worker keeps
    the ordering predictable and avoids stampeding a dozen apps at once.
    """

    def __init__(self, on_ready: Callable[[int, int, Image.Image], None]) -> None:
        self._on_ready = on_ready
        self._cv = threading.Condition()
        self._queue: deque[tuple[int, int, bool]] = deque()
        self._urgent: Optional[tuple[int, int, bool]] = None
        self._gen = 0
        self._stop = False
        self._thread: Optional[threading.Thread] = None
        self.size = (400, 225)

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name="fun-tab-preview", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        with self._cv:
            self._stop = True
            self._cv.notify_all()

    def new_generation(self) -> int:
        with self._cv:
            self._gen += 1
            self._queue.clear()
            self._urgent = None
            return self._gen

    @property
    def generation(self) -> int:
        return self._gen

    def request(self, hwnd: int, *, urgent: bool, allow_minimized: bool) -> None:
        with self._cv:
            item = (self._gen, int(hwnd), allow_minimized)
            if urgent:
                self._urgent = item
            else:
                self._queue.append(item)
            self._cv.notify()

    def _run(self) -> None:
        while True:
            with self._cv:
                while not self._stop and self._urgent is None and not self._queue:
                    self._cv.wait()
                if self._stop:
                    return
                if self._urgent is not None:
                    item = self._urgent
                    self._urgent = None
                else:
                    item = self._queue.popleft()
                current = self._gen
                width, height = self.size

            gen, hwnd, allow_minimized = item
            if gen != current:
                continue
            try:
                img = capture_thumbnail(
                    hwnd, width, height, allow_minimized=allow_minimized
                )
            except Exception:
                img = None
            if img is not None:
                self._on_ready(gen, hwnd, img)


# ---------------------------------------------------------------------------
# Overlay
# ---------------------------------------------------------------------------


class Overlay:
    def __init__(self, cfg: Optional[Config] = None) -> None:
        self.cfg = cfg or Config()
        self.theme = Theme.build(self.cfg)
        self._theme_rev = 0

        self.hwnd = 0
        self._dim_hwnd = 0
        self._preview_hwnd = 0
        self._origin_hwnd = 0
        self._wndprocs: list = []

        self._dim = LayeredSurface()
        self._wheel = LayeredSurface()
        self._preview = LayeredSurface()
        self._origin = LayeredSurface()
        self._grabber = ScreenGrabber()  # UI thread: stretching the plate out

        # Backdrop capture. The worker gets its own grabber so a capture in
        # flight never shares a DC with the paint that is presenting one.
        self._capture_grabber = ScreenGrabber()
        self._capture_lock = threading.Lock()
        self._capture_thread: Optional[threading.Thread] = None
        self._plate_lock = threading.Lock()
        self._plate: Optional[Image.Image] = None
        self._plate_rect: tuple[int, int, int, int] = (0, 0, 0, 0)
        self._plate_at = 0.0
        self._backdrop_plate: Optional[Image.Image] = None

        self._visible = False
        self._sticky = False
        self._apps: list[AppWindow] = []
        self._all_apps: list[AppWindow] = []
        self._selected = 0
        self._query = ""
        self._query_matched = True

        self._monitor = (0, 0, 1920, 1080)
        self._dpi = 96
        self._wheel_origin = (0, 0)
        self._preview_origin = (0, 0)

        self._on_commit: Optional[Callable[[], None]] = None
        self._on_cancel: Optional[Callable[[], None]] = None
        self.compat_active = False

        self._layers: Optional[WheelLayers] = None
        self._layer_cache: OrderedDict[tuple, WheelLayers] = OrderedDict()
        self._plate_cache: dict[tuple, Image.Image] = {}
        self._icon_cache: dict[tuple, Image.Image] = {}
        self._icon_variants: dict[tuple, Image.Image] = {}
        self._wedge_cache: dict[tuple, Layer] = {}
        self._hub_cache: dict[tuple, Layer] = {}
        self._label_cache: dict[tuple, Layer] = {}

        self._shown_at = 0.0
        self._last_frame = 0.0
        self._sel_from = -1
        self._sel_t0 = 0.0
        self._dirty = True
        self._written_index = -2

        self._thumbs: dict[int, Image.Image] = {}
        self._thumb_stamp: dict[int, int] = {}
        self._thumb_at: dict[int, float] = {}
        self._thumb_seq = 0
        self._pending: list[tuple[int, int, Image.Image]] = []
        self._pending_lock = threading.Lock()
        self._worker = PreviewWorker(self._on_capture)
        self._preview_t0 = 0.0
        self._card_key: tuple = ()
        self._card_cache: dict[tuple, Image.Image] = {}
        self._chrome_cache: dict[tuple, tuple[Image.Image, Image.Image]] = {}
        self._placeholder_cache: dict[tuple, Image.Image] = {}

        # Aim origin, and the last cursor position seen by the frame loop.
        self._mouse_anchor: Optional[tuple[int, int]] = None
        self._last_cursor: Optional[tuple[int, int]] = None
        self._mouse_slop = 8
        # Direction the cursor is aiming, drawn as a tick on the rim. None
        # whenever the pointer is not the authority, so it disappears the
        # moment a key is pressed.
        self._aim_angle: Optional[float] = None
        self._written_aim: Optional[float] = None
        # The pivot marker. Kept separately from `_mouse_anchor` because it
        # stays put, greyed out, while the keyboard is driving — the anchor
        # itself is cleared then, and a marker that vanished on every keypress
        # would defeat the point of showing where aiming starts from.
        self._origin_at: Optional[tuple[int, int]] = None
        self._origin_live = False
        self._origin_shown: tuple = ()
        self._origin_cache: dict[tuple, Image.Image] = {}

        self._apply_metrics(96)

    # -- properties --------------------------------------------------------

    @property
    def visible(self) -> bool:
        return self._visible

    @property
    def sticky(self) -> bool:
        return self._sticky

    @property
    def selected_index(self) -> int:
        return self._selected

    @property
    def apps(self) -> list[AppWindow]:
        return self._apps

    @property
    def query(self) -> str:
        return self._query

    def selected_app(self) -> Optional[AppWindow]:
        if self._apps and 0 <= self._selected < len(self._apps):
            return self._apps[self._selected]
        return None

    def set_callbacks(
        self, on_commit: Callable[[], None], on_cancel: Callable[[], None]
    ) -> None:
        self._on_commit = on_commit
        self._on_cancel = on_cancel

    # -- lifecycle ---------------------------------------------------------

    def create(self) -> int:
        hinstance = w.kernel32.GetModuleHandleW(None)
        self._register(hinstance, "FunTabDimClass", self._make_passive_proc())
        self._register(hinstance, "FunTabWheelClass", self._make_wheel_proc())
        self._register(hinstance, "FunTabPreviewClass", self._make_passive_proc())
        self._register(hinstance, "FunTabOriginClass", self._make_passive_proc())

        common_ex = (
            w.WS_EX_LAYERED | w.WS_EX_TOPMOST | w.WS_EX_TOOLWINDOW | w.WS_EX_NOACTIVATE
        )

        def make(class_name: str, title: str) -> int:
            hwnd = w.user32.CreateWindowExW(
                common_ex,
                class_name,
                title,
                w.WS_POPUP,
                0,
                0,
                16,
                16,
                None,
                None,
                hinstance,
                None,
            )
            return int(hwnd or 0)

        self._dim_hwnd = make("FunTabDimClass", "Fun Tab Backdrop")
        self.hwnd = make("FunTabWheelClass", "Fun Tab")
        self._preview_hwnd = make("FunTabPreviewClass", "Fun Tab Preview")
        self._origin_hwnd = make("FunTabOriginClass", "Fun Tab Origin")
        if not (self._dim_hwnd and self.hwnd and self._preview_hwnd and self._origin_hwnd):
            raise OSError("Failed to create overlay windows")

        self._dim.hwnd = self._dim_hwnd
        self._wheel.hwnd = self.hwnd
        self._preview.hwnd = self._preview_hwnd
        self._origin.hwnd = self._origin_hwnd
        self._worker.start()
        return self.hwnd

    def destroy(self) -> None:
        self._worker.stop()
        for surface in (self._dim, self._wheel, self._preview, self._origin):
            surface.destroy()
        thread = self._capture_thread
        if thread is not None:
            thread.join(0.5)
        self._grabber.destroy()
        self._capture_grabber.destroy()

    def own_hwnds(self) -> tuple[int, ...]:
        return (self._dim_hwnd, self.hwnd, self._preview_hwnd, self._origin_hwnd)

    def apply_config(self, cfg: Config) -> None:
        self.cfg = cfg
        self.theme = Theme.build(cfg)
        self._theme_rev += 1
        self._layer_cache.clear()
        self._layers = None
        self._plate_cache.clear()
        self._icon_cache.clear()
        self._icon_variants.clear()
        self._wedge_cache.clear()
        self._hub_cache.clear()
        self._label_cache.clear()
        self._card_cache.clear()
        self._chrome_cache.clear()
        self._placeholder_cache.clear()
        self._origin_cache.clear()
        self._origin_shown = ()
        self._thumbs.clear()
        self._thumb_at.clear()
        self._thumb_stamp.clear()
        with self._plate_lock:
            self._plate = None  # blur/veil/scale may all have moved
        self._backdrop_plate = None
        self._apply_metrics(self._dpi)

    def prewarm_render(self, apps: list[AppWindow]) -> None:
        """Build the caches the first Alt+Tab would otherwise pay for.

        Resizing an RGBA icon costs a premultiply round-trip inside Pillow, and
        that is most of a cold open. Doing it at startup — on the UI thread, so
        nothing can race a live frame — makes the first wheel as fast as the
        hundredth.
        """
        if self._visible or not apps:
            return
        self._update_monitor()
        saved_apps, saved_selected = self._apps, self._selected
        self._apps = list(apps)
        self._selected = 0
        try:
            self._ring_plate(len(apps))
            idle_px = self._icon_size(1)
            active_px = self._highlight_icon_px(1)
            for app in self._apps:
                self._icon_variant(app, idle_px, idle=True)
                self._icon_variant(app, active_px, idle=False)
            for index in range(len(self._apps)):
                self._wedge(index)
                self._build_hub(index)
                self._build_label(index)
        except Exception:
            pass
        finally:
            self._apps, self._selected = saved_apps, saved_selected

    def _register(self, hinstance, class_name: str, wndproc) -> None:
        wc = w.WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(w.WNDCLASSEXW)
        wc.lpfnWndProc = wndproc
        wc.hInstance = hinstance
        wc.lpszClassName = class_name
        wc.hCursor = w.user32.LoadCursorW(None, 32512)
        w.user32.RegisterClassExW(ctypes.byref(wc))
        self._wndprocs.append(wndproc)

    # -- window procedures -------------------------------------------------

    _MOUSE_MSGS = (
        w.WM_LBUTTONDOWN,
        w.WM_LBUTTONUP,
        w.WM_RBUTTONDOWN,
        w.WM_RBUTTONUP,
        w.WM_MBUTTONDOWN,
        w.WM_MBUTTONUP,
        w.WM_XBUTTONDOWN,
        w.WM_XBUTTONUP,
        w.WM_MOUSEWHEEL,
        w.WM_MOUSEMOVE,
    )

    def _make_passive_proc(self):
        @w.WNDPROC
        def wndproc(hwnd, msg, wparam, lparam):
            if msg == w.WM_DESTROY:
                return 0
            if msg == w.WM_PAINT and hwnd == self._dim_hwnd:
                self._paint_backdrop(hwnd)
                return 0
            if msg == w.WM_ERASEBKGND:
                return 1
            if msg == w.WM_NCHITTEST:
                return w.HTCLIENT
            if msg == w.WM_RBUTTONUP and self._visible and self._on_cancel:
                self._on_cancel()
                return 0
            if msg == w.WM_LBUTTONUP and self._visible and self._on_commit:
                # The cursor is usually nowhere near the ring it is aiming at,
                # so a click anywhere has to mean "take this one".
                self._on_commit()
                return 0
            if msg == w.WM_MOUSEWHEEL and self._visible:
                delta = ctypes.c_short((wparam >> 16) & 0xFFFF).value
                self.cycle(-1 if delta > 0 else 1)
                return 0
            if msg in self._MOUSE_MSGS:
                return 0  # absorb: clicks must never reach the apps underneath
            return w.user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        return wndproc

    def _make_wheel_proc(self):
        @w.WNDPROC
        def wndproc(hwnd, msg, wparam, lparam):
            if msg == w.WM_DESTROY:
                w.user32.PostQuitMessage(0)
                return 0
            if msg == w.WM_ERASEBKGND:
                return 1
            if msg == w.WM_LBUTTONUP:
                x, y = _lparam_point(lparam)
                if self._point_on_wheel(x, y) and self._on_commit:
                    self._on_commit()
                return 0
            if msg == w.WM_RBUTTONUP:
                if self._on_cancel:
                    self._on_cancel()
                return 0
            if msg == w.WM_MBUTTONUP:
                self.request_close_selected()
                return 0
            if msg == w.WM_MOUSEWHEEL:
                delta = ctypes.c_short((wparam >> 16) & 0xFFFF).value
                self.cycle(-1 if delta > 0 else 1)
                return 0
            if msg in self._MOUSE_MSGS:
                # Movement is read by the frame loop instead: only the canvas
                # window gets WM_MOUSEMOVE, and aiming has to work off it.
                return 0
            if msg == w.WM_NCHITTEST:
                point = w.POINT(*_lparam_point(lparam))
                w.user32.ScreenToClient(hwnd, ctypes.byref(point))
                if self._point_on_wheel(point.x, point.y):
                    return w.HTCLIENT
                return w.HTTRANSPARENT  # empty canvas: let the backdrop take it
            return w.user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        return wndproc

    def _point_on_wheel(self, x: float, y: float) -> bool:
        if not self._layers:
            return False
        layout = self._layers.layout
        return math.hypot(x - layout.cx, y - layout.cy) <= layout.outer_r * 1.06

    # -- metrics -----------------------------------------------------------

    def _apply_metrics(self, dpi: int) -> None:
        cfg = self.cfg
        scale = (dpi / 96.0) * cfg.scale
        self._dpi = dpi
        self.s = scale

        self.outer_r = max(60, int(round(cfg.outer_radius * scale)))
        self.inner_r = max(20, int(round(min(cfg.inner_radius, cfg.outer_radius - 24) * scale)))
        self.margin = int(round(30 * scale))
        self.label_h = int(round((96 if cfg.show_hints else 92) * scale))
        self._mouse_slop = max(3, int(round(7 * scale)))
        self._origin_size = max(12, int(round(26 * scale)) // 2 * 2)  # even, so it centres

        self.canvas_w = self.outer_r * 2 + self.margin * 2
        self.canvas_h = self.margin + self.outer_r * 2 + self.label_h
        self.wheel_cx = self.canvas_w // 2
        self.wheel_cy = self.margin + self.outer_r
        self.label_top = self.wheel_cy + self.outer_r + int(round(16 * scale))

        self.f_title = _font(TITLE_FONTS, 21 * scale)
        self.f_sub = _font(BODY_FONTS, 12.5 * scale)
        self.f_hub = _font(TITLE_FONTS, 17 * scale)
        self.f_hint = _font(BODY_FONTS, 11.5 * scale)
        self.f_badge = _font(BODY_FONTS, 11 * scale)

        self.preview_w = int(round(cfg.preview_width * scale))
        self.preview_h = int(round(cfg.preview_height * scale))
        self.preview_pad = int(round(10 * scale))
        self.preview_margin = int(round(cfg.preview_margin * scale))
        self._worker.size = (self.preview_w, self.preview_h)

        self._min_dt = 1.0 / max(30, cfg.max_fps)

    def _metrics_signature(self) -> tuple:
        return (
            self.canvas_w,
            self.canvas_h,
            self.outer_r,
            self.inner_r,
            self._theme_rev,
            self.cfg.show_hints,
            self.cfg.show_subtitle,
            self.cfg.show_counter,
        )

    def _update_monitor(self) -> None:
        point = w.POINT()
        w.user32.GetCursorPos(ctypes.byref(point))
        monitor = w.user32.MonitorFromPoint(point, w.MONITOR_DEFAULTTONEAREST)
        info = w.MONITORINFO()
        info.cbSize = ctypes.sizeof(w.MONITORINFO)
        if monitor and w.user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
            rect = info.rcMonitor
            self._monitor = (rect.left, rect.top, rect.right, rect.bottom)
            dpi = w.monitor_dpi(int(monitor))
        else:
            self._monitor = (
                0,
                0,
                w.user32.GetSystemMetrics(0),
                w.user32.GetSystemMetrics(1),
            )
            dpi = 96
        if dpi != self._dpi:
            self._apply_metrics(dpi)

    # -- show / hide -------------------------------------------------------

    def show(self, apps: list[AppWindow], selected: int = 0, sticky: bool = False) -> None:
        if not apps:
            return
        self._update_monitor()

        self._all_apps = list(apps)
        self._apps = list(apps)
        self._selected = max(0, min(selected, len(apps) - 1))
        self._sticky = sticky
        self._query = ""
        self._query_matched = True
        self._sel_from = -1
        self._shown_at = time.perf_counter()
        self._sel_t0 = self._shown_at
        self._dirty = True
        self._written_index = -2
        self._written_aim = None
        self._aim_angle = None

        left, top, right, bottom = self._monitor
        width, height = right - left, bottom - top
        self._wheel_origin = (
            left + (width - self.canvas_w) // 2,
            top + (height - self.canvas_h) // 2,
        )
        self._preview_origin = self._preview_position(left, top, width, height)

        # Decided before anything of ours is on screen, so a capture that has
        # to happen here cannot catch the overlay in it.
        show_backdrop = self._present_backdrop(left, top, width, height)

        self._rebuild_layers()
        self._render(self._shown_at, force=True)

        self._visible = True
        # Wherever the cursor happens to be is the origin to aim from, so the
        # wheel is equally usable with the pointer parked in a corner.
        point = w.POINT()
        w.user32.GetCursorPos(ctypes.byref(point))
        self._mouse_anchor = (int(point.x), int(point.y))
        self._last_cursor = self._mouse_anchor
        self._origin_at = self._mouse_anchor
        self._origin_live = True
        self._origin_shown = ()  # the window was hidden, so re-present regardless

        # The backdrop is sized explicitly because blur mode has no
        # UpdateLayeredWindow call to place it; the wheel and card were already
        # positioned by their own presents.
        if show_backdrop:
            w.user32.SetWindowPos(
                self._dim_hwnd,
                w.HWND_TOPMOST,
                left,
                top,
                width,
                height,
                w.SWP_SHOWWINDOW | w.SWP_NOACTIVATE,
            )
            if self._backdrop_plate is not None:
                # Paint it in this frame rather than whenever the loop next
                # idles, so the wheel never appears over a bare desktop.
                w.user32.UpdateWindow(self._dim_hwnd)

        # Ordered deliberately: each SetWindowPos raises that window to the top
        # of the topmost band, so the marker ends up over the backdrop but
        # under the wheel, where it cannot cover a slice.
        self._present_origin()

        flags = w.SWP_SHOWWINDOW | w.SWP_NOACTIVATE | w.SWP_NOMOVE | w.SWP_NOSIZE
        if self.cfg.preview_enabled:
            w.user32.SetWindowPos(self._preview_hwnd, w.HWND_TOPMOST, 0, 0, 0, 0, flags)
        w.user32.SetWindowPos(self.hwnd, w.HWND_TOPMOST, 0, 0, 0, 0, flags)

        self._start_previews()

    def hide(self) -> None:
        if not self._visible:
            return
        self._visible = False
        self._sticky = False
        self._worker.new_generation()
        flags = w.SWP_HIDEWINDOW | w.SWP_NOMOVE | w.SWP_NOSIZE | w.SWP_NOACTIVATE
        for hwnd in (self.hwnd, self._preview_hwnd, self._origin_hwnd, self._dim_hwnd):
            w.user32.SetWindowPos(hwnd, w.HWND_TOPMOST, 0, 0, 0, 0, flags)
        self._apps = []
        self._all_apps = []
        self._layers = None
        self._mouse_anchor = None
        self._last_cursor = None
        self._aim_angle = None
        self._origin_at = None
        self._origin_live = False
        self._origin_shown = ()
        self._card_key = ()
        self._query = ""
        self._query_matched = True
        self._thumbs.clear()
        self._thumb_at.clear()
        self._thumb_stamp.clear()
        with self._pending_lock:
            self._pending.clear()
        with self._plate_lock:
            self._plate = None
            self._plate_rect = None
            self._plate_at = 0.0
        self._backdrop_plate = None

    def _preview_position(
        self, left: int, top: int, width: int, height: int
    ) -> tuple[int, int]:
        card_w = self.preview_w + self.preview_pad * 2
        card_h = self.preview_h + self.preview_pad * 2
        gap = self.preview_margin
        position = self.cfg.preview_position
        x = left + gap
        y = top + gap
        if "right" in position:
            x = left + width - card_w - gap
        if "center" in position:
            x = left + (width - card_w) // 2
        if "bottom" in position:
            y = top + height - card_h - gap
        return (x, y)

    # -- navigation --------------------------------------------------------

    def select(self, index: int, *, animate: bool = True, from_mouse: bool = False) -> None:
        if not self._apps:
            return
        index = max(0, min(index, len(self._apps) - 1))
        if index == self._selected:
            return
        if not from_mouse:
            # Every discrete pick — Tab, digits, search, the scroll wheel —
            # takes the wheel back off the pointer.
            self._release_mouse()
        self._sel_from = self._selected if animate else -1
        self._sel_t0 = time.perf_counter()
        self._selected = index
        self._dirty = True
        self._request_preview(self._apps[index].hwnd, urgent=True)

    def cycle(self, delta: int) -> None:
        if not self._apps:
            return
        count = len(self._apps)
        if self.cfg.wrap_navigation:
            index = (self._selected + delta) % count
        else:
            index = max(0, min(self._selected + delta, count - 1))
        self.select(index)

    def jump(self, number: int) -> bool:
        """1-9 pick a slice directly; 0 means the tenth."""
        index = (number - 1) if number > 0 else 9
        if 0 <= index < len(self._apps):
            self.select(index)
            return True
        return False

    def first(self) -> None:
        self.select(0)

    def last(self) -> None:
        self.select(len(self._apps) - 1)

    def cycle_same_app(self, delta: int) -> None:
        """Move to the next window belonging to the same application."""
        current = self.selected_app()
        if current is None:
            return
        key = current.exe_path or current.class_name
        peers = [
            i
            for i, app in enumerate(self._apps)
            if (app.exe_path or app.class_name) == key
        ]
        if len(peers) < 2:
            return
        position = peers.index(self._selected) if self._selected in peers else 0
        self.select(peers[(position + delta) % len(peers)])

    # -- search ------------------------------------------------------------

    def type_query(self, char: str) -> None:
        self._set_query(self._query + char)

    def backspace_query(self) -> None:
        if self._query:
            self._set_query(self._query[:-1])

    def clear_query(self) -> None:
        if self._query:
            self._set_query("")

    def _set_query(self, text: str) -> None:
        self._query = text
        keep_hwnd = self.selected_app().hwnd if self.selected_app() else 0

        if text:
            needle = text.lower()
            matches = [
                a
                for a in self._all_apps
                if a.matches_query(needle, title_privacy=self.cfg.title_privacy)
            ]
        else:
            matches = list(self._all_apps)

        self._query_matched = bool(matches) or not text
        self._release_mouse()
        if matches:
            self._apps = matches
            index = next(
                (i for i, a in enumerate(self._apps) if a.hwnd == keep_hwnd), 0
            )
            self._selected = index
        # No match: keep showing the previous set and flag the query instead of
        # blanking the wheel under the user's fingers.

        self._sel_from = -1
        self._dirty = True
        self._rebuild_layers()
        if self.selected_app():
            self._request_preview(self.selected_app().hwnd, urgent=True)

    # -- window actions ----------------------------------------------------

    def request_close_selected(self) -> int:
        """Drop the selected window from the wheel and return its hwnd."""
        app = self.selected_app()
        if app is None:
            return 0
        hwnd = app.hwnd
        self._all_apps = [a for a in self._all_apps if a.hwnd != hwnd]
        remaining = [a for a in self._apps if a.hwnd != hwnd]
        if not remaining:
            if self._on_cancel:
                self._on_cancel()
            return hwnd
        self._apps = remaining
        self._selected = min(self._selected, len(remaining) - 1)
        self._release_mouse()
        self._sel_from = -1
        self._drop_thumb(hwnd)
        self._dirty = True
        self._rebuild_layers()
        return hwnd

    # -- previews ----------------------------------------------------------

    def _start_previews(self) -> None:
        if not self.cfg.preview_enabled:
            return
        self._prune_thumbs()
        self._worker.new_generation()
        app = self.selected_app()
        if app is not None:
            self._request_preview(app.hwnd, urgent=True)
        if not self.cfg.prefetch_previews:
            return
        for other in self._apps:
            if app is not None and other.hwnd == app.hwnd:
                continue
            if other.minimized or self._thumb_fresh(other.hwnd):
                continue  # restoring a minimised window off-screen is too invasive
            self._worker.request(other.hwnd, urgent=False, allow_minimized=False)

    def _thumb_fresh(self, hwnd: int) -> bool:
        seen = self._thumb_at.get(hwnd)
        return seen is not None and (time.perf_counter() - seen) < self.cfg.thumb_ttl

    def _request_preview(self, hwnd: int, *, urgent: bool) -> None:
        """Ask for a capture; a stale thumb still shows instantly meanwhile."""
        if not self.cfg.preview_enabled or not hwnd:
            return
        if self._thumb_fresh(hwnd):
            return
        self._worker.request(hwnd, urgent=urgent, allow_minimized=self.cfg.capture_minimized)

    def _prune_thumbs(self) -> None:
        live = {app.hwnd for app in self._all_apps}
        for hwnd in [h for h in self._thumbs if h not in live]:
            self._drop_thumb(hwnd)

    def _drop_thumb(self, hwnd: int) -> None:
        self._thumbs.pop(hwnd, None)
        self._thumb_at.pop(hwnd, None)
        self._thumb_stamp.pop(hwnd, None)

    def _on_capture(self, gen: int, hwnd: int, img: Image.Image) -> None:
        with self._pending_lock:
            self._pending.append((gen, hwnd, img))

    def _drain_captures(self) -> None:
        with self._pending_lock:
            if not self._pending:
                return
            items = self._pending
            self._pending = []
        current = self._worker.generation
        now = time.perf_counter()
        app = self.selected_app()
        for gen, hwnd, img in items:
            if gen != current:
                continue
            self._thumb_seq += 1
            self._thumbs[hwnd] = img
            self._thumb_stamp[hwnd] = self._thumb_seq
            self._thumb_at[hwnd] = now
            if app is not None and app.hwnd == hwnd:
                self._dirty = True

    # -- mouse -------------------------------------------------------------
    #
    # Aiming is relative to where the cursor was when it last became the
    # authority, not to the middle of the screen. The wheel is a direction
    # picker, so what matters is which way you flicked — and measuring from
    # the cursor's own position means a flick works identically with the
    # pointer parked in a corner, where measuring from the wheel's centre
    # would have pre-selected whichever slice the corner happened to lie in.

    def _release_mouse(self) -> None:
        """Hand control back to the keyboard until the cursor moves again.

        Re-anchoring on the cursor is what stops the two fighting: a discrete
        pick stands until a deliberate flick, rather than being undone by the
        next frame re-reading a pointer that never moved.
        """
        self._mouse_anchor = None
        self._last_cursor = None
        self._set_aim_angle(None)
        self._set_origin(self._origin_at, live=False)

    def _poll_cursor(self) -> None:
        """Read the cursor once a frame.

        Polling rather than WM_MOUSEMOVE because the canvas window is only a
        few hundred pixels wide and aiming has to work off it.
        """
        point = w.POINT()
        w.user32.GetCursorPos(ctypes.byref(point))
        self._cursor_at((int(point.x), int(point.y)))

    def _cursor_at(self, pos: tuple[int, int]) -> None:
        if pos == self._last_cursor:
            return  # standing still is not an input
        self._last_cursor = pos
        self._aim_from(pos)

    def _aim_from(self, pos: tuple[int, int]) -> None:
        if not self._visible or not self._layers:
            return
        if self._mouse_anchor is None:
            self._mouse_anchor = pos  # first sighting since the keyboard spoke
            self._set_origin(pos, live=True)
            return

        dx = pos[0] - self._mouse_anchor[0]
        dy = pos[1] - self._mouse_anchor[1]
        if abs(dx) < self._mouse_slop and abs(dy) < self._mouse_slop:
            return  # hand tremor, or a desk knock

        layout = self._layers.layout
        ox, oy = self._wheel_origin
        canvas = (pos[0] - ox, pos[1] - oy)
        if self._point_on_wheel(*canvas):
            # Actually on the ring: point at a slice and get that slice.
            vx, vy = canvas[0] - layout.cx, canvas[1] - layout.cy
            hit = layout.aim(*canvas)
        else:
            # Anywhere else, only the direction of the flick matters.
            vx, vy = dx, dy
            hit = layout.aim(layout.cx + dx, layout.cy + dy)

        # `aim` returns None inside the hub dead zone, which is exactly when
        # there is no direction worth drawing.
        self._set_aim_angle(None if hit is None else math.atan2(-vy, vx))

        if hit is not None and hit != self._selected:
            self.select(hit, from_mouse=True)

    # -- origin marker -----------------------------------------------------

    def _origin_plate(self, live: bool) -> Image.Image:
        """A ring marking the pivot, hollow so it never hides what's under it."""
        key = (self._metrics_signature(), self._theme_rev, live)
        cached = self._origin_cache.get(key)
        if cached is not None:
            return cached

        ss = RENDER_SCALE
        size = self._origin_size
        img = Image.new("RGBA", (size * ss, size * ss), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        mid = size * ss / 2
        radius = mid - 3 * self.s * ss
        ring = max(1.5, 2.0 * self.s) * ss
        theme = self.theme
        strength = 1.0 if live else 0.45

        # Dark ring just outside the bright one: the marker lands on unknown
        # wallpaper, and one colour alone disappears against half of it.
        draw.ellipse(
            (mid - radius, mid - radius, mid + radius, mid + radius),
            outline=alpha(theme.shadow, int(150 * strength)),
            width=int(ring + 2 * self.s * ss),
        )
        draw.ellipse(
            (mid - radius, mid - radius, mid + radius, mid + radius),
            outline=alpha(theme.accent, int(240 * strength)),
            width=int(ring),
        )
        dot = max(1.0, 1.6 * self.s) * ss
        draw.ellipse(
            (mid - dot, mid - dot, mid + dot, mid + dot),
            fill=alpha(theme.accent, int(230 * strength)),
        )

        plate = img.resize((size, size), Image.Resampling.LANCZOS)
        _trim(self._origin_cache, 8)
        self._origin_cache[key] = plate
        return plate

    def _set_origin(self, at: Optional[tuple[int, int]], live: bool) -> None:
        self._origin_at = at
        self._origin_live = live
        if self._visible:
            self._present_origin()

    def _present_origin(self) -> None:
        """Move the marker window to the pivot, or hide it if there is nothing to mark."""
        if not self._origin_hwnd:
            return
        at = self._origin_at if self.cfg.aim_origin else None
        wanted = (at, self._origin_live, self._metrics_signature(), self._theme_rev)
        if wanted == self._origin_shown:
            return
        self._origin_shown = wanted

        if at is None:
            w.user32.SetWindowPos(
                self._origin_hwnd,
                w.HWND_TOPMOST,
                0,
                0,
                0,
                0,
                w.SWP_HIDEWINDOW | w.SWP_NOMOVE | w.SWP_NOSIZE | w.SWP_NOACTIVATE,
            )
            return

        size = self._origin_size
        if not self._origin.ensure(size, size):
            return
        self._origin.paint(
            self._origin_plate(self._origin_live), at[0] - size // 2, at[1] - size // 2
        )
        w.user32.SetWindowPos(
            self._origin_hwnd,
            w.HWND_TOPMOST,
            0,
            0,
            0,
            0,
            w.SWP_SHOWWINDOW | w.SWP_NOMOVE | w.SWP_NOSIZE | w.SWP_NOACTIVATE,
        )

    def _set_aim_angle(self, angle: Optional[float]) -> None:
        """Quantised so a pixel of jitter does not repaint the wheel."""
        if angle is not None and not self.cfg.aim_needle:
            angle = None  # switched off: aiming still works, it just isn't drawn
        if angle is not None:
            angle = round(angle / AIM_STEP) * AIM_STEP
        if angle != self._aim_angle:
            self._aim_angle = angle
            self._dirty = True

    # -- frame loop --------------------------------------------------------

    def pump_idle(self) -> None:
        if not self._visible:
            return
        self._drain_captures()
        now = time.perf_counter()
        self._poll_cursor()
        if now - self._last_frame < self._min_dt:
            return
        if self._dirty or self._animating(now):
            self._render(now)

    def alt_released_while_open(self) -> bool:
        """Deprecated safety valve — the app now uses hook.hold_released()."""
        if not self._visible or self._sticky:
            return False
        if time.perf_counter() - self._shown_at < 0.25:
            return False
        return False

    def _animating(self, now: float) -> bool:
        if now - self._shown_at < self.cfg.appear_duration:
            return True
        if self._sel_from >= 0 and now - self._sel_t0 < self.cfg.transition_duration:
            return True
        if now - self._preview_t0 < PREVIEW_FADE:
            return True
        return False

    def _render(self, now: float, force: bool = False) -> None:
        if not self._layers:
            return
        self._last_frame = now
        self._dirty = False

        # The open animation is a rise plus a fade, and both are arguments to
        # UpdateLayeredWindow — no pixels are touched, so it stays smooth even
        # while the preview thread is busy.
        opacity = 1.0
        rise = 0
        appear = self.cfg.appear_duration
        if appear > 0:
            t = (now - self._shown_at) / appear
            if t < 1.0:
                eased = _ease_out_cubic(max(0.0, t))
                opacity = 0.15 + 0.85 * eased
                rise = int(round((1.0 - eased) * 14 * self.s))

        frame = self._compose(now)
        aim = self._aim_angle
        ox, oy = self._wheel_origin
        settled = not force and self._sel_from < 0 and self._written_index == self._selected

        if settled and self._written_aim == aim:
            # Nothing in the bitmap changed, so the DIB already holds this frame.
            self._wheel.present(ox, oy + rise, opacity)
        elif settled and self._patch_needle(frame, aim):
            # Only the needle moved: re-pack the band it lives in rather than
            # the whole canvas, which is most of the cost of a frame.
            self._wheel.present(ox, oy + rise, opacity)
            self._written_aim = aim
        else:
            if aim is not None:
                frame = frame.copy()
                self._draw_needle(frame, aim)
            self._wheel.paint(frame, ox, oy + rise, opacity=opacity)
            self._written_index = self._selected if self._sel_from < 0 else -1
            self._written_aim = aim

        if self.cfg.preview_enabled:
            self._render_preview(now, force=force)

    def _needle_span(self) -> tuple[float, float]:
        """Radii the aim needle occupies: hub edge out to just short of the icons."""
        icon_r = (self.inner_r + self.outer_r) * 0.52
        start = self.inner_r + 2 * self.s
        end = icon_r - self._icon_size(1) / 2 - 4 * self.s
        return start, max(start + 8 * self.s, end)

    def _needle_box(self) -> tuple[int, int, int, int]:
        """The square every needle position fits inside, whatever the angle.

        Angle-independent on purpose: writing the same box each time erases the
        old needle along with drawing the new one, so no history is needed.
        """
        reach = self._needle_span()[1] + 8 * self.s
        return (
            max(0, int(self.wheel_cx - reach)),
            max(0, int(self.wheel_cy - reach)),
            min(self.canvas_w, int(math.ceil(self.wheel_cx + reach))),
            min(self.canvas_h, int(math.ceil(self.wheel_cy + reach))),
        )

    def _patch_needle(self, frame: Image.Image, angle: Optional[float]) -> bool:
        """Update just the needle's band in the DIB, from a needle-free frame.

        Only valid when the DIB already holds this exact frame, which is what
        the caller checks: the patch is cut fresh from ``frame``, so whatever
        needle was there before is overwritten rather than blended with.

        False if the band would not fit, so the caller can fall back to a whole
        frame instead of leaving a stale needle behind.
        """
        box = self._needle_box()
        patch = frame.crop(box)
        if angle is not None:
            self._draw_needle(patch, angle, origin=(box[0], box[1]))
        return self._wheel.write_box(patch, box[0], box[1])

    def _draw_needle(
        self,
        img: Image.Image,
        angle: float,
        origin: tuple[int, int] = (0, 0),
    ) -> None:
        """Draw a needle from the hub showing where the cursor is aiming.

        Sits in the empty band between the hub and the icons, which is always
        there: icon size is capped by that same radial band, so it never
        collapses however many windows are listed.
        """
        draw = ImageDraw.Draw(img)
        cx, cy = self.wheel_cx - origin[0], self.wheel_cy - origin[1]
        # Screen coordinates, so the y component of the direction is negated.
        dx, dy = math.cos(angle), -math.sin(angle)
        px, py = -dy, dx  # perpendicular, for the taper

        start, end = self._needle_span()
        thin, wide = 1.7 * self.s, 4.6 * self.s

        def wedge(grow: float) -> list[tuple[float, float]]:
            near, far = thin + grow, wide + grow
            ax, ay = cx + dx * (start - grow), cy + dy * (start - grow)
            bx, by = cx + dx * (end + grow), cy + dy * (end + grow)
            return [
                (ax + px * near, ay + py * near),
                (bx + px * far, by + py * far),
                (bx - px * far, by - py * far),
                (ax - px * near, ay - py * near),
            ]

        theme = self.theme
        # A wider dark pass underneath keeps it legible whether it is over the
        # ring fill or the highlighted slice. Light themes need far less of it,
        # and too much just muddies the accent into a grey smear.
        draw.polygon(wedge(1.3 * self.s), fill=alpha(theme.shadow, 130 if theme.is_dark else 55))
        draw.polygon(wedge(0.0), fill=alpha(theme.accent, 242))

    def _compose(self, now: float) -> Image.Image:
        """The finished 1x wheel bitmap for this instant.

        Settled frames are cached whole, so holding a selection costs nothing
        at all: the same bitmap is handed back and never even re-uploaded.
        """
        duration = self.cfg.transition_duration
        if 0 <= self._sel_from < len(self._apps) and duration > 0:
            t = (now - self._sel_t0) / duration
            if t < 1.0:
                # A single pass over two finished frames. Compositing just the
                # layers that moved sounds cheaper, but it needs a copy of the
                # base plus five composites, which measures slightly slower
                # than letting Pillow blend the whole canvas in C.
                return Image.blend(
                    self._frame_for(self._sel_from),
                    self._frame_for(self._selected),
                    _ease_out_cubic(max(0.0, t)),
                )

        self._sel_from = -1
        return self._frame_for(self._selected)

    def _frame_for(self, index: int) -> Image.Image:
        layers = self._layers
        assert layers is not None
        cached = layers.frames.get(index)
        if cached is not None:
            return cached
        frame = layers.base.copy()
        for cache, build in (
            (layers.hl, self._build_highlight),
            (layers.hub, self._build_hub),
            (layers.label, self._build_label),
        ):
            layer = cache.get(index)
            if layer is None:
                layer = build(index)
                if layer is None:
                    continue
                cache[index] = layer
            frame.alpha_composite(layer.img, layer.pos)
        layers.frames[index] = frame
        _trim(layers.frames, 16)
        return frame

    # -- layer construction ------------------------------------------------

    def _rebuild_layers(self) -> None:
        if not self._apps:
            self._layers = None
            return
        signature = (
            self._metrics_signature(),
            tuple(a.hwnd for a in self._apps),
            tuple(a.title for a in self._apps),
            bool(self._query),
            self._query,
        )
        cached = self._layer_cache.get(signature)
        if cached is not None:
            self._layer_cache.move_to_end(signature)
            self._layers = cached
            return

        layers = WheelLayers(
            base=self._build_base(),
            layout=build_layout(
                len(self._apps),
                float(self.wheel_cx),
                float(self.wheel_cy),
                float(self.outer_r),
                float(self.inner_r),
            ),
        )
        self._layers = layers
        self._layer_cache[signature] = layers
        while len(self._layer_cache) > 4:
            self._layer_cache.popitem(last=False)

    def _hi_layout(self) -> WheelLayout:
        return self._hi_layout_for(len(self._apps))

    def _hi_layout_for(self, count: int) -> WheelLayout:
        ss = RENDER_SCALE
        return build_layout(
            count,
            float(self.wheel_cx * ss),
            float(self.wheel_cy * ss),
            float(self.outer_r * ss),
            float(self.inner_r * ss),
        )

    def _icon_at(self, app: AppWindow, size: int) -> Optional[Image.Image]:
        if app.icon is None or size < 4:
            return None
        key = (id(app.icon), size)
        cached = self._icon_cache.get(key)
        if cached is None:
            cached = app.icon.resize((size, size), Image.Resampling.LANCZOS)
            self._icon_cache[key] = cached
        return cached

    def _icon_size(self, ss: int) -> int:
        """The largest icon that still leaves margin inside its slice.

        Two things bound it: the radial band between the hub and the rim, and
        the arc each slice gets at the icon ring. Deriving it beats the hand
        tuned table this replaced, which was fitted for crowded wheels and so
        left the common three-to-eight window case with needlessly tiny icons.
        Both radii are already DPI-scaled, so this is too.
        """
        count = max(1, len(self._apps))
        band = self.outer_r - self.inner_r
        arc = (2 * math.pi * (self.inner_r + self.outer_r) * 0.52) / count
        size = int(round(min(band, arc) * 0.55 * self.cfg.icon_scale)) // 2 * 2
        return max(8, size) * ss

    def _build_base(self) -> Image.Image:
        """Cached ring plate plus this window set's icons, all at 1x.

        Only the icons depend on *which* windows are listed, so the expensive
        supersampled geometry is rendered once per (metrics, count) and simply
        copied afterwards. Re-ordering the wheel then costs a memcpy.
        """
        base = self._ring_plate(len(self._apps)).copy()
        layout = build_layout(
            len(self._apps),
            float(self.wheel_cx),
            float(self.wheel_cy),
            float(self.outer_r),
            float(self.inner_r),
        )
        icon_px = self._icon_size(1)
        for slice_geom in layout.slices:
            app = self._apps[slice_geom.index]
            icon = self._icon_variant(app, icon_px, idle=True)
            if icon is None:
                continue
            base.alpha_composite(
                icon,
                (
                    int(slice_geom.icon_x - icon_px / 2),
                    int(slice_geom.icon_y - icon_px / 2),
                ),
            )
        return base

    def _ring_plate(self, count: int) -> Image.Image:
        """Shadow + ring + dividers: everything that only depends on the count."""
        key = (self._metrics_signature(), count)
        cached = self._plate_cache.get(key)
        if cached is not None:
            return cached

        ss = RENDER_SCALE
        theme = self.theme
        width, height = self.canvas_w * ss, self.canvas_h * ss
        cx, cy = self.wheel_cx * ss, self.wheel_cy * ss
        outer, inner = self.outer_r * ss, self.inner_r * ss

        img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img, "RGBA")

        shade = theme.shadow
        steps = (26, 38, 54) if theme.is_dark else (12, 18, 26)
        for index, opacity in enumerate(steps):
            pad = (20 - index * 6) * ss
            draw.ellipse(
                (cx - outer - pad, cy - outer - pad, cx + outer + pad, cy + outer + pad),
                fill=(shade[0], shade[1], shade[2], opacity),
            )

        draw.ellipse(
            (cx - outer, cy - outer, cx + outer, cy + outer),
            fill=theme.ring_fill,
            outline=theme.ring_edge,
            width=max(2, 2 * ss),
        )

        if count > 1:
            for slice_geom in self._hi_layout_for(count).slices:
                angle = slice_geom.start_cw
                draw.line(
                    (
                        cx + math.cos(angle) * inner,
                        cy - math.sin(angle) * inner,
                        cx + math.cos(angle) * outer,
                        cy - math.sin(angle) * outer,
                    ),
                    fill=theme.divider,
                    width=max(2, 2 * ss),
                )

        # An exact 2x box reduce is the correct resolve for a supersampled
        # render, and skips the premultiply round-trip resize() does on RGBA.
        plate = img.reduce(ss)
        self._plate_cache[key] = plate
        while len(self._plate_cache) > 6:
            self._plate_cache.pop(next(iter(self._plate_cache)))
        return plate

    def _icon_variant(
        self, app: AppWindow, size: int, *, idle: bool
    ) -> Optional[Image.Image]:
        """Icon as drawn on the wheel: unselected icons sit back a little."""
        if app.icon is None or size < 4:
            return None
        key = (id(app.icon), size, idle, app.minimized)
        cached = self._icon_variants.get(key)
        if cached is not None:
            return cached
        icon = self._icon_at(app, size)
        if icon is None:
            return None
        if app.minimized:
            icon = _dim_image(icon, 0.55 if idle else 0.72)
        if idle:
            icon = _scale_alpha(icon, 0.82)
        self._icon_variants[key] = icon
        return icon

    def _layout_1x(self) -> WheelLayout:
        return build_layout(
            len(self._apps),
            float(self.wheel_cx),
            float(self.wheel_cy),
            float(self.outer_r),
            float(self.inner_r),
        )

    def _highlight_icon_px(self, ss: int) -> int:
        return max(8, int(self._icon_size(ss) * 1.12) // 2 * 2)

    def _build_highlight(self, index: int) -> Optional[Layer]:
        """The cached wedge for this position, with this window's icon on top."""
        if not (0 <= index < len(self._apps)):
            return None
        wedge = self._wedge(index)
        if wedge is None:
            return None

        icon_px = self._highlight_icon_px(1)
        icon = self._icon_variant(self._apps[index], icon_px, idle=False)
        if icon is None:
            return wedge

        slice_geom = self._layout_1x().slices[index]
        img = wedge.img.copy()
        img.alpha_composite(
            icon,
            (
                int(slice_geom.icon_x - wedge.pos[0] - icon_px / 2),
                int(slice_geom.icon_y - wedge.pos[1] - icon_px / 2),
            ),
        )
        return Layer(img, wedge.pos)

    def _wedge(self, index: int) -> Optional[Layer]:
        """Highlight geometry only — depends on the slice count, not the apps."""
        count = len(self._apps)
        key = (self._metrics_signature(), count, index)
        cached = self._wedge_cache.get(key)
        if cached is not None:
            return cached

        ss = RENDER_SCALE
        theme = self.theme
        layout = self._hi_layout_for(count)
        slice_geom = layout.slices[index]
        outer, inner = self.outer_r * ss, self.inner_r * ss

        points = pie_points(
            layout.cx, layout.cy, inner, outer, slice_geom.start_cw, slice_geom.end_cw, steps=72
        )
        icon_px = self._icon_size(ss)
        icon_px = int(icon_px * 1.12) // 2 * 2

        xs = [p[0] for p in points] + [slice_geom.icon_x - icon_px, slice_geom.icon_x + icon_px]
        ys = [p[1] for p in points] + [slice_geom.icon_y - icon_px, slice_geom.icon_y + icon_px]
        pad = 5 * ss
        x0 = _floor_to(max(0, int(min(xs) - pad)), ss)
        y0 = _floor_to(max(0, int(min(ys) - pad)), ss)
        x1 = _ceil_to(min(self.canvas_w * ss, int(max(xs) + pad) + 1), ss)
        y1 = _ceil_to(min(self.canvas_h * ss, int(max(ys) + pad) + 1), ss)
        if x1 <= x0 or y1 <= y0:
            return None

        sub = Image.new("RGBA", (x1 - x0, y1 - y0), (0, 0, 0, 0))
        draw = ImageDraw.Draw(sub, "RGBA")
        shifted = [(px - x0, py - y0) for px, py in points]
        draw.polygon(shifted, fill=theme.slice_hover)

        edge = pie_points(
            layout.cx - x0,
            layout.cy - y0,
            inner + ss,
            outer - ss,
            slice_geom.start_cw,
            slice_geom.end_cw,
            steps=80,
        )
        # Widest-and-faintest first: three passes fake a glow far more cheaply
        # than blurring the layer would.
        closed = edge + [edge[0]]
        for stroke, opacity in ((7, 50), (5, 105), (3, 255)):
            draw.line(
                closed, fill=alpha(theme.accent, opacity), width=max(2, int(stroke * ss / 2))
            )

        layer = Layer(sub.reduce(ss), (x0 // ss, y0 // ss))
        self._wedge_cache[key] = layer
        _trim(self._wedge_cache, 96)
        return layer

    def _build_hub(self, index: int) -> Optional[Layer]:
        key = (self._metrics_signature(), len(self._apps), index)
        cached = self._hub_cache.get(key)
        if cached is not None:
            return cached

        ss = RENDER_SCALE
        theme = self.theme
        inner = self.inner_r * ss
        pad = 3 * ss
        size = int(inner * 2 + pad * 2)
        size = _ceil_to(size, ss)

        sub = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(sub, "RGBA")
        centre = size / 2
        draw.ellipse(
            (centre - inner, centre - inner, centre + inner, centre + inner),
            fill=theme.hub_fill,
            outline=theme.hub_edge,
            width=max(2, 2 * ss),
        )

        if self.cfg.show_counter and self._apps:
            font = _font(TITLE_FONTS, 17 * self.s * ss)
            small_font = _font(BODY_FONTS, 11 * self.s * ss)
            _centered(
                draw,
                str(index + 1),
                centre,
                centre - 7 * self.s * ss,
                font,
                theme.text,
            )
            _centered(
                draw,
                f"of {len(self._apps)}",
                centre,
                centre + 11 * self.s * ss,
                small_font,
                theme.text_dim,
            )

        x0 = int(self.wheel_cx - size / (2 * ss))
        y0 = int(self.wheel_cy - size / (2 * ss))
        layer = Layer(sub.reduce(ss), (x0, y0))
        self._hub_cache[key] = layer
        _trim(self._hub_cache, 96)
        return layer

    def _build_label(self, index: int) -> Optional[Layer]:
        if not (0 <= index < len(self._apps)):
            return None
        app = self._apps[index]
        # Deliberately not keyed by position: the label says nothing about where
        # the slice sits, so re-ordering the wheel reuses every label.
        title = app.label(title_privacy=self.cfg.title_privacy)
        key = (
            self._metrics_signature(),
            title,
            app.subtitle if self.cfg.title_privacy == "full" else "",
            app.minimized,
            app.maximized,
            self._query,
            self._query_matched,
            self.compat_active,
            self.cfg.open_hotkey,
            self.cfg.title_privacy,
        )
        cached = self._label_cache.get(key)
        if cached is not None:
            return cached

        # Text is the one element drawn at 1x: FreeType hinting at the final
        # size beats supersampling it and box-reducing, and costs a quarter.
        ss = 1
        theme = self.theme
        width = self.canvas_w
        top = self.label_top
        height = self.canvas_h - top
        if height <= 0:
            return None

        sub = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(sub, "RGBA")
        centre = width / 2
        max_width = width - 24

        title_font = _font(TITLE_FONTS, 21 * self.s)
        sub_font = _font(BODY_FONTS, 12.5 * self.s)
        hint_font = _font(BODY_FONTS, 11.5 * self.s)

        y = 14 * self.s * ss
        title = _ellipsize(draw, title, title_font, max_width)
        _centered(draw, title, centre + ss, y + ss, title_font, (0, 0, 0, 120))
        _centered(draw, title, centre, y, title_font, theme.text)

        y += 22 * self.s * ss
        if self.cfg.show_subtitle:
            parts = []
            if self.cfg.title_privacy == "full":
                subtitle = app.subtitle
                if subtitle:
                    parts.append(subtitle)
            if app.minimized:
                parts.append("minimised")
            elif app.maximized:
                parts.append("maximised")
            line = _ellipsize(draw, "  ·  ".join(parts), sub_font, max_width)
            if line:
                _centered(draw, line, centre, y, sub_font, theme.text_dim)
            y += 19 * self.s * ss

        if self._query:
            colour = theme.accent if self._query_matched else (226, 106, 106, 255)
            text = f"search: {self._query}" + ("" if self._query_matched else "  (no match)")
            _centered(
                draw, _ellipsize(draw, text, hint_font, max_width), centre, y, hint_font, colour
            )
        elif self.cfg.show_hints:
            from .hotkey import parse_hotkey

            hint = f"{parse_hotkey(self.cfg.open_hotkey).label()} · type to search · Esc cancel"
            _centered(
                draw,
                _ellipsize(draw, hint, hint_font, max_width),
                centre,
                y,
                hint_font,
                alpha(theme.text_dim, 190),
            )

        layer = Layer(sub, (0, self.label_top))
        self._label_cache[key] = layer
        _trim(self._label_cache, 64)
        return layer

    # -- backdrop ----------------------------------------------------------

    def prepare_backdrop(self) -> None:
        """Start blurring the desktop while Alt is held, before Tab arrives.

        Reading the framebuffer back costs ~32ms at 1080p no matter how small
        a destination it is scaled into — it is the screen DC access, not the
        pixel count — which is more than everything else an open does put
        together. Alt always lands before Tab, so the capture runs on a worker
        during that gap and `show` finds a finished plate waiting.

        Only runs while the wheel is down, so the overlay can never end up
        inside its own backdrop.
        """
        if self.cfg.backdrop not in ("blur", "dim") or self._visible:
            return
        self._update_monitor()
        rect = self._monitor
        with self._plate_lock:
            if self._plate_is_fresh(rect):
                return
            running = self._capture_thread
            if running is not None and running.is_alive():
                return
            thread = threading.Thread(
                target=self._capture_plate,
                args=(rect,),
                name="fun-tab-backdrop",
                daemon=True,
            )
            self._capture_thread = thread
        thread.start()

    def _plate_is_fresh(self, rect: tuple[int, int, int, int]) -> bool:
        """Whether the cached plate still describes `rect`. Holds _plate_lock."""
        return (
            self._plate is not None
            and self._plate_rect == rect
            and time.perf_counter() - self._plate_at <= self.cfg.backdrop_ttl
        )

    def _capture_plate(self, rect: tuple[int, int, int, int]) -> Optional[Image.Image]:
        """Grab the desktop, shrink it, blur it, darken it.

        Runs on the capture worker, and on the UI thread only when an open
        beat the worker to it.
        """
        left, top, right, bottom = rect
        width, height = right - left, bottom - top
        cfg = self.cfg
        scale = max(1, cfg.dim_scale)
        small_w = max(8, width // scale)
        small_h = max(8, height // scale)

        with self._capture_lock:
            plate = self._capture_grabber.grab(
                left, top, width, height, small_w, small_h
            )
            if plate is None:
                return None

            plate = treat_plate(plate, cfg)

        with self._plate_lock:
            self._plate = plate
            self._plate_rect = rect
            self._plate_at = time.perf_counter()
        return plate

    def _resolve_plate(self, rect: tuple[int, int, int, int]) -> Optional[Image.Image]:
        """The plate to show now: cached, nearly-finished, or grabbed on the spot."""
        with self._plate_lock:
            if self._plate_is_fresh(rect):
                return self._plate
            running = self._capture_thread

        if running is not None and running.is_alive():
            # A capture from Alt-down is mid-flight; finishing it is cheaper
            # than throwing it away and reading the screen again.
            running.join(0.06)
            with self._plate_lock:
                if self._plate is not None and self._plate_rect == rect:
                    # Past its TTL is still far better than stalling the open.
                    return self._plate
            if running.is_alive():
                return None  # wedged display driver: let the veil handle it

        return self._capture_plate(rect)

    def _present_backdrop(
        self, left: int, top: int, width: int, height: int
    ) -> bool:
        """Set up the sheet behind the wheel; returns whether to show it.

        Nothing is drawn here. The backdrop is an ordinary opaque window, so it
        paints from `_backdrop_plate` on WM_PAINT the moment SetWindowPos
        reveals it; making it layered instead would mean pushing a screen-sized
        bitmap up on every open, which measured slower than the whole wheel.
        """
        mode = self.cfg.backdrop
        if mode == "none":
            return False

        plate = self._resolve_plate((left, top, left + width, top + height))
        if plate is not None:
            self._backdrop_plate = plate
            self._set_layered(self._dim_hwnd, False)
            return True

        # Capture is unavailable on some remote sessions. Fall back to a plain
        # translucent veil, which needs per-pixel alpha and so needs layering.
        self._backdrop_plate = None
        self._set_layered(self._dim_hwnd, True)
        if not self._dim.ensure(width, height):
            return False
        view = self._dim.view
        if view is None:
            return False
        view[...] = 0  # premultiplied black: only the alpha carries the veil
        view[..., 3] = max(24, self.cfg.dim_veil + 60)
        self._dim.present(left, top)
        return True

    def _paint_backdrop(self, hwnd: int) -> None:
        """Blow the small blurred plate up over the whole backdrop window."""
        ps = w.PAINTSTRUCT()
        hdc = w.user32.BeginPaint(hwnd, ctypes.byref(ps))
        try:
            plate = self._backdrop_plate
            if not hdc or plate is None:
                return
            rect = w.RECT()
            if not w.user32.GetClientRect(hwnd, ctypes.byref(rect)):
                return
            self._grabber.stretch_to(hdc, rect.right, rect.bottom, plate)
        finally:
            w.user32.EndPaint(hwnd, ctypes.byref(ps))

    @staticmethod
    def _set_layered(hwnd: int, enabled: bool) -> None:
        """The blurred plate is opaque and blitted; the veil needs per-pixel alpha."""
        gwl_exstyle = -20
        style = int(w.user32.GetWindowLongW(hwnd, gwl_exstyle))
        wanted = (style | w.WS_EX_LAYERED) if enabled else (style & ~w.WS_EX_LAYERED)
        if wanted != style:
            w.user32.SetWindowLongW(hwnd, gwl_exstyle, wanted)

    # -- preview card ------------------------------------------------------

    def _render_preview(self, now: float, force: bool = False) -> None:
        """Update the card only when its contents change; fade it in the compositor."""
        app = self.selected_app()
        if app is None:
            return

        key = (
            app.hwnd,
            self._thumb_stamp.get(app.hwnd, 0),
            app.label(title_privacy=self.cfg.title_privacy),
            self.preview_w,
            self.preview_h,
            self.cfg.title_privacy,
        )
        if force or key != self._card_key:
            self._card_key = key
            self._preview_t0 = now
            card = self._card_cache.get(key)
            if card is None:
                card = self._build_card(app)
                self._card_cache[key] = card
                _trim(self._card_cache, 12)
            if not self._preview.ensure(*card.size):
                return
            self._preview.write(card)

        fade = 1.0
        if PREVIEW_FADE > 0:
            fade = min(1.0, (now - self._preview_t0) / PREVIEW_FADE)
        x, y = self._preview_origin
        self._preview.present(x, y, 0.3 + 0.7 * _ease_out_cubic(fade))

    def _card_chrome(self) -> tuple[Image.Image, Image.Image]:
        """Frame and inner rounded mask — identical for every card at a size."""
        theme = self.theme
        pw, ph, pad = self.preview_w, self.preview_h, self.preview_pad
        key = (pw, ph, pad, self._theme_rev)
        cached = self._chrome_cache.get(key)
        if cached is not None:
            return cached

        width, height = pw + pad * 2, ph + pad * 2
        radius = int(14 * self.s)
        frame = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        ImageDraw.Draw(frame, "RGBA").rounded_rectangle(
            (0, 0, width - 1, height - 1),
            radius,
            fill=theme.card_fill,
            outline=alpha(_mix(theme.card_edge, theme.accent, 0.5), 200),
            width=max(1, int(round(1.5 * self.s))),
        )
        mask = Image.new("L", (pw, ph), 0)
        ImageDraw.Draw(mask).rounded_rectangle(
            (0, 0, pw - 1, ph - 1), max(2, radius - 6), fill=255
        )
        self._chrome_cache.clear()
        self._chrome_cache[key] = (frame, mask)
        return (frame, mask)

    def _build_card(self, app: AppWindow, fade: float = 1.0) -> Image.Image:
        """Chrome copy plus two blits.

        ``Image.alpha_composite`` at an offset copies and crops the whole
        destination first, so three of them cost more than the rest of the card
        put together. Everything here is opaque over opaque, so ``paste`` — a
        straight blit, with the rounded mask applied once — is equivalent.
        """
        pw, ph, pad = self.preview_w, self.preview_h, self.preview_pad
        frame, mask = self._card_chrome()
        img = frame.copy()

        thumb = self._thumbs.get(app.hwnd)
        if thumb is None:
            thumb = self._placeholder_card(app)

        fit = min(pw / thumb.width, ph / thumb.height)
        size = (max(1, int(thumb.width * fit)), max(1, int(thumb.height * fit)))
        if size != thumb.size:
            thumb = thumb.resize(size, Image.Resampling.BILINEAR)

        if size == (pw, ph):
            inner = thumb
        else:
            inner = Image.new("RGBA", (pw, ph), alpha(self.theme.hub_fill, 255))
            inner.paste(thumb, ((pw - size[0]) // 2, (ph - size[1]) // 2))
        img.paste(inner, (pad, pad), mask)

        if fade < 0.999:
            img = _scale_alpha(img, fade)
        return img

    def _placeholder_card(self, app: AppWindow) -> Image.Image:
        """Icon + status text, shown until the real capture lands."""
        key = (app.hwnd, app.minimized, self.preview_w, self.preview_h, self._theme_rev)
        cached = self._placeholder_cache.get(key)
        if cached is not None:
            return cached

        theme = self.theme
        pw, ph = self.preview_w, self.preview_h
        card = Image.new("RGBA", (pw, ph), alpha(theme.card_fill, 255))
        draw = ImageDraw.Draw(card, "RGBA")
        icon_px = int(76 * self.s)
        icon = self._icon_at(app, icon_px)
        if icon is not None:
            card.alpha_composite(icon, ((pw - icon_px) // 2, (ph - icon_px) // 2 - int(10 * self.s)))
        label = "minimised" if app.minimized else "loading preview…"
        _centered(draw, label, pw / 2, ph - int(30 * self.s), self.f_sub, theme.text_dim)

        self._placeholder_cache[key] = card
        _trim(self._placeholder_cache, 16)
        return card


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _lparam_point(lparam: int) -> tuple[int, int]:
    return (
        ctypes.c_short(lparam & 0xFFFF).value,
        ctypes.c_short((lparam >> 16) & 0xFFFF).value,
    )


def _trim(cache: dict, limit: int) -> None:
    while len(cache) > limit:
        cache.pop(next(iter(cache)))


def _floor_to(value: int, step: int) -> int:
    return (value // step) * step


def _ceil_to(value: int, step: int) -> int:
    return ((value + step - 1) // step) * step


def _dim_image(img: Image.Image, factor: float) -> Image.Image:
    arr = np.array(img, dtype=np.uint8)
    arr[..., :3] = (arr[..., :3].astype(np.float32) * factor).astype(np.uint8)
    return Image.fromarray(arr, "RGBA")


