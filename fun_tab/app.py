"""Application orchestration: hook + overlay + tray."""

from __future__ import annotations

import ctypes
import sys
import threading

from . import win32_types as w
from .hook import AltTabHook
from .overlay import Overlay
from .windows_enum import activate_window, enumerate_windows


def _hide_console() -> None:
    try:
        hwnd = w.kernel32.GetConsoleWindow()
        if hwnd:
            w.user32.ShowWindow(hwnd, w.SW_HIDE)
    except Exception:
        pass


class FunTabApp:
    def __init__(self) -> None:
        self.overlay = Overlay()
        self.hook = AltTabHook()
        self._lock = threading.Lock()
        self._tray = None
        self._running = True

    def start(self) -> None:
        self.overlay.create()
        self.overlay.set_callbacks(
            on_commit=self.commit,
            on_cancel=self.cancel,
        )

        self.hook.is_open = lambda: self.overlay.visible
        self.hook.set_ui_thread(int(w.kernel32.GetCurrentThreadId()))
        self.hook.install()

        self._start_tray()
        self._message_loop()

    def open_wheel(self) -> None:
        with self._lock:
            if self.overlay.visible:
                return
            apps = enumerate_windows(self_hwnd=self.overlay.hwnd)
            exclude = {
                getattr(self.overlay, "_dim_hwnd", 0),
                getattr(self.overlay, "_preview_hwnd", 0),
            }
            apps = [a for a in apps if a.hwnd not in exclude]
            if not apps:
                return
            selected = 1 if len(apps) > 1 else 0
            self.overlay.show(apps, selected=selected)

    def commit(self) -> None:
        with self._lock:
            if not self.overlay.visible:
                return
            apps = list(self.overlay.apps)
            idx = self.overlay.selected_index
            self.overlay.hide()
        if apps and 0 <= idx < len(apps):
            activate_window(apps[idx].hwnd)

    def cancel(self) -> None:
        with self._lock:
            if self.overlay.visible:
                self.overlay.hide()

    def shutdown(self) -> None:
        self._running = False
        self.cancel()
        self.hook.uninstall()
        if self.overlay.hwnd:
            w.user32.PostMessageW(self.overlay.hwnd, w.WM_CLOSE, 0, 0)
        w.user32.PostQuitMessage(0)

    def _start_tray(self) -> None:
        try:
            import pystray
            from PIL import Image, ImageDraw
        except ImportError:
            return

        icon_img = self._load_app_icon()
        if icon_img is None:
            icon_img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
            d = ImageDraw.Draw(icon_img)
            d.ellipse((4, 4, 60, 60), outline=(120, 180, 255, 255), width=4)
            d.ellipse((22, 22, 42, 42), fill=(120, 180, 255, 230))

        def on_quit(icon, _item):
            icon.stop()
            self.shutdown()

        menu = pystray.Menu(
            pystray.MenuItem("Fun Tab — Alt+Tab wheel", None, enabled=False),
            pystray.MenuItem("Quit", on_quit),
        )
        self._tray = pystray.Icon("fun_tab", icon_img, "Fun Tab", menu)
        threading.Thread(target=self._tray.run, daemon=True).start()

    @staticmethod
    def _load_app_icon():
        from pathlib import Path

        from PIL import Image

        root = Path(__file__).resolve().parent.parent
        for name in ("fun-tab-tray.png", "fun-tab-logo.png", "fun-tab.ico"):
            path = root / "assets" / name
            if path.exists():
                try:
                    return Image.open(path).convert("RGBA").resize(
                        (64, 64), Image.Resampling.LANCZOS
                    )
                except Exception:
                    continue
        return None

    def _apply_hook_actions(self) -> None:
        pending = self.hook.drain()
        if pending["cancel"]:
            self.cancel()
            return
        if pending["commit"]:
            self.commit()
            return
        if pending["open"]:
            self.open_wheel()
        if pending["cycle"]:
            if not self.overlay.visible and not pending["open"]:
                self.open_wheel()
            if self.overlay.visible:
                self.overlay.cycle(pending["cycle"])

    def _message_loop(self) -> None:
        msg = w.MSG()
        QS_ALLINPUT = 0x04FF
        while self._running:
            # Hook wakes us via PostThreadMessage — long idle wait is fine.
            timeout = 8 if self.overlay.visible else 500
            w.user32.MsgWaitForMultipleObjects(0, None, False, timeout, QS_ALLINPUT)
            self._apply_hook_actions()
            while w.user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
                if msg.message == w.WM_QUIT:
                    self._running = False
                    break
                w.user32.TranslateMessage(ctypes.byref(msg))
                w.user32.DispatchMessageW(ctypes.byref(msg))
            if self.overlay.visible:
                self.overlay.pump_idle()

        self.hook.uninstall()
        if self._tray is not None:
            try:
                self._tray.stop()
            except Exception:
                pass


def main() -> int:
    _hide_console()

    mutex = w.kernel32.CreateMutexW(None, False, "Local\\FunTabAltTabMutex")
    if w.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        return 1

    app = FunTabApp()
    try:
        app.start()
    except KeyboardInterrupt:
        app.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
