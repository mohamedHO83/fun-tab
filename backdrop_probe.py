"""Screenshot the backdrop and judge it on the pixels that reach the screen.

Two modes:

    python backdrop_probe.py          # each config backdrop, via the real overlay
    python backdrop_probe.py dwm      # the DWM blur APIs this deliberately avoids

The `dwm` matrix exists because both composition routes return success on a
full-screen topmost window and then composite a flat tint with no trace of the
windows behind them. That is where the black backdrop came from, and it is
worth being able to re-check on a new Windows build rather than taking it on
faith.
"""

from __future__ import annotations

import ctypes
import sys
import time
from ctypes import wintypes

from PIL import Image, ImageGrab

from fun_tab import win32_types as w
from fun_tab.config import Config
from fun_tab.gdi import LayeredSurface
from fun_tab.overlay import Overlay
from fun_tab.preview import _demo_apps
from fun_tab.windows_enum import enumerate_windows


def judge(shot: Image.Image, box: tuple[int, int, int, int]) -> tuple[int, int, int]:
    pixels = list(shot.crop(box).convert("RGB").getdata())
    avg = sum(sum(p) for p in pixels) // (len(pixels) * 3)
    return avg, min(min(p) for p in pixels), max(max(p) for p in pixels)


def report(name: str, shot: Image.Image, box, extra: str = "") -> None:
    avg, lo, hi = judge(shot, box)
    verdict = "FLAT TINT" if hi - lo < 20 else "shows desktop"
    print(f"{name:30} avg={avg:3d} range={lo:3d}-{hi:3d}  {verdict:14} {extra}")


# -- the real thing ---------------------------------------------------------


def pump(overlay: Overlay, seconds: float) -> None:
    """Run the frame loop the way app.py does, so animations finish."""
    end = time.perf_counter() + seconds
    msg = w.MSG()
    while time.perf_counter() < end:
        while w.user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):  # PM_REMOVE
            w.user32.TranslateMessage(ctypes.byref(msg))
            w.user32.DispatchMessageW(ctypes.byref(msg))
        overlay.pump_idle()
        time.sleep(0.004)


def probe_mode(mode: str) -> None:
    overlay = Overlay(Config(backdrop=mode))
    overlay.create()
    try:
        apps = enumerate_windows(exclude_hwnds=overlay.own_hwnds())
        if len(apps) < 4:
            apps = (apps + _demo_apps(6))[:6]
        overlay.prewarm_render(apps)

        start = time.perf_counter()
        overlay.prepare_backdrop()  # what the hook does when Alt goes down
        prepared = (time.perf_counter() - start) * 1000
        time.sleep(0.12)  # the gap before Tab

        start = time.perf_counter()
        overlay.show(apps, selected=1)
        opened = (time.perf_counter() - start) * 1000

        pump(overlay, 0.45)
        shot = ImageGrab.grab()
        shot.save(f"probe_{mode}.png")
        report(
            mode,
            shot,
            (40, 40, 560, 400),
            f"alt-down={prepared:5.2f}ms open={opened:6.2f}ms",
        )
    finally:
        overlay.hide()
        overlay.destroy()


# -- the DWM APIs this avoids ----------------------------------------------

dwmapi = ctypes.windll.dwmapi
DWMWA_USE_IMMERSIVE_DARK_MODE = 20
DWMWA_SYSTEMBACKDROP_TYPE = 38
DWMSBT_MAINWINDOW = 2  # Mica
DWMSBT_TRANSIENTWINDOW = 3  # Acrylic


class MARGINS(ctypes.Structure):
    _fields_ = [
        ("cxLeftWidth", ctypes.c_int),
        ("cxRightWidth", ctypes.c_int),
        ("cyTopHeight", ctypes.c_int),
        ("cyBottomHeight", ctypes.c_int),
    ]


dwmapi.DwmSetWindowAttribute.argtypes = [
    wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD
]
dwmapi.DwmExtendFrameIntoClientArea.argtypes = [wintypes.HWND, ctypes.POINTER(MARGINS)]

PROBE_CLASS = "FunTabBackdropProbe"
_procs: list = []
_registered = False


def screen() -> tuple[int, int]:
    return int(w.user32.GetSystemMetrics(0)), int(w.user32.GetSystemMetrics(1))


