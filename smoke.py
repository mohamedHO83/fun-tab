"""End-to-end smoke test: really create the layered windows and present frames.

Briefly flashes the overlay on screen, then tears everything down and prints
timings for the paths that can only be measured against live Win32.
"""

from __future__ import annotations

import ctypes
import math
import statistics
import time

from fun_tab import win32_types as w
from fun_tab.config import Config
from fun_tab.overlay import Overlay
from fun_tab.preview import _demo_apps
from fun_tab.windows_enum import enumerate_windows


def main() -> int:
    w.enable_dpi_awareness()

    cfg = Config()
    overlay = Overlay(cfg)
    overlay.create()
    print(f"windows created: dim={overlay._dim_hwnd} wheel={overlay.hwnd} preview={overlay._preview_hwnd}")

    apps = enumerate_windows(exclude_hwnds=overlay.own_hwnds())
    if len(apps) < 3:
        apps = (apps + _demo_apps(6))[:6]
    print(f"apps: {len(apps)} -> {[a.display_name[:22] for a in apps]}")

    monitor_left, monitor_top, monitor_right, monitor_bottom = 0, 0, 0, 0

    try:
        start = time.perf_counter()
        overlay.prewarm_render(apps)
        print(f"prewarm_render          {(time.perf_counter() - start) * 1000:7.2f} ms")

        monitor_left, monitor_top, monitor_right, monitor_bottom = overlay._monitor
        print(
            f"monitor {monitor_right - monitor_left}x{monitor_bottom - monitor_top} "
            f"@ {overlay._dpi} dpi, canvas {overlay.canvas_w}x{overlay.canvas_h}"
        )

        # Backdrop, split the way the app pays for it: the capture happens on
        # a worker while Alt is held, and the open only resolves and blits.
        width = monitor_right - monitor_left
        height = monitor_bottom - monitor_top

        samples = []
        for _ in range(5):
            with overlay._plate_lock:
                overlay._plate = None
            start = time.perf_counter()
            overlay._capture_plate(overlay._monitor)
            samples.append((time.perf_counter() - start) * 1000)
        print(f"backdrop capture+blur (off-thread) {statistics.median(samples):7.2f} ms")

        samples = []
        for _ in range(8):
            start = time.perf_counter()
            overlay._present_backdrop(monitor_left, monitor_top, width, height)
            samples.append((time.perf_counter() - start) * 1000)
        print(f"backdrop resolve (at open) {statistics.median(samples):7.2f} ms")

        # Full open.
        start = time.perf_counter()
        overlay.show(apps, selected=1)
        open_ms = (time.perf_counter() - start) * 1000
        print(f"overlay.show (full open) {open_ms:7.2f} ms")

        # Frames while cycling: a transition repaints, a held selection should
        # cost nothing but a re-present of the bitmap already in the DIB.
        moved, held = [], []
        for index in range(min(len(apps), 8)):
            overlay.select(index)
            start = time.perf_counter()
            overlay._render(time.perf_counter())
            moved.append((time.perf_counter() - start) * 1000)

            # Let the crossfade finish, then settle on the target frame before
            # timing a held one - otherwise this just measures a second
            # transition frame.
            time.sleep(cfg.transition_duration + 0.02)
            overlay._render(time.perf_counter())
            start = time.perf_counter()
            overlay._render(time.perf_counter())
            held.append((time.perf_counter() - start) * 1000)
        print(f"transition frame         {statistics.median(moved):7.2f} ms")
        print(f"held frame (re-present)  {statistics.median(held):7.2f} ms")

        time.sleep(0.4)

        start = time.perf_counter()
        overlay.hide()
        print(f"overlay.hide             {(time.perf_counter() - start) * 1000:7.2f} ms")

        # Re-open (caches hot) — the number that matters day to day.
        reopen = []
        for _ in range(5):
            start = time.perf_counter()
            overlay.show(apps, selected=1)
            reopen.append((time.perf_counter() - start) * 1000)
            overlay.hide()
            time.sleep(0.05)
        print(f"re-open (hot caches)     {statistics.median(reopen):7.2f} ms")
    finally:
        overlay.hide()
        overlay.destroy()

    check_aiming(apps)
    check_app_wiring()
    print("OK")
    return 0


