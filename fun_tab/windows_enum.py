"""Enumerate switchable windows, extract icons, and activate targets."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass, field
from typing import Optional

from PIL import Image

from . import win32_types as w


@dataclass
class AppWindow:
    hwnd: int
    title: str
    class_name: str
    pid: int
    icon: Optional[Image.Image] = field(default=None, repr=False)

    @property
    def display_name(self) -> str:
        title = (self.title or "").strip()
        if title:
            # Prefer the app-ish part after the last separator when titles are long.
            for sep in (" - ", " — ", " | "):
                if sep in title:
                    left, right = title.rsplit(sep, 1)
                    # Usually "Document - App"; show App if short, else full.
                    if 0 < len(right) <= 40:
                        return right.strip()
            if len(title) > 48:
                return title[:45] + "…"
            return title
        return self.class_name or f"Window {self.hwnd}"


def _window_title(hwnd: int) -> str:
    length = w.user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    w.user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value


def _class_name(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    w.user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def _is_alt_tab_candidate(hwnd: int, self_hwnd: int | None) -> bool:
    if self_hwnd and hwnd == self_hwnd:
        return False
    if not w.user32.IsWindowVisible(hwnd):
        return False

    # Skip owned windows (tool windows, dialogs owned by a parent).
    GW_OWNER = 4
    if w.user32.GetWindow(hwnd, GW_OWNER):
        return False

    title = _window_title(hwnd)
    if not title.strip():
        return False

    GWL_EXSTYLE = -20
    WS_EX_TOOLWINDOW = 0x00000080
    WS_EX_APPWINDOW = 0x00040000
    ex = w.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    if ex & WS_EX_TOOLWINDOW and not (ex & WS_EX_APPWINDOW):
        return False

    # Cloaked UWP / invisible shells (Win10+)
    if _is_cloaked(hwnd):
        return False

    class_name = _class_name(hwnd)
    skip_classes = {
        "Progman",
        "WorkerW",
        "Shell_TrayWnd",
        "Shell_SecondaryTrayWnd",
        "NotifyIconOverflowWindow",
        "Windows.UI.Core.CoreWindow",
        "ApplicationFrameWindow",  # filtered further below if empty
    }
    # ApplicationFrameWindow without a useful child title often is a ghost UWP host.
    if class_name in skip_classes and class_name != "ApplicationFrameWindow":
        return False

    return True


def _is_cloaked(hwnd: int) -> bool:
    try:
        cloaked = wintypes.DWORD()
        DWMWA_CLOAKED = 14
        hr = w.dwmapi.DwmGetWindowAttribute(
            hwnd,
            DWMWA_CLOAKED,
            ctypes.byref(cloaked),
            ctypes.sizeof(cloaked),
        )
        return hr == 0 and cloaked.value != 0
    except Exception:
        return False


def _hicon_to_image(hicon: int, size: int = 96) -> Optional[Image.Image]:
    if not hicon:
        return None

    hdc = w.user32.GetDC(0)
    if not hdc:
        return None

    try:
        mem_dc = w.gdi32.CreateCompatibleDC(hdc)
        bmp = w.gdi32.CreateCompatibleBitmap(hdc, size, size)
        old = w.gdi32.SelectObject(mem_dc, bmp)

        # Clear to transparent-ish black then draw icon.
        brush = w.gdi32.CreateSolidBrush(0x000000)
        rect = w.RECT(0, 0, size, size)
        w.user32.FillRect(mem_dc, ctypes.byref(rect), brush)
        w.gdi32.DeleteObject(brush)

        w.user32.DrawIconEx(mem_dc, 0, 0, hicon, size, size, 0, None, 0x0003)

        bmi = w.BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(w.BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = size
        bmi.bmiHeader.biHeight = -size  # top-down
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = w.BI_RGB

        buf_len = size * size * 4
        buf = (ctypes.c_ubyte * buf_len)()
        w.gdi32.GetDIBits(mem_dc, bmp, 0, size, buf, ctypes.byref(bmi), w.DIB_RGB_COLORS)

        w.gdi32.SelectObject(mem_dc, old)
        w.gdi32.DeleteObject(bmp)
        w.gdi32.DeleteDC(mem_dc)

        img = Image.frombuffer("RGBA", (size, size), bytes(buf), "raw", "BGRA", 0, 1).copy()
        # Punch near-black drawn on opaque black canvas through to alpha.
        try:
            import numpy as np

            arr = np.array(img)
            mask = (arr[..., 0] < 8) & (arr[..., 1] < 8) & (arr[..., 2] < 8)
            arr[mask, 3] = 0
            return Image.fromarray(arr, "RGBA")
        except Exception:
            return img
    finally:
        w.user32.ReleaseDC(0, hdc)


def _extract_icon(hwnd: int) -> Optional[Image.Image]:
    # Prefer window icons, then class icons — pull at 96px for sharp wheel icons.
    for which in (w.ICON_BIG, w.ICON_SMALL2, w.ICON_SMALL):
        hicon = w.user32.SendMessageW(hwnd, w.WM_GETICON, which, 0)
        img = _hicon_to_image(hicon, 96)
        if img is not None:
            return img

    for index in (w.GCLP_HICON, w.GCLP_HICONSM):
        hicon = w.get_class_long(hwnd, index)
        img = _hicon_to_image(hicon, 96)
        if img is not None:
            return img

    # Fallback: generic rounded square
    img = Image.new("RGBA", (96, 96), (0, 0, 0, 0))
    from PIL import ImageDraw

    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle((8, 8, 88, 88), radius=18, fill=(220, 220, 220, 230))
    return img


def enumerate_windows(self_hwnd: int | None = None) -> list[AppWindow]:
    result: list[AppWindow] = []

    @w.WNDENUMPROC
    def enum_proc(hwnd, _lparam):
        if _is_alt_tab_candidate(hwnd, self_hwnd):
            pid = wintypes.DWORD()
            w.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            result.append(
                AppWindow(
                    hwnd=int(hwnd),
                    title=_window_title(hwnd),
                    class_name=_class_name(hwnd),
                    pid=int(pid.value),
                    icon=_extract_icon(hwnd),
                )
            )
        return True

    w.user32.EnumWindows(enum_proc, 0)
    return result


def activate_window(hwnd: int) -> None:
    """Bring a window to the foreground reliably from a background hook process."""
    if not w.user32.IsWindow(hwnd):
        return

    if w.user32.IsIconic(hwnd):
        w.user32.ShowWindow(hwnd, w.SW_RESTORE)

    fg = w.user32.GetForegroundWindow()
    target_tid = w.user32.GetWindowThreadProcessId(hwnd, None)
    fg_tid = w.user32.GetWindowThreadProcessId(fg, None) if fg else 0
    cur_tid = w.kernel32.GetCurrentThreadId()

    attached_fg = False
    attached_target = False
    try:
        if fg_tid and fg_tid != cur_tid:
            attached_fg = bool(w.user32.AttachThreadInput(cur_tid, fg_tid, True))
        if target_tid and target_tid != cur_tid and target_tid != fg_tid:
            attached_target = bool(w.user32.AttachThreadInput(cur_tid, target_tid, True))

        w.user32.BringWindowToTop(hwnd)
        w.user32.ShowWindow(hwnd, w.SW_SHOW)
        w.user32.SetForegroundWindow(hwnd)
        w.user32.SetActiveWindow(hwnd)
    finally:
        if attached_target:
            w.user32.AttachThreadInput(cur_tid, target_tid, False)
        if attached_fg:
            w.user32.AttachThreadInput(cur_tid, fg_tid, False)


def foreground_hwnd() -> int:
    return int(w.user32.GetForegroundWindow() or 0)


def _bits_to_image(hdc_mem, hbmp, width: int, height: int) -> Optional[Image.Image]:
    bmi = w.BITMAPINFO()
    bmi.bmiHeader.biSize = ctypes.sizeof(w.BITMAPINFOHEADER)
    bmi.bmiHeader.biWidth = width
    bmi.bmiHeader.biHeight = -height
    bmi.bmiHeader.biPlanes = 1
    bmi.bmiHeader.biBitCount = 32
    bmi.bmiHeader.biCompression = w.BI_RGB

    buf_len = width * height * 4
    buf = (ctypes.c_ubyte * buf_len)()
    got = w.gdi32.GetDIBits(
        hdc_mem, hbmp, 0, height, buf, ctypes.byref(bmi), w.DIB_RGB_COLORS
    )
    if not got:
        return None
    return Image.frombuffer(
        "RGBA", (width, height), bytes(buf), "raw", "BGRA", 0, 1
    ).convert("RGBA")


def _is_blank_capture(img: Image.Image) -> bool:
    """PrintWindow often 'succeeds' for minimized windows with a flat black/white frame."""
    try:
        import numpy as np

        small = img.resize((48, 48), Image.Resampling.BILINEAR)
        arr = np.array(small.convert("RGB"), dtype=np.int16)
        # Near-uniform = blank
        if arr.std() < 6:
            return True
        # Almost entirely black or white
        mean = float(arr.mean())
        if mean < 8 or mean > 247:
            return True
        return False
    except Exception:
        return False


def _print_window_to_image(hwnd: int, src_w: int, src_h: int) -> Optional[Image.Image]:
    hdc_screen = w.user32.GetDC(0)
    if not hdc_screen:
        return None
    try:
        hdc_mem = w.gdi32.CreateCompatibleDC(hdc_screen)
        full_bmp = w.gdi32.CreateCompatibleBitmap(hdc_screen, src_w, src_h)
        if not full_bmp:
            w.gdi32.DeleteDC(hdc_mem)
            return None
        old = w.gdi32.SelectObject(hdc_mem, full_bmp)

        ok = w.user32.PrintWindow(hwnd, hdc_mem, w.PW_RENDERFULLCONTENT)
        if not ok:
            ok = w.user32.PrintWindow(hwnd, hdc_mem, 0)

        img = _bits_to_image(hdc_mem, full_bmp, src_w, src_h) if ok else None

        w.gdi32.SelectObject(hdc_mem, old)
        w.gdi32.DeleteObject(full_bmp)
        w.gdi32.DeleteDC(hdc_mem)
        if img is not None and _is_blank_capture(img):
            return None
        return img
    finally:
        w.user32.ReleaseDC(0, hdc_screen)


def _bitblt_window_to_image(hwnd: int, rect: w.RECT) -> Optional[Image.Image]:
    src_w = rect.right - rect.left
    src_h = rect.bottom - rect.top
    hdc_screen = w.user32.GetDC(0)
    if not hdc_screen:
        return None
    try:
        hdc_mem = w.gdi32.CreateCompatibleDC(hdc_screen)
        full_bmp = w.gdi32.CreateCompatibleBitmap(hdc_screen, src_w, src_h)
        if not full_bmp:
            w.gdi32.DeleteDC(hdc_mem)
            return None
        old = w.gdi32.SelectObject(hdc_mem, full_bmp)
        SRCCOPY = 0x00CC0020
        ok = w.gdi32.BitBlt(
            hdc_mem, 0, 0, src_w, src_h, hdc_screen, rect.left, rect.top, SRCCOPY
        )
        img = _bits_to_image(hdc_mem, full_bmp, src_w, src_h) if ok else None
        w.gdi32.SelectObject(hdc_mem, old)
        w.gdi32.DeleteObject(full_bmp)
        w.gdi32.DeleteDC(hdc_mem)
        if img is not None and _is_blank_capture(img):
            return None
        return img
    finally:
        w.user32.ReleaseDC(0, hdc_screen)


def _capture_minimized(hwnd: int) -> Optional[Image.Image]:
    """
    Minimized windows don't paint — briefly restore off-screen (no focus steal),
    PrintWindow, then minimize again.
    """
    placement = w.WINDOWPLACEMENT()
    placement.length = ctypes.sizeof(w.WINDOWPLACEMENT)
    if not w.user32.GetWindowPlacement(hwnd, ctypes.byref(placement)):
        return None

    normal = placement.rcNormalPosition
    src_w = max(8, normal.right - normal.left)
    src_h = max(8, normal.bottom - normal.top)

    # Park off-screen so restore doesn't flash on the desktop.
    flags = (
        w.SWP_NOSIZE
        | w.SWP_NOZORDER
        | w.SWP_NOACTIVATE
        | w.SWP_SHOWWINDOW
        | w.SWP_NOSENDCHANGING
    )
    w.user32.SetWindowPos(hwnd, None, -32000, -32000, 0, 0, flags)
    w.user32.ShowWindow(hwnd, w.SW_SHOWNOACTIVATE)
    w.user32.MsgWaitForMultipleObjects(0, None, False, 40, 0)

    rect = w.RECT()
    w.user32.GetWindowRect(hwnd, ctypes.byref(rect))
    rw = rect.right - rect.left
    rh = rect.bottom - rect.top
    if rw < 8 or rh < 8:
        rw, rh = src_w, src_h

    # PrintWindow only — BitBlt of an off-screen window is empty/wrong.
    img = _print_window_to_image(hwnd, rw, rh)

    placement.length = ctypes.sizeof(w.WINDOWPLACEMENT)
    placement.showCmd = w.SW_SHOWMINIMIZED
    w.user32.SetWindowPlacement(hwnd, ctypes.byref(placement))
    w.user32.ShowWindow(hwnd, w.SW_SHOWMINNOACTIVE)

    return img


def capture_thumbnail(
    hwnd: int,
    max_width: int = 480,
    max_height: int = 270,
    *,
    allow_screen_grab: bool = False,
) -> Optional[Image.Image]:
    """Window preview for the switcher.

    Uses PrintWindow (the window's own pixels) — NOT a screen BitBlt — by
    default. Screen BitBlt looks sharp but captures whatever is on top of the
    window, including our Alt+Tab overlay (recursive preview bug).
    """
    if not hwnd or not w.user32.IsWindow(hwnd):
        return None

    iconic = bool(w.user32.IsIconic(hwnd))
    img: Optional[Image.Image] = None

    if iconic:
        img = _capture_minimized(hwnd)
    else:
        rect = w.RECT()
        if not w.user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return None
        src_w = rect.right - rect.left
        src_h = rect.bottom - rect.top
        if src_w < 8 or src_h < 8:
            return None

        # Always PrintWindow first — true window contents.
        img = _print_window_to_image(hwnd, src_w, src_h)
        # Screen grab only when the caller guarantees our overlay is hidden.
        if img is None and allow_screen_grab:
            img = _bitblt_window_to_image(hwnd, rect)

    if img is None:
        return None

    src_w, src_h = img.size
    capture_w = min(src_w, max(max_width * 2, max_width))
    capture_h = min(src_h, max(max_height * 2, max_height))
    fit = min(capture_w / src_w, capture_h / src_h, 1.0)
    mid_w = max(1, int(src_w * fit))
    mid_h = max(1, int(src_h * fit))

    if img.size != (mid_w, mid_h):
        img = img.resize((mid_w, mid_h), Image.Resampling.LANCZOS)

    out_scale = min(max_width / mid_w, max_height / mid_h, 1.0)
    if out_scale < 1.0:
        dst_w = max(1, int(mid_w * out_scale))
        dst_h = max(1, int(mid_h * out_scale))
        img = img.resize((dst_w, dst_h), Image.Resampling.LANCZOS)
    return img
