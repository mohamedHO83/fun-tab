"""Reusable GDI surfaces for the overlay.

The original renderer created a DC + DIB section, copied the pixels through a
``bytes`` object and tore everything down again on *every* frame. At 4K that is
tens of megabytes of churn per second. Here each window keeps one DIB section
alive and NumPy writes straight into its pixel memory, so a frame costs one
composite plus one ``UpdateLayeredWindow``.
"""

from __future__ import annotations

import ctypes

import numpy as np
from PIL import Image

from . import win32_types as w


class Dib:
    """A top-down 32bpp DIB section with a NumPy view of its pixels (BGRA)."""

    def __init__(self) -> None:
        self._hdc: int = 0
        self._hbmp: int = 0
        self._old: int = 0
        self._bits: ctypes.c_void_p | None = None
        self._view: np.ndarray | None = None
        self._size: tuple[int, int] = (0, 0)

    @property
    def hdc(self) -> int:
        return self._hdc

    @property
    def size(self) -> tuple[int, int]:
        return self._size

    @property
    def view(self) -> np.ndarray | None:
        """(height, width, 4) uint8 BGRA array aliasing the DIB memory."""
        return self._view

    def ensure(self, width: int, height: int) -> bool:
        width = max(1, int(width))
        height = max(1, int(height))
        if self._size == (width, height) and self._hbmp:
            return True
        self.destroy()

        screen_dc = w.user32.GetDC(0)
        if not screen_dc:
            return False
        try:
            hdc = w.gdi32.CreateCompatibleDC(screen_dc)
            if not hdc:
                return False

            bmi = w.BITMAPINFO()
            bmi.bmiHeader.biSize = ctypes.sizeof(w.BITMAPINFOHEADER)
            bmi.bmiHeader.biWidth = width
            bmi.bmiHeader.biHeight = -height  # top-down: row 0 is the top row
            bmi.bmiHeader.biPlanes = 1
            bmi.bmiHeader.biBitCount = 32
            bmi.bmiHeader.biCompression = w.BI_RGB

            bits = ctypes.c_void_p()
            hbmp = w.gdi32.CreateDIBSection(
                hdc, ctypes.byref(bmi), w.DIB_RGB_COLORS, ctypes.byref(bits), None, 0
            )
            if not hbmp or not bits:
                w.gdi32.DeleteDC(hdc)
                return False

            self._hdc = int(hdc)
            self._hbmp = int(hbmp)
            self._bits = bits
            self._old = int(w.gdi32.SelectObject(hdc, hbmp) or 0)
            self._size = (width, height)

            buffer = (ctypes.c_uint8 * (width * height * 4)).from_address(bits.value)
            self._view = np.ctypeslib.as_array(buffer).reshape(height, width, 4)
            return True
        finally:
            w.user32.ReleaseDC(0, screen_dc)

    def write(self, img: Image.Image, opaque: bool = False) -> bool:
        """Copy a PIL RGBA image into the DIB as premultiplied BGRA.

        ``UpdateLayeredWindow`` wants premultiplied BGRA, and Pillow's ``BGRa``
        packer produces exactly that in a single C pass. Going via
        ``convert("RGBa")`` first walks the canvas twice for the same result
        (~4.1ms vs ~2.6ms at 488x570), and doing it in NumPy is far worse
        still: 8-bit premultiply needs 16-bit intermediates, so it turns into
        several strided passes over twice the memory.
        """
        if self._view is None or self._bits is None:
            return False
        height, width = self._view.shape[:2]
        if img.size != (width, height):
            return False
        if img.mode != "RGBA":
            img = img.convert("RGBA")

        data = img.tobytes("raw", "BGRA" if opaque else "BGRa")
        ctypes.memmove(self._bits, data, len(data))
        return True

    def write_box(self, img: Image.Image, x: int, y: int) -> bool:
        """Copy a patch into part of the DIB, leaving the rest of it standing.

        The DIB outlives each frame, so a change confined to a small region
        need not re-pack the whole canvas — which is the dominant cost of a
        full write, and pure waste when only a cursor needle has moved.
        """
        if self._view is None:
            return False
        height, width = self._view.shape[:2]
        x0, y0 = int(x), int(y)
        if x0 < 0 or y0 < 0 or x0 + img.width > width or y0 + img.height > height:
            return False
        if img.mode != "RGBA":
            img = img.convert("RGBA")

        data = img.tobytes("raw", "BGRa")
        patch = np.frombuffer(data, dtype=np.uint8).reshape(img.height, img.width, 4)
        self._view[y0 : y0 + img.height, x0 : x0 + img.width] = patch
        return True

    def fill_opaque_black(self) -> None:
        if self._view is not None:
            self._view[...] = 0
            self._view[..., 3] = 255

    def flush(self) -> None:
        w.gdi32.GdiFlush()

    def destroy(self) -> None:
        self._view = None
        self._bits = None
        if self._hdc:
            if self._old:
                w.gdi32.SelectObject(self._hdc, self._old)
            if self._hbmp:
                w.gdi32.DeleteObject(self._hbmp)
            w.gdi32.DeleteDC(self._hdc)
        self._hdc = 0
        self._hbmp = 0
        self._old = 0
        self._size = (0, 0)


