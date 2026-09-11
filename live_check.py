"""Drive the real app with real key events and screenshot the result.

Covers the whole chain that only exists when everything is connected: the
keyboard hook sees Alt and starts the backdrop capture, Tab opens the wheel
over it, and the backdrop window paints from a worker-captured plate.

Runs the app in this process rather than spawning it, for two reasons: a
full-screen grab taken from another process does not reliably show a plain
window's content, and the app hides the console it inherits — which would be
one of the windows we were hoping to switch to. Cancels with Escape, so
nothing is actually switched.
"""

from __future__ import annotations

import ctypes
import math
import threading
import time

from PIL import Image, ImageGrab

from fun_tab import win32_types as w

VK_MENU = 0x12
VK_TAB = 0x09
VK_ESCAPE = 0x1B
KEYEVENTF_KEYUP = 0x0002


def key(vk: int, up: bool = False) -> None:
    w.user32.keybd_event(vk, 0, KEYEVENTF_KEYUP if up else 0, 0)


def sharpness(img: Image.Image, box: tuple[int, int, int, int]) -> int:
    """Contrast in a patch of desktop; a blurred plate flattens it."""
    pixels = list(img.crop(box).convert("L").getdata())
    return max(pixels) - min(pixels)


def main() -> int:
    w.enable_dpi_awareness()
    from fun_tab.app import FunTabApp

    app = FunTabApp()
    print(f"config: backdrop={app.cfg.backdrop!r} dim_scale={app.cfg.dim_scale}")

    app.overlay.create()
    app.overlay.set_callbacks(on_commit=app.commit, on_cancel=app.cancel)
    app.tracker.install()
    app.hook.is_open = lambda: app.overlay.visible
    app.hook.is_sticky = lambda: app.overlay.sticky
    app.hook.has_query = lambda: bool(app.overlay.query)
    app._apply_hook_settings()
    app.hook.set_ui_thread(int(w.kernel32.GetCurrentThreadId()))
    app.hook.install()
    app._prewarm()

    results: dict[str, object] = {}

    def script() -> None:
        """Real keystrokes, from off the UI thread so the loop keeps running."""
        try:
            time.sleep(0.6)
            results["idle"] = ImageGrab.grab()

            key(VK_MENU)  # Alt down: the backdrop capture starts here
            time.sleep(0.10)  # the gap a human leaves before Tab
            key(VK_TAB)
            key(VK_TAB, up=True)
            time.sleep(0.40)  # let the open animation settle

            results["open"] = ImageGrab.grab()
            results["visible"] = app.overlay.visible
            results["apps"] = len(app.overlay.apps)
            results["plate"] = app.overlay._backdrop_plate is not None
            results["selected"] = app.overlay._selected

            # Aim with the pointer where it already is, and check the keyboard
            # still wins afterwards.
            point = w.POINT()
            w.user32.GetCursorPos(ctypes.byref(point))
            here = (int(point.x), int(point.y))
            w.user32.SetCursorPos(here[0], max(0, here[1] - 260))  # flick up
            time.sleep(0.12)
            results["after_flick"] = app.overlay._selected
            results["aimed"] = ImageGrab.grab()
            results["needle"] = app.overlay._aim_angle
            results["origin"] = app.overlay._origin_at
            results["origin_live"] = app.overlay._origin_live
            w.user32.SetCursorPos(*here)
            time.sleep(0.12)

            key(VK_ESCAPE)
            key(VK_ESCAPE, up=True)
            key(VK_MENU, up=True)
            time.sleep(0.20)
        finally:
            key(VK_MENU, up=True)  # never leave Alt stuck down
            app._running = False
            w.user32.PostThreadMessageW(
                int(w.kernel32.GetCurrentThreadId()), w.WM_NULL, 0, 0
            )

    driver = threading.Thread(target=script, name="live-check", daemon=True)
    driver.start()
    try:
        app._message_loop()
    finally:
        driver.join(5)
        app.hook.uninstall()
        app.tracker.uninstall()
        app.overlay.hide()
        app.overlay.destroy()

    if "open" not in results:
        print("FAIL: the script never got as far as opening the wheel")
        return 1

    idle: Image.Image = results["idle"]  # type: ignore[assignment]
    shot: Image.Image = results["open"]  # type: ignore[assignment]
    shot.save("live_open.png")
    shot.resize((shot.width // 2, shot.height // 2), Image.Resampling.LANCZOS).save(
        "live_open_small.png"
    )

    print(
        f"opened={results['visible']} apps={results['apps']} "
        f"plate={results['plate']} selected={results['selected']}"
    )
    print(f"aim origin at {results.get('origin')} live={results.get('origin_live')}")
    if not results["visible"]:
        print("FAIL: Alt+Tab did not open the wheel")
        return 1

    if int(results["apps"]) > 1:
        print(
            f"flick up from the cursor: slice {results['selected']} -> "
            f"{results['after_flick']}"
        )
    else:
        print("flick up: only one window open, so aiming had nothing to move to")

    aimed = results.get("aimed")
    if isinstance(aimed, Image.Image):
        aimed.save("live_aim.png")
        angle = results.get("needle")
        if angle is None:
            print("FAIL: flicking left no aim needle to draw")
            return 1
        print(f"aim needle at {math.degrees(float(angle)):.0f} deg (up is 90)")

    # A corner of desktop well away from the wheel and the preview card.
    corner = (1500, 60, shot.width - 20, 520)
    before = sharpness(idle, corner)
    after = sharpness(shot, corner)
    print(f"desktop contrast  idle={before}  with wheel open={after}")
    if after >= before * 0.85:
        print("FAIL: the desktop behind the wheel is not blurred")
        return 1
    print("PASS: backdrop is a blurred desktop")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
