"""Layered overlay: dim + GTA wheel matching the sketch layout.

Layout (from the user's sketch):
  - centered wheel with equal slices + icons
  - selected slice outlined
  - app name under the wheel
  - one visualisation preview of ONLY the highlighted app
"""

from __future__ import annotations

import ctypes
import math
import threading
import time
from typing import Callable, Optional

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from . import win32_types as w
from .wheel import WheelLayout, build_layout, pie_points
from .windows_enum import AppWindow, capture_thumbnail


def _load_font(size: int) -> ImageFont.ImageFont:
    for path in (
        r"C:\Windows\Fonts\segoeui.ttf",
        r"C:\Windows\Fonts\seguisb.ttf",
        r"C:\Windows\Fonts\arial.ttf",
    ):
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _ease_out_cubic(t: float) -> float:
    return 1 - (1 - t) ** 3


class Overlay:
    # Wheel-only panel (centered on screen)
    CANVAS_W = 440
    CANVAS_H = 460
    # Paint at 2× then LANCZOS-down for clean anti-aliased edges.
    RENDER_SCALE = 2
    APPEAR_DURATION = 0.12  # short snap — open must feel instant

    WHEEL_CX = CANVAS_W // 2
    WHEEL_CY = 190
    OUTER_R = 165
    INNER_R = 52

    PREVIEW_W = 400
    PREVIEW_H = 225
    PREVIEW_MARGIN = 48
    PREVIEW_PAD = 10

    # --- Dim backdrop (tweak here) ---
    # 0.0 = sharp desktop photo, 1.0 = fully soft. ~0.3 ≈ lightly frosted.
    DIM_BLUR = 1.0
    DIM_VEIL = 36  # 0–255 extra black after blur (darkness only)
    DIM_SCALE = 4  # work at 1/N resolution for speed

    def __init__(self) -> None:
        self.hwnd: int = 0
        self._dim_hwnd: int = 0
        self._preview_hwnd: int = 0
        self._wndproc_dim = None
        self._wndproc_wheel = None
        self._wndproc_preview = None
        self._visible = False
        self._apps: list[AppWindow] = []
        self._selected = 0
        self._layout: Optional[WheelLayout] = None
        self._monitor = (0, 0, 1920, 1080)
        self._dim_bounds = (0, 0, 1920, 1080)
        self._wheel_origin = (0, 0)
        self._preview_origin = (0, 0)
        self._on_commit: Optional[Callable[[], None]] = None
        self._on_cancel: Optional[Callable[[], None]] = None
        self._last_paint = 0.0
        self._appear_t = 0.0
        self._font = _load_font(24)
        self._font_small = _load_font(13)
        self._font_hi = _load_font(24 * self.RENDER_SCALE)
        self._thumb_cache: dict[int, Image.Image] = {}
        self._thumb_persist: dict[int, Image.Image] = {}  # survives hide → instant reopen
        self._icon_hi_cache: dict[int, Image.Image] = {}
        self._shadow_cache: dict[tuple, Image.Image] = {}
        self._last_painted_sel = -1
        self._preview_gen = 0
        self._pending_preview: tuple[int, int, Image.Image] | None = None  # gen, hwnd, img
        self._preview_lock = threading.Lock()
        self._mouse_armed = False
        self._mouse_anchor: tuple[int, int] | None = None
        self._mouse_arm_slop = 10

    def create(self) -> int:
        hinstance = w.kernel32.GetModuleHandleW(None)
        self._register(hinstance, "FunTabDimClass", self._make_dim_proc())
        self._register(hinstance, "FunTabWheelClass", self._make_wheel_proc())
        self._register(hinstance, "FunTabPreviewClass", self._make_preview_proc())

        common_ex = (
            w.WS_EX_LAYERED
            | w.WS_EX_TOPMOST
            | w.WS_EX_TOOLWINDOW
            | w.WS_EX_NOACTIVATE
        )

        self._dim_hwnd = int(
            w.user32.CreateWindowExW(
                common_ex,  # NOT transparent — absorbs clicks so they don't hit apps
                "FunTabDimClass",
                "Fun Tab Dim",
                w.WS_POPUP,
                0,
                0,
                100,
                100,
                None,
                None,
                hinstance,
                None,
            )
        )
        self.hwnd = int(
            w.user32.CreateWindowExW(
                common_ex,
                "FunTabWheelClass",
                "Fun Tab",
                w.WS_POPUP,
                0,
                0,
                self.CANVAS_W,
                self.CANVAS_H,
                None,
                None,
                hinstance,
                None,
            )
        )
        preview_w = self.PREVIEW_W + self.PREVIEW_PAD * 2
        preview_h = self.PREVIEW_H + self.PREVIEW_PAD * 2
        self._preview_hwnd = int(
            w.user32.CreateWindowExW(
                common_ex,  # absorb clicks on the preview card too
                "FunTabPreviewClass",
                "Fun Tab Preview",
                w.WS_POPUP,
                0,
                0,
                preview_w,
                preview_h,
                None,
                None,
                hinstance,
                None,
            )
        )
        if not self._dim_hwnd or not self.hwnd or not self._preview_hwnd:
            raise OSError("Failed to create overlay windows")
        return self.hwnd

    def _register(self, hinstance, class_name: str, wndproc) -> None:
        wc = w.WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(w.WNDCLASSEXW)
        wc.lpfnWndProc = wndproc
        wc.hInstance = hinstance
        wc.lpszClassName = class_name
        wc.hCursor = w.user32.LoadCursorW(None, 32512)
        w.user32.RegisterClassExW(ctypes.byref(wc))

    def _swallow_click(self, msg: int) -> bool:
        return msg in (
            w.WM_LBUTTONDOWN,
            w.WM_LBUTTONUP,
            w.WM_RBUTTONDOWN,
            w.WM_RBUTTONUP,
            w.WM_MBUTTONDOWN,
            w.WM_MBUTTONUP,
            w.WM_XBUTTONDOWN,
            w.WM_XBUTTONUP,
            w.WM_MOUSEWHEEL,
        )

    def _make_dim_proc(self):
        @w.WNDPROC
        def wndproc(hwnd, msg, wparam, lparam):
            if msg == w.WM_DESTROY:
                return 0
            if msg == w.WM_ERASEBKGND:
                return 1
            if msg == w.WM_NCHITTEST:
                return w.HTCLIENT
            # Eat all clicks — never let them reach apps underneath.
            if self._swallow_click(msg) or msg == w.WM_MOUSEMOVE:
                return 0
            return w.user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        self._wndproc_dim = wndproc
        return wndproc

    def _make_preview_proc(self):
        @w.WNDPROC
        def wndproc(hwnd, msg, wparam, lparam):
            if msg == w.WM_DESTROY:
                return 0
            if msg == w.WM_ERASEBKGND:
                return 1
            if msg == w.WM_NCHITTEST:
                return w.HTCLIENT
            if self._swallow_click(msg) or msg == w.WM_MOUSEMOVE:
                return 0
            return w.user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        self._wndproc_preview = wndproc
        return wndproc

    def _make_wheel_proc(self):
        @w.WNDPROC
        def wndproc(hwnd, msg, wparam, lparam):
            if msg == w.WM_DESTROY:
                w.user32.PostQuitMessage(0)
                return 0
            if msg == w.WM_LBUTTONUP:
                x = ctypes.c_short(lparam & 0xFFFF).value
                y = ctypes.c_short((lparam >> 16) & 0xFFFF).value
                # Only commit when the click lands on the wheel itself.
                if self._click_on_wheel(x, y) and self._on_commit:
                    self._on_commit()
                return 0
            if msg == w.WM_LBUTTONDOWN:
                return 0
            if self._swallow_click(msg) and msg != w.WM_MOUSEWHEEL:
                return 0
            if msg == w.WM_MOUSEMOVE:
                x = ctypes.c_short(lparam & 0xFFFF).value
                y = ctypes.c_short((lparam >> 16) & 0xFFFF).value
                self._handle_move(x, y, from_event=True)
                return 0
            if msg == w.WM_MOUSEWHEEL:
                delta = ctypes.c_short((wparam >> 16) & 0xFFFF).value
                self._arm_mouse()
                self.cycle(-1 if delta > 0 else 1)
                return 0
            if msg == w.WM_ERASEBKGND:
                return 1
            if msg == w.WM_NCHITTEST:
                # Transparent empty canvas → let dimmer eat the click.
                x = ctypes.c_short(lparam & 0xFFFF).value
                y = ctypes.c_short((lparam >> 16) & 0xFFFF).value
                pt = w.POINT(x, y)
                w.user32.ScreenToClient(hwnd, ctypes.byref(pt))
                if self._click_on_wheel(pt.x, pt.y):
                    return w.HTCLIENT
                return w.HTTRANSPARENT
            return w.user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        self._wndproc_wheel = wndproc
        return wndproc

    def _click_on_wheel(self, x: int, y: int) -> bool:
        if not self._layout:
            return False
        dx = x - self._layout.cx
        dy = y - self._layout.cy
        return math.hypot(dx, dy) <= self._layout.outer_r * 1.06

    def set_callbacks(
        self,
        on_commit: Callable[[], None],
        on_cancel: Callable[[], None],
    ) -> None:
        self._on_commit = on_commit
        self._on_cancel = on_cancel

    @property
    def visible(self) -> bool:
        return self._visible

    @property
    def selected_index(self) -> int:
        return self._selected

    @property
    def apps(self) -> list[AppWindow]:
        return self._apps

    def show(self, apps: list[AppWindow], selected: int = 0) -> None:
        if not apps:
            return
        self._apps = apps
        self._selected = max(0, min(selected, len(apps) - 1))
        self._thumb_cache.clear()
        # Reuse last session's previews instantly when possible.
        for app in apps:
            cached = self._thumb_persist.get(app.hwnd)
            if cached is not None:
                self._thumb_cache[app.hwnd] = cached
        self._icon_hi_cache.clear()
        self._shadow_cache.clear()
        self._last_painted_sel = -1
        self._preview_sel = -2
        self._preview_opacity = -1
        self._preview_gen += 1
        self._appear_t = time.perf_counter()
        self._update_monitor()

        left, top, right, bottom = self._monitor
        width, height = right - left, bottom - top
        wx = left + (width - self.CANVAS_W) // 2
        wy = top + (height - self.CANVAS_H) // 2
        self._wheel_origin = (wx, wy)
        self._dim_bounds = (left, top, right, bottom)

        preview_w = self.PREVIEW_W + self.PREVIEW_PAD * 2
        preview_h = self.PREVIEW_H + self.PREVIEW_PAD * 2
        px = left + self.PREVIEW_MARGIN
        py = top + self.PREVIEW_MARGIN
        self._preview_origin = (px, py)

        # Dimmer at full strength immediately — no wait for captures.
        self._show_dimmer(left, top, width, height, fade=1.0)

        w.user32.SetWindowPos(
            self._dim_hwnd,
            w.HWND_TOPMOST,
            left,
            top,
            width,
            height,
            w.SWP_SHOWWINDOW | w.SWP_NOACTIVATE,
        )
        w.user32.SetWindowPos(
            self.hwnd,
            w.HWND_TOPMOST,
            wx,
            wy,
            self.CANVAS_W,
            self.CANVAS_H,
            w.SWP_SHOWWINDOW | w.SWP_NOACTIVATE,
        )
        w.user32.SetWindowPos(
            self._preview_hwnd,
            w.HWND_TOPMOST,
            px,
            py,
            preview_w,
            preview_h,
            w.SWP_SHOWWINDOW | w.SWP_NOACTIVATE,
        )
        self._visible = True
        self._mouse_armed = False
        pt = w.POINT()
        w.user32.GetCursorPos(ctypes.byref(pt))
        self._mouse_anchor = (int(pt.x), int(pt.y))

        # Wheel on screen NOW — preview fills in async.
        self._paint(force=True)
        self._kick_preview_capture(self._apps[self._selected].hwnd)

    def hide(self) -> None:
        if not self._visible:
            return
        self._visible = False
        self._preview_gen += 1  # invalidate in-flight captures
        flags = w.SWP_HIDEWINDOW | w.SWP_NOMOVE | w.SWP_NOSIZE | w.SWP_NOACTIVATE
        w.user32.SetWindowPos(self.hwnd, w.HWND_TOPMOST, 0, 0, 0, 0, flags)
        w.user32.SetWindowPos(self._preview_hwnd, w.HWND_TOPMOST, 0, 0, 0, 0, flags)
        w.user32.SetWindowPos(self._dim_hwnd, w.HWND_TOPMOST, 0, 0, 0, 0, flags)
        self._apps = []
        self._layout = None
        self._thumb_cache.clear()
        self._icon_hi_cache.clear()
        self._shadow_cache.clear()
        self._last_painted_sel = -1
        self._mouse_armed = False
        self._mouse_anchor = None
        with self._preview_lock:
            self._pending_preview = None

    def cycle(self, delta: int) -> None:
        if not self._apps:
            return
        self._selected = (self._selected + delta) % len(self._apps)
        self._paint(force=True)
        self._kick_preview_capture(self._apps[self._selected].hwnd)

    def select(self, index: int) -> None:
        if not self._apps:
            return
        index = max(0, min(index, len(self._apps) - 1))
        if index == self._selected:
            return
        self._selected = index
        self._paint(force=True)
        self._kick_preview_capture(self._apps[self._selected].hwnd)

    def pump_idle(self) -> None:
        if not self._visible:
            return
        self._apply_pending_preview()
        now = time.perf_counter()
        if now - self._last_paint < 1 / 60:
            self._poll_cursor()
            return
        self._poll_cursor()
        elapsed = now - self._appear_t
        if elapsed < self.APPEAR_DURATION:
            self._paint(force=True)

    def _appear_state(self, now: float) -> tuple[float, float]:
        # Nearly instant: tiny scale pop only.
        if self.APPEAR_DURATION <= 0:
            return 1.0, 1.0
        t = min(1.0, (now - self._appear_t) / self.APPEAR_DURATION)
        eased = _ease_out_cubic(t)
        scale = 0.94 + 0.06 * eased
        opacity = 1.0
        return scale, opacity

    def _kick_preview_capture(self, hwnd: int) -> None:
        """Capture off the UI thread so Alt+Tab never waits on PrintWindow."""
        # Persist = real capture. Session cache may only hold a placeholder.
        if hwnd in self._thumb_persist:
            self._thumb_cache[hwnd] = self._thumb_persist[hwnd]
            return
        gen = self._preview_gen
        app = next((a for a in self._apps if a.hwnd == hwnd), None)

        def worker() -> None:
            cap = capture_thumbnail(
                hwnd,
                max_width=self.PREVIEW_W,
                max_height=self.PREVIEW_H,
                allow_screen_grab=False,
            )
            if cap is None and app is not None:
                cap = self._icon_fallback_card(app)
            if cap is None:
                return
            with self._preview_lock:
                if gen != self._preview_gen:
                    return
                self._pending_preview = (gen, hwnd, cap)

        threading.Thread(target=worker, daemon=True).start()

    def _apply_pending_preview(self) -> None:
        with self._preview_lock:
            pending = self._pending_preview
            self._pending_preview = None
        if not pending:
            return
        gen, hwnd, cap = pending
        if gen != self._preview_gen or not self._visible:
            return
        self._thumb_cache[hwnd] = cap
        self._thumb_persist[hwnd] = cap
        # Refresh preview card if this is still the selection.
        if self._apps and self._apps[self._selected].hwnd == hwnd:
            self._preview_sel = -2  # force preview repaint
            self._paint_preview(1.0)
            self._preview_sel = self._selected
            self._preview_opacity = 1.0

    def _arm_mouse(self) -> None:
        self._mouse_armed = True
        self._mouse_anchor = None

    def _show_dimmer(
        self, left: int, top: int, width: int, height: int, fade: float = 1.0
    ) -> None:
        """Frozen desktop snapshot, blurred, then lightly veiled.

        UpdateLayeredWindow does NOT stretch — the bitmap must be exactly
        width×height or the rest of the screen stays transparent (clicks
        fall through and the live wallpaper shows sharp).
        """
        self._clear_acrylic(self._dim_hwnd)
        self._prepare_ulw(self._dim_hwnd)
        backdrop = self._make_blur_backdrop(left, top, width, height, fade)
        if backdrop is None:
            alpha = max(8, min(255, int(self.DIM_VEIL * fade)))
            backdrop = Image.new("RGBA", (width, height), (0, 0, 0, alpha))
        self._blit_to(self._dim_hwnd, backdrop, left, top)

    def _prepare_ulw(self, hwnd: int) -> None:
        """Clear constant-alpha layered mode so UpdateLayeredWindow can paint."""
        gwl_exstyle = -20
        ex = int(w.user32.GetWindowLongW(hwnd, gwl_exstyle))
        w.user32.SetWindowLongW(hwnd, gwl_exstyle, ex & ~w.WS_EX_LAYERED)
        w.user32.SetWindowLongW(hwnd, gwl_exstyle, ex | w.WS_EX_LAYERED)

    def _make_blur_backdrop(
        self, left: int, top: int, width: int, height: int, fade: float
    ) -> Image.Image | None:
        scale = max(2, int(self.DIM_SCALE))
        sw = max(8, width // scale)
        sh = max(8, height // scale)
        shot = self._bitblt_region(left, top, width, height, sw, sh)
        if shot is None:
            return None

        # Soft plate — radius scales with the downsampled size.
        radius = max(3, min(sw, sh) // 18)
        soft = shot.filter(ImageFilter.GaussianBlur(radius=radius))
        amount = max(0.0, min(1.0, float(self.DIM_BLUR))) * fade
        if amount < 0.01:
            mixed = shot
        elif amount > 0.99:
            mixed = soft
        else:
            mixed = Image.blend(shot.convert("RGB"), soft.convert("RGB"), amount)

        # Upscale to the real monitor size (ULW is 1:1, no stretch).
        out = mixed.resize((width, height), Image.Resampling.BILINEAR).convert("RGBA")
        r, g, b, _a = out.split()
        out = Image.merge("RGBA", (r, g, b, Image.new("L", out.size, 255)))

        veil_a = max(0, min(255, int(self.DIM_VEIL * fade)))
        if veil_a > 0:
            veil = Image.new("RGBA", out.size, (0, 0, 0, veil_a))
            out = Image.alpha_composite(out, veil)
        return out

    def _bitblt_region(
        self,
        left: int,
        top: int,
        width: int,
        height: int,
        out_w: int,
        out_h: int,
    ) -> Image.Image | None:
        """StretchBlt the monitor into a small RGB image."""
        SRCCOPY = 0x00CC0020
        HALFTONE = 4
        hdc_screen = w.user32.GetDC(0)
        if not hdc_screen:
            return None
        hdc_mem = w.gdi32.CreateCompatibleDC(hdc_screen)
        hbmp = w.gdi32.CreateCompatibleBitmap(hdc_screen, out_w, out_h)
        if not hbmp:
            w.gdi32.DeleteDC(hdc_mem)
            w.user32.ReleaseDC(0, hdc_screen)
            return None
        old = w.gdi32.SelectObject(hdc_mem, hbmp)
        try:
            w.gdi32.SetStretchBltMode(hdc_mem, HALFTONE)
            ok = w.gdi32.StretchBlt(
                hdc_mem,
                0,
                0,
                out_w,
                out_h,
                hdc_screen,
                left,
                top,
                width,
                height,
                SRCCOPY,
            )
            if not ok:
                return None
            bmi = w.BITMAPINFO()
            bmi.bmiHeader.biSize = ctypes.sizeof(w.BITMAPINFOHEADER)
            bmi.bmiHeader.biWidth = out_w
            bmi.bmiHeader.biHeight = -out_h
            bmi.bmiHeader.biPlanes = 1
            bmi.bmiHeader.biBitCount = 32
            bmi.bmiHeader.biCompression = w.BI_RGB
            buf_len = out_w * out_h * 4
            buf = (ctypes.c_ubyte * buf_len)()
            got = w.gdi32.GetDIBits(
                hdc_mem, hbmp, 0, out_h, buf, ctypes.byref(bmi), w.DIB_RGB_COLORS
            )
            if not got:
                return None
            return Image.frombuffer(
                "RGBA", (out_w, out_h), bytes(buf), "raw", "BGRA", 0, 1
            ).convert("RGB")
        finally:
            w.gdi32.SelectObject(hdc_mem, old)
            w.gdi32.DeleteObject(hbmp)
            w.gdi32.DeleteDC(hdc_mem)
            w.user32.ReleaseDC(0, hdc_screen)

    def _clear_acrylic(self, hwnd: int) -> None:
        if not w.SetWindowCompositionAttribute:
            return
        try:
            accent = w.ACCENTPOLICY(w.ACCENT_DISABLED, 0, 0, 0)
            data = w.WINDOWCOMPOSITIONATTRIBDATA(
                w.WCA_ACCENT_POLICY,
                ctypes.cast(ctypes.pointer(accent), ctypes.c_void_p),
                ctypes.sizeof(accent),
            )
            w.SetWindowCompositionAttribute(hwnd, ctypes.byref(data))
        except Exception:
            pass

    def _cache_preview(self, app: AppWindow, *, allow_screen_grab: bool = False) -> None:
        if app.hwnd in self._thumb_cache:
            return
        persisted = self._thumb_persist.get(app.hwnd)
        if persisted is not None:
            self._thumb_cache[app.hwnd] = persisted
            return
        # Placeholder only — real capture is async via _kick_preview_capture.
        self._thumb_cache[app.hwnd] = self._icon_fallback_card(app)

    def _icon_fallback_card(self, app: AppWindow) -> Image.Image:
        cap = Image.new("RGBA", (self.PREVIEW_W, self.PREVIEW_H), (28, 32, 42, 255))
        d = ImageDraw.Draw(cap)
        d.rounded_rectangle(
            (8, 8, self.PREVIEW_W - 8, self.PREVIEW_H - 8),
            14,
            fill=(40, 46, 58, 255),
            outline=(70, 80, 100, 200),
            width=2,
        )
        if app.icon:
            ic = app.icon.resize((72, 72), Image.Resampling.LANCZOS)
            cap.alpha_composite(
                ic,
                ((self.PREVIEW_W - 72) // 2, (self.PREVIEW_H - 72) // 2 - 8),
            )
        if w.user32.IsIconic(app.hwnd):
            tb = d.textbbox((0, 0), "minimized", font=self._font_small)
            tw = tb[2] - tb[0]
            d.text(
                ((self.PREVIEW_W - tw) / 2, self.PREVIEW_H - 36),
                "minimized",
                font=self._font_small,
                fill=(160, 170, 185, 220),
            )
        return cap

    def _get_selected_preview(self) -> Image.Image:
        app = self._apps[self._selected]
        self._cache_preview(app)
        return self._thumb_cache[app.hwnd]

    def _update_monitor(self) -> None:
        pt = w.POINT()
        w.user32.GetCursorPos(ctypes.byref(pt))
        mon = w.user32.MonitorFromPoint(pt, w.MONITOR_DEFAULTTONEAREST)
        info = w.MONITORINFO()
        info.cbSize = ctypes.sizeof(w.MONITORINFO)
        if w.user32.GetMonitorInfoW(mon, ctypes.byref(info)):
            r = info.rcMonitor
            self._monitor = (r.left, r.top, r.right, r.bottom)
        else:
            self._monitor = (
                0,
                0,
                w.user32.GetSystemMetrics(0),
                w.user32.GetSystemMetrics(1),
            )

    def _poll_cursor(self) -> None:
        pt = w.POINT()
        w.user32.GetCursorPos(ctypes.byref(pt))
        sx, sy = int(pt.x), int(pt.y)

        if not self._mouse_armed:
            if self._mouse_anchor is None:
                self._mouse_anchor = (sx, sy)
                return
            ax, ay = self._mouse_anchor
            if abs(sx - ax) < self._mouse_arm_slop and abs(sy - ay) < self._mouse_arm_slop:
                return
            self._arm_mouse()

        wx, wy = self._wheel_origin
        self._handle_move(sx - wx, sy - wy, from_event=False)

    def _handle_move(self, x: int, y: int, from_event: bool = False) -> None:
        if not self._visible or not self._layout:
            return

        if not self._mouse_armed:
            if from_event:
                pt = w.POINT()
                w.user32.GetCursorPos(ctypes.byref(pt))
                if self._mouse_anchor is None:
                    self._mouse_anchor = (int(pt.x), int(pt.y))
                    return
                ax, ay = self._mouse_anchor
                if abs(int(pt.x) - ax) < self._mouse_arm_slop and abs(
                    int(pt.y) - ay
                ) < self._mouse_arm_slop:
                    return
                self._arm_mouse()
            else:
                return

        hit = self._layout.hit_test(x, y)
        if hit is not None and hit != self._selected:
            self.select(hit)

    def _soft_shadow(
        self, rw: int, rh: int, rcx: float, rcy: float, outer: float, ss: int
    ) -> Image.Image:
        """Cheap soft shadow — GaussianBlur at 2× was ~200ms+/frame."""
        key = (rw, rh, int(outer), ss)
        cached = self._shadow_cache.get(key)
        if cached is not None:
            return cached
        shadow = Image.new("RGBA", (rw, rh), (0, 0, 0, 0))
        sdraw = ImageDraw.Draw(shadow, "RGBA")
        for i, alpha in enumerate((28, 40, 55)):
            pad = (18 - i * 5) * ss
            sdraw.ellipse(
                (
                    rcx - outer - pad,
                    rcy - outer - pad,
                    rcx + outer + pad,
                    rcy + outer + pad,
                ),
                fill=(0, 0, 0, alpha),
            )
        self._shadow_cache[key] = shadow
        return shadow

    def _paint(self, force: bool = False) -> None:
        if not self._visible and not force:
            return
        now = time.perf_counter()
        animating = (now - self._appear_t) < self.APPEAR_DURATION
        if not force and not animating and now - self._last_paint < 1 / 60:
            return
        if (
            not force
            and not animating
            and self._selected == self._last_painted_sel
            and now - self._last_paint < 0.05
        ):
            return
        self._last_paint = now
        self._last_painted_sel = self._selected

        anim_scale, opacity = self._appear_state(now)
        # 1× while popping in (smooth anim); 2× when settled (HD edges).
        ss = 1 if animating and anim_scale < 0.98 else self.RENDER_SCALE
        cw, ch = self.CANVAS_W, self.CANVAS_H
        cx, cy = float(self.WHEEL_CX), float(self.WHEEL_CY)
        outer_1x = self.OUTER_R * anim_scale
        inner_1x = self.INNER_R * anim_scale
        self._layout = build_layout(len(self._apps), cx, cy, outer_1x, inner_1x)

        rw, rh = cw * ss, ch * ss
        rcx, rcy = cx * ss, cy * ss
        outer = outer_1x * ss
        inner = inner_1x * ss

        img = Image.new("RGBA", (rw, rh), (0, 0, 0, 0))
        img = Image.alpha_composite(
            img, self._soft_shadow(rw, rh, rcx, rcy, outer, ss)
        )
        draw = ImageDraw.Draw(img, "RGBA")
        hi_layout = build_layout(len(self._apps), rcx, rcy, outer, inner)

        draw.ellipse(
            (rcx - outer, rcy - outer, rcx + outer, rcy + outer),
            fill=(28, 30, 36, 245),
            outline=(55, 58, 68, 255),
            width=max(2, 2 * ss),
        )

        n = len(self._apps)
        for sl in hi_layout.slices:
            selected = sl.index == self._selected
            fill = (48, 52, 64, 255) if selected else (28, 30, 36, 245)
            pts = pie_points(
                rcx, rcy, inner, outer, sl.start_cw, sl.end_cw, steps=64
            )
            draw.polygon(pts, fill=fill)

            if n > 1:
                a = sl.start_cw
                draw.line(
                    (
                        rcx + math.cos(a) * inner,
                        rcy - math.sin(a) * inner,
                        rcx + math.cos(a) * outer,
                        rcy - math.sin(a) * outer,
                    ),
                    fill=(70, 74, 86, 220),
                    width=max(2, 2 * ss),
                )

            if selected:
                outline_pts = pie_points(
                    rcx, rcy, inner + ss, outer - ss, sl.start_cw, sl.end_cw, steps=72
                )
                draw.line(
                    outline_pts + [outline_pts[0]],
                    fill=(80, 160, 255, 255),
                    width=max(3, 3 * ss),
                )

            app = self._apps[sl.index]
            if app.icon is not None:
                icon_size = (44 if n <= 4 else (36 if n <= 6 else 28)) * ss
                if n == 1:
                    icon_size = 56 * ss
                cache_key = (id(app.icon), icon_size)
                ic = self._icon_hi_cache.get(cache_key)
                if ic is None:
                    ic = app.icon.resize(
                        (icon_size, icon_size), Image.Resampling.LANCZOS
                    )
                    self._icon_hi_cache[cache_key] = ic
                ix = int(sl.icon_x - icon_size / 2)
                iy = int(sl.icon_y - icon_size / 2)
                img.alpha_composite(ic, (ix, iy))

        draw.ellipse(
            (rcx - inner, rcy - inner, rcx + inner, rcy + inner),
            fill=(18, 20, 26, 255),
            outline=(70, 74, 86, 255),
            width=max(2, 2 * ss),
        )

        name = self._apps[self._selected].display_name if self._apps else ""
        name_y = rcy + outer + 28 * ss
        self._draw_centered_text(
            draw,
            name,
            rcx,
            name_y,
            self._font_hi,
            outer * 2.2,
            fill=(235, 238, 245, 255),
        )

        img = img.resize((cw, ch), Image.Resampling.LANCZOS)

        if anim_scale < 0.995 or opacity < 0.995:
            sw, sh = max(1, int(cw * anim_scale)), max(1, int(ch * anim_scale))
            scaled = img.resize((sw, sh), Image.Resampling.BILINEAR)
            if opacity < 1.0:
                r, g, b, a = scaled.split()
                a = a.point(lambda v: int(v * opacity))
                scaled = Image.merge("RGBA", (r, g, b, a))
            framed = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
            framed.alpha_composite(scaled, ((cw - sw) // 2, (ch - sh) // 2))
            img = framed

        wx, wy = self._wheel_origin
        self._blit_to(self.hwnd, img, wx, wy)
        # Preview: only when selection changes or opacity steps (not every anim frame).
        op_step = round(opacity, 1)
        if (
            self._selected != getattr(self, "_preview_sel", -2)
            or op_step != getattr(self, "_preview_opacity", -1)
        ):
            self._paint_preview(opacity)
            self._preview_sel = self._selected
            self._preview_opacity = op_step

    def _paint_preview(self, opacity: float = 1.0) -> None:
        """Preview card in the monitor's top-left (with margin)."""
        if not self._apps or not self._preview_hwnd:
            return

        pw, ph = self.PREVIEW_W, self.PREVIEW_H
        pad = self.PREVIEW_PAD
        cw, ch = pw + pad * 2, ph + pad * 2

        img = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img, "RGBA")

        draw.rounded_rectangle(
            (0, 0, cw - 1, ch - 1),
            14,
            fill=(16, 18, 24, int(235 * opacity)),
            outline=(80, 160, 255, int(210 * opacity)),
            width=2,
        )

        thumb = self._get_selected_preview()
        tw, th = thumb.size
        fit = min(pw / tw, ph / th)
        rw, rh = max(1, int(tw * fit)), max(1, int(th * fit))
        fitted = thumb.resize((rw, rh), Image.Resampling.LANCZOS)

        card = Image.new("RGBA", (pw, ph), (22, 24, 30, 255))
        ox, oy = (pw - rw) // 2, (ph - rh) // 2
        card.alpha_composite(fitted.convert("RGBA"), (ox, oy))

        mask = Image.new("L", (pw, ph), 0)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, pw - 1, ph - 1), 8, fill=255)
        card.putalpha(mask)
        img.alpha_composite(card, (pad, pad))

        if opacity < 0.995:
            r, g, b, a = img.split()
            a = a.point(lambda v: int(v * opacity))
            img = Image.merge("RGBA", (r, g, b, a))

        px, py = self._preview_origin
        self._blit_to(self._preview_hwnd, img, px, py)

    def _blit_to(
        self,
        hwnd: int,
        img: Image.Image,
        left: int,
        top: int,
        dest_size: tuple[int, int] | None = None,
    ) -> None:
        import numpy as np

        width, height = img.size
        arr = np.asarray(img.convert("RGBA"), dtype=np.uint8)
        # Premultiply + BGRA in one vectorized pass
        a = arr[..., 3:4].astype(np.uint16)
        rgb = (arr[..., :3].astype(np.uint16) * a // 255).astype(np.uint8)
        bgra = np.empty((height, width, 4), dtype=np.uint8)
        bgra[..., 0] = rgb[..., 2]
        bgra[..., 1] = rgb[..., 1]
        bgra[..., 2] = rgb[..., 0]
        bgra[..., 3] = arr[..., 3]
        buf = np.ascontiguousarray(bgra).tobytes()

        hdc_screen = w.user32.GetDC(0)
        hdc_mem = w.gdi32.CreateCompatibleDC(hdc_screen)
        bmi = w.BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(w.BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = width
        bmi.bmiHeader.biHeight = -height
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = w.BI_RGB

        bits = ctypes.c_void_p()
        hbmp = w.gdi32.CreateDIBSection(
            hdc_mem,
            ctypes.byref(bmi),
            w.DIB_RGB_COLORS,
            ctypes.byref(bits),
            None,
            0,
        )
        if not hbmp:
            w.gdi32.DeleteDC(hdc_mem)
            w.user32.ReleaseDC(0, hdc_screen)
            return

        ctypes.memmove(bits, buf, len(buf))
        old = w.gdi32.SelectObject(hdc_mem, hbmp)

        out_w, out_h = dest_size if dest_size else (width, height)
        size = w.SIZE(out_w, out_h)
        pt_src = w.POINT(0, 0)
        pt_dst = w.POINT(left, top)
        blend = w.BLENDFUNCTION(w.AC_SRC_OVER, 0, 255, w.AC_SRC_ALPHA)

        w.user32.UpdateLayeredWindow(
            hwnd,
            hdc_screen,
            ctypes.byref(pt_dst),
            ctypes.byref(size),
            hdc_mem,
            ctypes.byref(pt_src),
            0,
            ctypes.byref(blend),
            w.ULW_ALPHA,
        )

        w.gdi32.SelectObject(hdc_mem, old)
        w.gdi32.DeleteObject(hbmp)
        w.gdi32.DeleteDC(hdc_mem)
        w.user32.ReleaseDC(0, hdc_screen)

    def _draw_centered_text(
        self,
        draw: ImageDraw.ImageDraw,
        text: str,
        cx: float,
        cy: float,
        font: ImageFont.ImageFont,
        max_width: float,
        fill=(235, 240, 250, 255),
    ) -> None:
        if not text:
            return
        display = text
        bbox = draw.textbbox((0, 0), display, font=font)
        tw = bbox[2] - bbox[0]
        if tw > max_width:
            while len(display) > 1:
                display = display[:-1]
                bbox = draw.textbbox((0, 0), display + "…", font=font)
                tw = bbox[2] - bbox[0]
                if tw <= max_width:
                    break
            display += "…"
            bbox = draw.textbbox((0, 0), display, font=font)
            tw = bbox[2] - bbox[0]
        th = bbox[3] - bbox[1]
        draw.text(
            (cx - tw / 2 + 1, cy - th / 2 + 1), display, font=font, fill=(0, 0, 0, 140)
        )
        draw.text((cx - tw / 2, cy - th / 2), display, font=font, fill=fill)