class LayeredSurface(Dib):
    """DIB bound to one layered window, presented via UpdateLayeredWindow."""

    def __init__(self, hwnd: int = 0) -> None:
        super().__init__()
        self.hwnd = int(hwnd)

    def present(self, x: int, y: int, opacity: float = 1.0) -> bool:
        """Show the DIB. ``opacity`` is applied by the compositor, for free."""
        if not self.hwnd or self._view is None:
            return False
        width, height = self._size
        size = w.SIZE(width, height)
        src = w.POINT(0, 0)
        dst = w.POINT(int(x), int(y))
        constant = max(0, min(255, int(round(opacity * 255))))
        blend = w.BLENDFUNCTION(w.AC_SRC_OVER, 0, constant, w.AC_SRC_ALPHA)
        return bool(
            w.user32.UpdateLayeredWindow(
                self.hwnd,
                None,  # NULL destination DC == the desktop
                ctypes.byref(dst),
                ctypes.byref(size),
                self._hdc,
                ctypes.byref(src),
                0,
                ctypes.byref(blend),
                w.ULW_ALPHA,
            )
        )

    def paint(
        self,
        img: Image.Image,
        x: int,
        y: int,
        opaque: bool = False,
        opacity: float = 1.0,
    ) -> bool:
        if not self.ensure(*img.size):
            return False
        if not self.write(img, opaque=opaque):
            return False
        return self.present(x, y, opacity)


class ScreenGrabber:
    """Captures a monitor into a small reusable DIB and scales it back up in GDI.

    Doing the upscale with ``StretchBlt`` instead of PIL keeps every
    megapixel-scale operation inside the graphics driver; Python only ever
    touches the tiny blurred plate.
    """

    def __init__(self) -> None:
        self._small = Dib()

    def grab(
        self, left: int, top: int, width: int, height: int, out_w: int, out_h: int
    ) -> Image.Image | None:
        """Screenshot a monitor region, downscaled to out_w x out_h (RGB)."""
        if not self._small.ensure(out_w, out_h):
            return None
        screen_dc = w.user32.GetDC(0)
        if not screen_dc:
            return None
        try:
            # COLORONCOLOR, not HALFTONE: HALFTONE costs ~20ms more per blit at
            # 1080p and the plate gets blurred immediately afterwards anyway.
            w.gdi32.SetStretchBltMode(self._small.hdc, w.COLORONCOLOR)
            ok = w.gdi32.StretchBlt(
                self._small.hdc,
                0,
                0,
                out_w,
                out_h,
                screen_dc,
                left,
                top,
                width,
                height,
                w.SRCCOPY,
            )
        finally:
            w.user32.ReleaseDC(0, screen_dc)
        if not ok:
            return None
        w.gdi32.GdiFlush()
        view = self._small.view
        if view is None:
            return None
        rgb = view[..., 2::-1]  # BGRA -> RGB
        return Image.fromarray(np.ascontiguousarray(rgb), "RGB")

    def stretch_to(self, hdc: int, dest_w: int, dest_h: int, img: Image.Image) -> bool:
        """Blow a small opaque plate up onto a DC, scaling inside the driver.

        Used to paint the backdrop window: pushing a 240x135 plate through
        StretchBlt costs a couple of milliseconds, where making the backdrop a
        layered window would mean uploading a screen-sized bitmap instead.
        """
        small_w, small_h = img.size
        if not self._small.ensure(small_w, small_h):
            return False
        # Opaque plate, so premultiplied and straight alpha are identical.
        if img.mode != "RGBA":
            img = img.convert("RGBA")
        if not self._small.write(img, opaque=True):
            return False
        w.gdi32.GdiFlush()

        w.gdi32.SetStretchBltMode(hdc, w.COLORONCOLOR)
        ok = w.gdi32.StretchBlt(
            hdc, 0, 0, dest_w, dest_h, self._small.hdc, 0, 0, small_w, small_h, w.SRCCOPY
        )
        w.gdi32.GdiFlush()
        return bool(ok)

    def destroy(self) -> None:
        self._small.destroy()