def make_window(layered: bool) -> int:
    global _registered
    if not _registered:
        @w.WNDPROC
        def proc(hwnd, msg, wparam, lparam):
            if msg == w.WM_ERASEBKGND:
                return 1
            return w.user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        _procs.append(proc)
        wc = w.WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(w.WNDCLASSEXW)
        wc.lpfnWndProc = proc
        wc.hInstance = w.kernel32.GetModuleHandleW(None)
        wc.lpszClassName = PROBE_CLASS
        w.user32.RegisterClassExW(ctypes.byref(wc))
        _registered = True

    ex = w.WS_EX_TOPMOST | w.WS_EX_TOOLWINDOW | w.WS_EX_NOACTIVATE
    if layered:
        ex |= w.WS_EX_LAYERED
    return int(
        w.user32.CreateWindowExW(
            ex, PROBE_CLASS, "probe", w.WS_POPUP, 0, 0, 16, 16,
            None, None, w.kernel32.GetModuleHandleW(None), None,
        )
        or 0
    )


def run_dwm_variant(name: str, setup, *, layered: bool = False, inset: int = 0) -> None:
    hwnd = make_window(layered)
    surface = LayeredSurface(hwnd) if layered else None
    try:
        setup(hwnd)
        width, height = screen()
        w.user32.SetWindowPos(
            hwnd, w.HWND_TOPMOST,
            inset, inset, width - inset * 2, height - inset * 2,
            w.SWP_SHOWWINDOW | w.SWP_NOACTIVATE,
        )
        if surface is not None and surface.ensure(width - inset * 2, height - inset * 2):
            surface.view[...] = 0  # fully transparent: let the blur show
            surface.present(inset, inset)

        time.sleep(0.7)  # let DWM settle
        shot = ImageGrab.grab()
        shot.save(f"probe_{name}.png")
        # Sampled well inside the window, so uncovered desktop cannot flatter it.
        report(name, shot, (inset + 240, inset + 200, width - inset - 240, height - inset - 200))
    finally:
        if surface is not None:
            surface.destroy()
        w.user32.DestroyWindow(hwnd)


def system_backdrop(kind: int, extend: bool):
    def setup(hwnd: int) -> None:
        dark = ctypes.c_int(1)
        dwmapi.DwmSetWindowAttribute(hwnd, DWMWA_USE_IMMERSIVE_DARK_MODE, ctypes.byref(dark), 4)
        value = ctypes.c_int(kind)
        dwmapi.DwmSetWindowAttribute(hwnd, DWMWA_SYSTEMBACKDROP_TYPE, ctypes.byref(value), 4)
        if extend:
            margins = MARGINS(-1, -1, -1, -1)
            dwmapi.DwmExtendFrameIntoClientArea(hwnd, ctypes.byref(margins))

    return setup


def accent_policy(state: int, grad_alpha: int):
    def setup(hwnd: int) -> None:
        if not w.SetWindowCompositionAttribute:
            return
        accent = w.ACCENTPOLICY(state, 2, (grad_alpha << 24) & 0xFFFFFFFF, 0)
        data = w.WINDOWCOMPOSITIONATTRIBDATA(
            w.WCA_ACCENT_POLICY,
            ctypes.cast(ctypes.pointer(accent), ctypes.c_void_p),
            ctypes.sizeof(accent),
        )
        w.SetWindowCompositionAttribute(hwnd, ctypes.byref(data))

    return setup


def probe_dwm() -> None:
    print("-- Windows 11 system backdrop --")
    run_dwm_variant("sbt_acrylic", system_backdrop(DWMSBT_TRANSIENTWINDOW, True))
    run_dwm_variant("sbt_acrylic_layered", system_backdrop(DWMSBT_TRANSIENTWINDOW, True), layered=True)
    run_dwm_variant("sbt_mica", system_backdrop(DWMSBT_MAINWINDOW, True))
    run_dwm_variant("sbt_acrylic_inset", system_backdrop(DWMSBT_TRANSIENTWINDOW, True), inset=200)

    print("\n-- legacy accent policy --")
    run_dwm_variant("accent_acrylic", accent_policy(w.ACCENT_ENABLE_ACRYLICBLURBEHIND, 160))
    run_dwm_variant("accent_blur", accent_policy(w.ACCENT_ENABLE_BLURBEHIND, 0))
    run_dwm_variant(
        "accent_acrylic_layered",
        accent_policy(w.ACCENT_ENABLE_ACRYLICBLURBEHIND, 160),
        layered=True,
    )


def main() -> int:
    w.enable_dpi_awareness()
    args = sys.argv[1:]
    if args and args[0] == "dwm":
        probe_dwm()
        return 0
    for mode in args or ["blur", "dim", "none"]:
        probe_mode(mode)
        time.sleep(0.3)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