def check_aiming(apps) -> None:
    """Aim the real wheel by really moving the cursor.

    The unit tests feed `_cursor_at` directly, which skips the part that
    actually goes wrong in practice: screen-to-canvas origins, DPI scaling and
    the poll in the frame loop. This drives GetCursorPos for real, from a
    corner of the screen rather than the middle, and puts the cursor back.
    """
    overlay = Overlay(Config(preview_enabled=False))
    overlay.create()
    saved = w.POINT()
    w.user32.GetCursorPos(ctypes.byref(saved))
    try:
        overlay.prewarm_render(apps)
        left, top, right, bottom = overlay._monitor

        # Park the cursor in the bottom-right corner and open there.
        corner = (right - 60, bottom - 60)
        w.user32.SetCursorPos(*corner)
        time.sleep(0.05)
        overlay.show(apps, selected=1)

        results = []
        for label, (dx, dy) in {
            "up": (0, -260),
            "right": (260, 0),
            "down": (0, 260),
            "left": (-260, 0),
        }.items():
            w.user32.SetCursorPos(*corner)
            overlay._poll_cursor()  # re-establish the origin after each gesture
            w.user32.SetCursorPos(corner[0] + dx, corner[1] + dy)
            time.sleep(0.02)
            overlay._poll_cursor()
            results.append(f"{label}={overlay._selected}")

        picked = {r.split("=")[1] for r in results}
        print(f"aim from screen corner   {' '.join(results)}")
        # With three windows on screen a quarter-turn apart, two of the four
        # directions share a slice by geometry, not by getting it wrong.
        wanted = min(4, len(apps))
        if len(picked) < wanted:
            raise SystemExit(
                f"flicks did not resolve to {wanted} distinct slices: {results}"
            )

        # The needle should be up after a flick, and gone the moment a key is
        # used — otherwise it would point somewhere the selection is not.
        if overlay._aim_angle is None:
            raise SystemExit("no aim needle after flicking")

        # Swinging the aim inside one slice repaints only the needle, so this
        # is the true cost of tracking the cursor with no selection change.
        # Straight up is the middle of slice 0, and a quarter of a slice
        # either side of it stays well clear of the boundaries.
        swing = (math.pi * 2 / len(overlay.apps)) / 4

        def aim_at(offset: float) -> None:
            angle = math.pi / 2 + offset
            w.user32.SetCursorPos(
                corner[0] + int(260 * math.cos(angle)),
                corner[1] - int(260 * math.sin(angle)),
            )
            time.sleep(0.004)

        aim_at(0.0)
        overlay._poll_cursor()
        time.sleep(overlay.cfg.transition_duration + 0.02)  # let the change settle
        overlay._render(time.perf_counter())
        held = overlay._selected

        aimed = []
        for step in range(24):
            aim_at(swing * ((step % 8) - 4) / 4)
            start = time.perf_counter()
            overlay._poll_cursor()
            overlay._render(time.perf_counter())
            aimed.append((time.perf_counter() - start) * 1000)
        if overlay._selected != held:
            raise SystemExit("aim sweep left its slice; the timing is not needle-only")
        print(f"aim frame (needle only)  {statistics.median(aimed):7.2f} ms")

        # And the reported bug: a still cursor must not undo a keyboard pick.
        overlay.cycle(1)
        chosen = overlay._selected
        if overlay._aim_angle is not None:
            raise SystemExit("needle survived a keypress")
        for _ in range(12):
            overlay._poll_cursor()
        if overlay._selected != chosen:
            raise SystemExit(
                f"stationary cursor overrode the keyboard: {chosen} -> {overlay._selected}"
            )
        print(f"keyboard holds vs still cursor  slice {chosen} after 12 frames")
    finally:
        overlay.hide()
        overlay.destroy()
        w.user32.SetCursorPos(int(saved.x), int(saved.y))


def check_app_wiring() -> None:
    """Drive the real app the way the keyboard hook does.

    Everything above tests the overlay in isolation; this covers the wiring
    that only exists once the hook, the foreground tracker and the window
    enumeration are all connected. The hook is installed for well under a
    second, and Alt+Tab is only intercepted while it is.
    """
    from fun_tab.app import FunTabApp
    from fun_tab.hook import OPEN, Action

    app = FunTabApp()
    app.overlay.create()
    app.overlay.set_callbacks(on_commit=app.commit, on_cancel=app.cancel)
    app.tracker.install()
    app.hook.is_open = lambda: app.overlay.visible
    app.hook.is_sticky = lambda: app.overlay.sticky
    app.hook.has_query = lambda: bool(app.overlay.query)
    app._apply_hook_settings()
    app.hook.set_ui_thread(int(w.kernel32.GetCurrentThreadId()))
    app.hook.install()
    try:
        app._prewarm()

        start = time.perf_counter()
        app._dispatch(Action(OPEN, {"sticky": True}))
        opened_ms = (time.perf_counter() - start) * 1000
        if not app.overlay.visible:
            raise SystemExit("open_wheel did not show the overlay")
        print(
            f"app open (enumerate+show){opened_ms:7.2f} ms"
            f"   [{len(app.overlay.apps)} windows, MRU first: "
            f"{app.overlay.apps[0].display_name[:24]!r}]"
        )

        app.overlay.cycle(1)
        app.overlay.type_query("e")
        app.overlay.clear_query()
        app.cancel()
        if app.overlay.visible:
            raise SystemExit("cancel did not hide the overlay")
    finally:
        app.hook.uninstall()
        app.tracker.uninstall()
        app.overlay.destroy()


if __name__ == "__main__":
    raise SystemExit(main())
