"""Application orchestration: keyboard hook + overlay + tray."""

from __future__ import annotations

import ctypes
import hashlib
import sys
import threading
import time

from . import hook as hook_actions
from . import win32_types as w
from .config import Config, config_path
from .privacy import CONSENT_TEXT, CONSENT_VERSION
from .hook import AltTabHook
from .mru import ForegroundTracker
from .overlay import Overlay
from .windows_enum import (
    activate_window,
    close_window,
    enumerate_windows,
    foreground_hwnd,
    minimize_window,
    present_windows,
)


def _config_stamp(path=None) -> tuple:
    """A fingerprint of the settings file, or `()` if it isn't there.

    Hashes the contents rather than trusting the timestamp: Windows only moves
    file times on about a 16ms tick, so two quick saves of a same-sized file
    can share one and the second edit would never be noticed. It also means
    re-saving without changing anything costs nothing.
    """
    try:
        data = (path or config_path()).read_bytes()
    except OSError:
        return ()
    return (len(data), hashlib.blake2b(data, digest_size=16).digest())


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
        self._settings_proc = None
        self._config_stamp = _config_stamp()
        # After leaving a game, stay out of the way until Alt is fully released
        # (and a short beat after). Otherwise Windows Alt+Tab is still in
        # progress when we re-arm and the next Tab opens Fun Tab by mistake.
        self._compat_latched = False
        self._paused_for_game = False
        self._resume_hooks_at = 0.0
        # After the user confirms one close this session, skip further prompts.
        self._close_confirmed_session = False
        self._paused_manually = False
        self._menu_open = False

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        self.overlay.create()
        self.overlay.set_callbacks(
            on_commit=self.commit, on_cancel=self.cancel, on_context=self._slice_menu
        )

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
        self.hook.set_open_hotkey(self.cfg.open_hotkey)
        self.hook.open_sticky = bool(self.cfg.open_sticky)
        self._refresh_compat()

    def _alt_held(self) -> bool:
        return bool(w.user32.GetAsyncKeyState(w.VK_MENU) & 0x8000)

    def _refresh_compat(self) -> None:
        """Turn game-compat on or off, and optionally pause hooks in games.

        Leaving a game mid Alt+Tab must not re-arm Fun Tab on the next Tab:
        Windows still owns that gesture until Alt comes up.
        """
        in_game = False
        in_switcher = False
        if not self.overlay.visible:
            from .game_detect import foreground_is_task_switcher, foreground_looks_like_a_game

            try:
                in_game = foreground_looks_like_a_game(self.cfg.game_exes)
            except Exception:
                in_game = False
            try:
                in_switcher = foreground_is_task_switcher()
            except Exception:
                in_switcher = False

        leaving_game = self._paused_for_game or self._compat_latched
        transitional = in_switcher or (leaving_game and self._alt_held())

        mode = self.cfg.game_compat
        if mode == "always":
            active = True
            self._compat_latched = False
        elif mode == "off":
            active = False
            self._compat_latched = False
        elif self.overlay.visible:
            active = self.hook.compat_active
        elif in_game:
            active = True
            self._compat_latched = True
        elif transitional:
            # Windows Alt+Tab (or Alt still down after leaving the game).
            active = True
        else:
            active = False
            self._compat_latched = False

        self.hook.compat_active = active
        self.overlay.compat_active = active

        if self._paused_manually:
            if self.hook.installed:
                self.cancel()
                self.hook.uninstall()
            return

        want_pause = bool(self.cfg.pause_in_games) and not self.overlay.visible
        if want_pause and (in_game or transitional):
            if self.hook.installed:
                self.cancel()
                self.hook.uninstall()
            if in_game:
                self._paused_for_game = True
            # Stay paused through the Windows switcher / Alt-hold; resume later.
            self._resume_hooks_at = 0.0
            return

        if self._paused_for_game and not in_game and not transitional:
            # Just cleared the game + Alt-up + switcher gone: brief grace so a
            # quick second Alt+Tab does not race the reinstall.
            if self._resume_hooks_at <= 0.0:
                self._resume_hooks_at = time.perf_counter() + 0.8
            if time.perf_counter() < self._resume_hooks_at:
                return
            self._paused_for_game = False
            self._resume_hooks_at = 0.0

        if want_pause and self._paused_for_game:
            return  # still waiting on grace / Alt

        if not self.hook.installed and not (want_pause and in_game):
            try:
                self.hook.install()
            except OSError:
                pass
            self._paused_for_game = False
            self._resume_hooks_at = 0.0

    def reload_config(self) -> None:
        self.cfg = Config.load()
        self._apply_hook_settings()
        self.overlay.apply_config(self.cfg)
        # Loading rewrites the file if it was unreadable, so re-stamp after
        # rather than before, or that write would look like another edit.
        self._config_stamp = _config_stamp()

    def _config_file_edited(self) -> bool:
        """Whether config.json has changed since we last looked.

        Saving from the settings window is just a file write, so watching the
        file is all the coupling the two processes need — and it means a
        hand-edit applies on its own too.
        """
        stamp = _config_stamp()
        if stamp == self._config_stamp:
            return False
        self._config_stamp = stamp
        return True

    def open_settings_ui(self) -> None:
        """Launch the settings window as its own process.

        Out of process because Tk wants to own a thread's message loop and
        ours belongs to the keyboard hook.
        """
        import subprocess

        from .paths import install_dir, settings_command
        from .settings_ui import WINDOW_TITLE

        existing = w.user32.FindWindowW(None, WINDOW_TITLE)
        if existing:
            w.user32.SetForegroundWindow(existing)
            return
        if self._settings_proc is not None and self._settings_proc.poll() is None:
            return  # starting up, window not there yet

        try:
            self._settings_proc = subprocess.Popen(
                settings_command(),
                cwd=str(install_dir()),
                creationflags=0x08000000,  # CREATE_NO_WINDOW
            )
        except OSError:
            self._settings_proc = None

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
            shown = present_windows(
                apps,
                group_by_app=cfg.group_by_app,
                pinned_exes=cfg.pinned_exes,
                expand_app=same_app,
            )
            if not shown:
                return

            if reverse:
                selected = len(shown) - 1
            else:
                selected = 1 if len(shown) > 1 else 0
            self.overlay.show(
                apps, selected=selected, sticky=sticky, expand_app=same_app
            )

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
            self._close_selected()
        elif kind == hook_actions.MINIMIZE:
            hwnd = overlay.request_close_selected()
            if hwnd:
                minimize_window(hwnd)
        elif kind == hook_actions.HIDE:
            self._hide_selected_app()
        elif kind == hook_actions.PIN:
            self._toggle_pin_selected()

    def _close_selected(self) -> None:
        app = self.overlay.selected_app()
        if app is None:
            return
        if self.cfg.close_confirm and not self._close_confirmed_session:
            title = app.label(title_privacy=self.cfg.title_privacy)
            question = (
                f"Close “{title}”?\n\n"
                "Further closes this session will not ask again."
            )
            flags = w.MB_YESNO | w.MB_ICONQUESTION
            if w.message_box(question, flags=flags) != w.IDYES:
                return
            self._close_confirmed_session = True
        hwnd = self.overlay.request_close_selected()
        if hwnd:
            if close_window(hwnd):
                self.tracker.forget(hwnd)
            else:
                w.message_box(
                    "That window belongs to an elevated app, so Fun Tab "
                    "cannot close it.",
                    flags=w.MB_OK | w.MB_ICONWARNING,
                )

    def _persist_config(self) -> None:
        self.cfg.save()
        self._config_stamp = _config_stamp()

    def _hide_selected_app(self) -> None:
        app = self.overlay.selected_app()
        if app is None or not app.exe_name:
            return
        name = app.exe_name.lower()
        if name not in self.cfg.exclude_exes:
            self.cfg.exclude_exes = list(self.cfg.exclude_exes) + [name]
            self.cfg.clamp()
            self._persist_config()
        self.overlay.drop_exe(name)

    def _toggle_pin_selected(self) -> None:
        app = self.overlay.selected_app()
        if app is None or not app.exe_name:
            return
        name = app.exe_name.lower()
        pinned = list(self.cfg.pinned_exes)
        if name in pinned:
            pinned = [item for item in pinned if item != name]
        else:
            pinned.append(name)
        self.cfg.pinned_exes = pinned
        self.cfg.clamp()
        self._persist_config()
        hwnd = app.hwnd
        if not self.overlay._query:
            self.overlay._refresh_visible(keep_hwnd=hwnd)

    def _slice_menu(self) -> None:
        """Right-click on a slice: hide, pin, close, minimise."""
        app = self.overlay.selected_app()
        if app is None or not self.overlay.hwnd:
            return
        menu = w.user32.CreatePopupMenu()
        if not menu:
            return
        hide_id, pin_id, close_id, min_id, dismiss_id = 1, 2, 3, 4, 5
        pinned = app.exe_name.lower() in self.cfg.pinned_exes
        pin_label = "Unpin this app" if pinned else "Pin this app"
        self._menu_open = True
        try:
            w.user32.AppendMenuW(menu, w.MF_STRING, hide_id, "Hide this app")
            w.user32.AppendMenuW(menu, w.MF_STRING, pin_id, pin_label)
            w.user32.AppendMenuW(menu, w.MF_SEPARATOR, 0, None)
            w.user32.AppendMenuW(menu, w.MF_STRING, close_id, "Close window")
            w.user32.AppendMenuW(menu, w.MF_STRING, min_id, "Minimise window")
            w.user32.AppendMenuW(menu, w.MF_SEPARATOR, 0, None)
            w.user32.AppendMenuW(menu, w.MF_STRING, dismiss_id, "Cancel")
            point = w.POINT()
            w.user32.GetCursorPos(ctypes.byref(point))
            w.user32.SetForegroundWindow(self.overlay.hwnd)
            choice = int(
                w.user32.TrackPopupMenu(
                    menu,
                    w.TPM_RIGHTBUTTON | w.TPM_RETURNCMD,
                    point.x,
                    point.y,
                    0,
                    self.overlay.hwnd,
                    None,
                )
            )
            w.user32.PostMessageW(self.overlay.hwnd, w.WM_NULL, 0, 0)
        finally:
            w.user32.DestroyMenu(menu)
            self._menu_open = False
        if choice == hide_id:
            self._hide_selected_app()
        elif choice == pin_id:
            self._toggle_pin_selected()
        elif choice == close_id:
            self._close_selected()
        elif choice == min_id:
            hwnd = self.overlay.request_close_selected()
            if hwnd:
                minimize_window(hwnd)
        elif choice == dismiss_id:
            self.cancel()

    def toggle_pause(self) -> None:
        self.set_paused(not self._paused_manually)

    def set_paused(self, paused: bool) -> None:
        self._paused_manually = bool(paused)
        if self._paused_manually:
            self.cancel()
            if self.hook.installed:
                self.hook.uninstall()
        else:
            self._refresh_compat()
        if self._tray is not None:
            self._tray.set_paused(self._paused_manually)

    def show_about(self) -> None:
        from . import __version__

        w.message_box(
            f"Fun Tab {__version__}\n\n"
            "A GTA-style radial Alt+Tab for Windows.\n\n"
            "Hold your open shortcut, flick the mouse, let go.",
            flags=w.MB_OK | w.MB_ICONINFORMATION,
        )

    # -- tray --------------------------------------------------------------

    def _start_tray(self) -> None:
        from .tray import TrayIcon

        tray = TrayIcon(self)
        if not tray.install():
            tray.uninstall()
            w.message_box(
                "Fun Tab is running, but the tray icon could not be shown.\n\n"
                "Run Fun Tab again if you want to quit it."
            )
            return
        self._tray = tray

    # -- message loop ------------------------------------------------------

    def _message_loop(self) -> None:
        msg = w.MSG()
        QS_ALLINPUT = 0x04FF
        frame_ms = max(2, int(1000 / max(30, self.cfg.max_fps)))

        while self._running:
            # The hook wakes us with PostThreadMessage, so idling long is free.
            timeout = frame_ms if self.overlay.visible else 1000
            w.user32.MsgWaitForMultipleObjects(0, None, False, timeout, QS_ALLINPUT)

            # Saving in the settings window shows up here, without the user
            # having to know that "reload" is a thing.
            if self._reload_requested or (
                not self.overlay.visible and self._config_file_edited()
            ):
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
                # Commit when the open hold is gone (Alt, modifiers, mouse, …).
                # Checking Alt alone used to kill custom chords like Ctrl+Shift+U
                # the moment they opened, because Alt was never held.
                if (
                    not self.overlay.sticky
                    and not self._menu_open
                    and time.perf_counter() - self.overlay._shown_at >= 0.25
                    and self.hook.hold_released()
                ):
                    self.commit()
                else:
                    self.overlay.pump_idle()
            else:
                self._refresh_compat()

        self.hook.uninstall()
        self.tracker.uninstall()
        self.overlay.destroy()
        if self._tray is not None:
            try:
                self._tray.uninstall()
            except Exception:
                pass


def main() -> int:
    if sys.argv[1:2] == ["--settings"]:
        sys.argv = [sys.argv[0], *sys.argv[2:]]
        from .settings_ui import main as settings_main

        return settings_main()

    w.enable_dpi_awareness()
    _hide_console()

    mutex = w.kernel32.CreateMutexW(None, False, "Local\\FunTabAltTabMutex")
    if w.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        from .tray import offer_to_quit_running

        return offer_to_quit_running()

    if not _ensure_consent():
        if mutex:
            w.kernel32.CloseHandle(mutex)
        return 0

    app = FunTabApp()
    try:
        app.start()
    except KeyboardInterrupt:
        app.shutdown()
    finally:
        if mutex:
            w.kernel32.CloseHandle(mutex)
    return 0


def _ensure_consent() -> bool:
    """First-run disclosure. False means the user cancelled."""
    cfg = Config.load()
    if cfg.consent_version >= CONSENT_VERSION:
        return True
    flags = w.MB_OKCANCEL | w.MB_ICONINFORMATION
    if w.message_box(CONSENT_TEXT, flags=flags) != w.IDOK:
        return False
    cfg.consent_version = CONSENT_VERSION
    cfg.save()
    return True


if __name__ == "__main__":
    raise SystemExit(main())
