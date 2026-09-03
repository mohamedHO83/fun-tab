"""Application orchestration: keyboard hook + overlay + tray."""

from __future__ import annotations

import ctypes
import os
import sys
import threading

from . import autostart, hook as hook_actions
from . import win32_types as w
from .config import Config, config_path
from .hook import AltTabHook
from .mru import ForegroundTracker
from .overlay import Overlay
from .windows_enum import (
    activate_window,
    close_window,
    enumerate_windows,
    foreground_hwnd,
    minimize_window,
)


def _hide_console() -> None:
    try:
        hwnd = w.kernel32.GetConsoleWindow()
        if hwnd:
            w.user32.ShowWindow(hwnd, w.SW_HIDE)
    except Exception:
        pass


class FunTabApp:
    def __init__(self) -> None:
        self.cfg = Config.load()
        self.overlay = Overlay(self.cfg)
        self.hook = AltTabHook()
        self.tracker = ForegroundTracker()
        self._lock = threading.RLock()
        self._tray = None
        self._running = True
        self._reload_requested = False

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        self.overlay.create()
        self.overlay.set_callbacks(on_commit=self.commit, on_cancel=self.cancel)

        self.tracker.install()

        self.hook.is_open = lambda: self.overlay.visible
        self.hook.is_sticky = lambda: self.overlay.sticky
        self.hook.has_query = lambda: bool(self.overlay.query)
        self._apply_hook_settings()
        self.hook.set_ui_thread(int(w.kernel32.GetCurrentThreadId()))
        self.hook.install()

        self._prewarm()
        self._start_tray()
        self._message_loop()

    def shutdown(self) -> None:
        self._running = False
        self.cancel()
        self.hook.uninstall()
        self.tracker.uninstall()
        w.user32.PostQuitMessage(0)

    def _prewarm(self) -> None:
        """Fill the icon, executable and render caches before the user needs them."""
        try:
            apps = enumerate_windows(exclude_hwnds=self.overlay.own_hwnds())
            self.overlay.prewarm_render(apps)
        except Exception:
            pass

    def _apply_hook_settings(self) -> None:
        self.hook.search_enabled = self.cfg.search_enabled
        self.hook.digit_jump = self.cfg.digit_jump
        self.hook.close_key_enabled = self.cfg.close_key_enabled

    def reload_config(self) -> None:
        self.cfg = Config.load()
        self._apply_hook_settings()
        self.overlay.apply_config(self.cfg)

    # -- wheel -------------------------------------------------------------

    def open_wheel(
        self, *, sticky: bool = False, reverse: bool = False, same_app: bool = False
    ) -> None:
        with self._lock:
            if self.overlay.visible:
                return
            cfg = self.cfg
            apps = enumerate_windows(
                exclude_hwnds=self.overlay.own_hwnds(),
                exclude_exes=cfg.exclude_exes,
                exclude_titles=cfg.exclude_titles,
                rank=self.tracker.rank if cfg.mru_order else None,
                minimized_last=cfg.minimized_last,
            )
            if same_app and apps:
                apps = self._same_app_peers(apps)
            if not apps:
                return

            if reverse:
                selected = len(apps) - 1
            else:
                selected = 1 if len(apps) > 1 else 0
            self.overlay.show(apps, selected=selected, sticky=sticky)

    @staticmethod
    def _same_app_peers(apps: list) -> list:
        """Windows belonging to the focused app, that app's window first.

        The reference has to be the real foreground window: with MRU ordering
        off, ``apps[0]`` is whatever Windows enumerated first, which would make
        Alt+` cycle a random app's windows.
        """
        current = foreground_hwnd()
        reference = next((a for a in apps if a.hwnd == current), apps[0])
        key = reference.exe_path or reference.class_name
        peers = [a for a in apps if (a.exe_path or a.class_name) == key]
        if len(peers) < 2:
            return apps
        peers.sort(key=lambda a: a.hwnd != reference.hwnd)
        return peers

    def commit(self) -> None:
        with self._lock:
            if not self.overlay.visible:
                return
            app = self.overlay.selected_app()
            self.overlay.hide()
        if app is not None:
            activate_window(app.hwnd)
            self.tracker.note(app.hwnd)

    def cancel(self) -> None:
        with self._lock:
            if self.overlay.visible:
                self.overlay.hide()

    # -- hook actions ------------------------------------------------------

    def _apply_hook_actions(self) -> None:
        for action in self.hook.drain():
            self._dispatch(action)

    def _dispatch(self, action) -> None:
        kind = action.kind
        value = action.value
        overlay = self.overlay

        if kind == hook_actions.PREPARE:
            overlay.prepare_backdrop()
            return

        if kind == hook_actions.OPEN:
            options = value if isinstance(value, dict) else {}
            self.open_wheel(
                sticky=bool(options.get("sticky")),
                reverse=bool(options.get("reverse")),
                same_app=bool(options.get("same_app")),
            )
            return

        if not overlay.visible:
            return

        if kind == hook_actions.CYCLE:
            overlay.cycle(int(value))
        elif kind == hook_actions.COMMIT:
            self.commit()
        elif kind == hook_actions.CANCEL:
            self.cancel()
        elif kind == hook_actions.CLEAR:
            overlay.clear_query()
        elif kind == hook_actions.TYPE:
            overlay.type_query(str(value))
        elif kind == hook_actions.BACKSPACE:
            overlay.backspace_query()
        elif kind == hook_actions.FIRST:
            overlay.first()
        elif kind == hook_actions.LAST:
            overlay.last()
        elif kind == hook_actions.SAME_APP:
            overlay.cycle_same_app(int(value))
        elif kind == hook_actions.JUMP:
            if overlay.jump(int(value)):
                self.commit()
        elif kind == hook_actions.CLOSE:
            hwnd = overlay.request_close_selected()
            if hwnd:
                close_window(hwnd)
                self.tracker.forget(hwnd)
        elif kind == hook_actions.MINIMIZE:
            hwnd = overlay.request_close_selected()
            if hwnd:
                minimize_window(hwnd)

    # -- tray --------------------------------------------------------------

    def _start_tray(self) -> None:
        try:
            import pystray
            from PIL import Image, ImageDraw
        except ImportError:
            return

        icon_img = self._load_app_icon()
        if icon_img is None:
            icon_img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
            draw = ImageDraw.Draw(icon_img)
            draw.ellipse((4, 4, 60, 60), outline=(120, 180, 255, 255), width=5)
            draw.ellipse((23, 23, 41, 41), fill=(120, 180, 255, 235))

        def on_quit(icon, _item):
            icon.stop()
            self.shutdown()

        def on_toggle_autostart(_icon, _item):
            autostart.toggle()

        def on_open_settings(_icon, _item):
            path = config_path()
            if not path.exists():
                self.cfg.save(path)
            try:
                os.startfile(str(path))  # noqa: S606 - user-initiated
            except OSError:
                pass

        def on_reload(_icon, _item):
            self._reload_requested = True
            w.user32.PostThreadMessageW(
                int(w.kernel32.GetCurrentThreadId()), w.WM_NULL, 0, 0
            )

        menu = pystray.Menu(
            pystray.MenuItem("Fun Tab — Alt+Tab wheel", None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "Start with Windows",
                on_toggle_autostart,
                checked=lambda _item: autostart.is_enabled(),
            ),
            pystray.MenuItem("Edit settings…", on_open_settings),
            pystray.MenuItem("Reload settings", on_reload),
            pystray.Menu.SEPARATOR,
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

    # -- message loop ------------------------------------------------------

    def _message_loop(self) -> None:
        msg = w.MSG()
        QS_ALLINPUT = 0x04FF
        frame_ms = max(2, int(1000 / max(30, self.cfg.max_fps)))

        while self._running:
            # The hook wakes us with PostThreadMessage, so idling long is free.
            timeout = frame_ms if self.overlay.visible else 1000
            w.user32.MsgWaitForMultipleObjects(0, None, False, timeout, QS_ALLINPUT)

            if self._reload_requested:
                self._reload_requested = False
                self.reload_config()
                frame_ms = max(2, int(1000 / max(30, self.cfg.max_fps)))

            self._apply_hook_actions()

            while w.user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
                if msg.message == w.WM_QUIT:
                    self._running = False
                    break
                w.user32.TranslateMessage(ctypes.byref(msg))
                w.user32.DispatchMessageW(ctypes.byref(msg))

            if self.overlay.visible:
                if self.overlay.alt_released_while_open():
                    self.commit()
                else:
                    self.overlay.pump_idle()

        self.hook.uninstall()
        self.tracker.uninstall()
        self.overlay.destroy()
        if self._tray is not None:
            try:
                self._tray.stop()
            except Exception:
                pass


def main() -> int:
    w.enable_dpi_awareness()
    _hide_console()

    mutex = w.kernel32.CreateMutexW(None, False, "Local\\FunTabAltTabMutex")
    if w.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        print("Fun Tab is already running (see the tray icon).", file=sys.stderr)
        return 1

    app = FunTabApp()
    try:
        app.start()
    except KeyboardInterrupt:
        app.shutdown()
    finally:
        if mutex:
            w.kernel32.CloseHandle(mutex)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
