"""System tray icon on the same thread as the overlay message loop.

pystray adds the icon from a helper thread. ``Shell_NotifyIcon(NIM_ADD)`` then
often fails silently, which is how Fun Tab can own Alt+Tab with no way to
quit. This module talks to the shell from the UI thread, keeps a stable GUID
so Windows 11 can remember the icon, and promotes it out of the overflow.
"""

from __future__ import annotations

import ctypes
import os
import sys
import uuid
from pathlib import Path

from . import autostart
from . import win32_types as w
from .config import config_path

HOST_CLASS = "FunTabHostClass"
HOST_TITLE = "Fun Tab Host"
WHEEL_CLASS = "FunTabWheelClass"
WHEEL_TITLE = "Fun Tab"
WM_TRAYICON = w.WM_APP + 1
WM_HOST_QUIT = w.WM_APP + 2
# Stable across runs so Explorer can keep "show on taskbar" for this app.
ICON_GUID = uuid.UUID("6e0f2c4a-8b17-4d9e-9a3c-1b2d3e4f5a60")

_ID_SETTINGS = 1
_ID_AUTOSTART = 2
_ID_EDIT = 3
_ID_RELOAD = 4
_ID_QUIT = 5


def _guid() -> w.GUID:
    guid = w.GUID()
    guid.Data1 = ICON_GUID.time_low
    guid.Data2 = ICON_GUID.time_mid
    guid.Data3 = ICON_GUID.time_hi_version
    guid.Data4 = (ctypes.c_ubyte * 8).from_buffer_copy(ICON_GUID.bytes[8:])
    return guid


def find_running_instance() -> int:
    hwnd = w.user32.FindWindowW(HOST_CLASS, HOST_TITLE)
    if hwnd:
        return int(hwnd)
    hwnd = w.user32.FindWindowW(WHEEL_CLASS, WHEEL_TITLE)
    return int(hwnd or 0)


def ask_running_instance_to_quit() -> bool:
    """Tell the already-running copy to shut down. True if the message went out."""
    host = w.user32.FindWindowW(HOST_CLASS, HOST_TITLE)
    if host:
        return bool(w.user32.PostMessageW(host, WM_HOST_QUIT, 0, 0))
    wheel = w.user32.FindWindowW(WHEEL_CLASS, WHEEL_TITLE)
    if wheel:
        return bool(w.user32.PostMessageW(wheel, w.WM_CLOSE, 0, 0))
    return False


def offer_to_quit_running() -> int:
    """Second launch: ask, then quit the first copy. Returns a process exit code."""
    question = (
        "Fun Tab is already running.\n\n"
        "The tray icon is sometimes hidden behind the ^ arrow on the taskbar.\n\n"
        "Quit Fun Tab now?"
    )
    flags = w.MB_YESNO | w.MB_ICONQUESTION
    if w.message_box(question, flags=flags) != w.IDYES:
        return 0
    if ask_running_instance_to_quit():
        w.message_box("Fun Tab has been closed.")
        return 0
    w.message_box(
        "Could not reach the running copy.\n\n"
        "Open Task Manager, end pythonw.exe (or Python), then start Fun Tab again."
    )
    return 1


def promote_notify_icon(executable: str) -> bool:
    """Ask Windows 11 to pin our tray icon next to the clock, not under ^."""
    import winreg

    exe = os.path.normcase(os.path.abspath(executable))
    try:
        root = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Control Panel\NotifyIconSettings",
            0,
            winreg.KEY_READ,
        )
    except OSError:
        return False

    promoted = False
    index = 0
    while True:
        try:
            name = winreg.EnumKey(root, index)
        except OSError:
            break
        index += 1
        try:
            sub = winreg.OpenKey(root, name, 0, winreg.KEY_READ | winreg.KEY_SET_VALUE)
        except OSError:
            continue
        with sub:
            try:
                path, _ = winreg.QueryValueEx(sub, "ExecutablePath")
            except OSError:
                continue
            if os.path.normcase(os.path.abspath(str(path))) != exe:
                continue
            try:
                winreg.SetValueEx(sub, "IsPromoted", 0, winreg.REG_DWORD, 1)
                promoted = True
            except OSError:
                continue
    return promoted


class TrayIcon:
    def __init__(self, app) -> None:
        self.app = app
        self.hwnd = 0
        self._hicon = 0
        self._own_icon = False
        self._wndproc = None
        self._taskbar_created = 0
        self._promote_tries = 8

    def install(self) -> bool:
        hinstance = w.kernel32.GetModuleHandleW(None)
        self._wndproc = self._make_proc()
        wc = w.WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(w.WNDCLASSEXW)
        wc.lpfnWndProc = self._wndproc
        wc.hInstance = hinstance
        wc.lpszClassName = HOST_CLASS
        w.user32.RegisterClassExW(ctypes.byref(wc))

        hwnd = w.user32.CreateWindowExW(
            w.WS_EX_TOOLWINDOW,
            HOST_CLASS,
            HOST_TITLE,
            w.WS_POPUP,
            0,
            0,
            0,
            0,
            None,
            None,
            hinstance,
            None,
        )
        if not hwnd:
            return False
        self.hwnd = int(hwnd)
        self._hicon = self._load_icon()
        self._taskbar_created = int(w.user32.RegisterWindowMessageW("TaskbarCreated"))
        if not self._add_icon(announce=True):
            return False
        self.try_promote()
        return True

    def uninstall(self) -> None:
        if self.hwnd:
            nid = self._nid(0)
            w.shell32.Shell_NotifyIconW(w.NIM_DELETE, ctypes.byref(nid))
            if w.user32.IsWindow(self.hwnd):
                w.user32.DestroyWindow(self.hwnd)
        if self._own_icon and self._hicon:
            w.user32.DestroyIcon(self._hicon)
        self.hwnd = 0
        self._hicon = 0

    def try_promote(self) -> None:
        if self._promote_tries <= 0:
            return
        self._promote_tries -= 1
        if promote_notify_icon(sys.executable):
            self._promote_tries = 0

    def _add_icon(self, *, announce: bool) -> bool:
        flags = w.NIF_MESSAGE | w.NIF_ICON | w.NIF_TIP | w.NIF_GUID | w.NIF_SHOWTIP
        if announce:
            flags |= w.NIF_INFO
        nid = self._nid(flags)
        if announce:
            nid.szInfoTitle = "Fun Tab"
            nid.szInfo = (
                "Click this icon for settings or to quit. "
                "If you do not see it, click the ^ arrow on the taskbar."
            )
            nid.dwInfoFlags = w.NIIF_INFO | w.NIIF_NOSOUND
        if w.shell32.Shell_NotifyIconW(w.NIM_ADD, ctypes.byref(nid)):
            return True
        # Older shells reject NIF_GUID; try again as a plain uID icon.
        nid.uFlags &= ~w.NIF_GUID
        nid.uID = 1
        return bool(w.shell32.Shell_NotifyIconW(w.NIM_ADD, ctypes.byref(nid)))

    def _nid(self, flags: int) -> w.NOTIFYICONDATAW:
        nid = w.NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(w.NOTIFYICONDATAW)
        nid.hWnd = self.hwnd
        nid.uFlags = flags
        nid.uCallbackMessage = WM_TRAYICON
        nid.hIcon = self._hicon
        nid.szTip = "Fun Tab"
        nid.guidItem = _guid()
        return nid

    def _load_icon(self) -> int:
        ico = Path(__file__).resolve().parent.parent / "assets" / "fun-tab.ico"
        handle = 0
        if ico.exists():
            handle = int(
                w.user32.LoadImageW(
                    None,
                    str(ico),
                    w.IMAGE_ICON,
                    0,
                    0,
                    w.LR_LOADFROMFILE | w.LR_DEFAULTSIZE,
                )
                or 0
            )
            self._own_icon = bool(handle)
        if not handle:
            handle = int(w.user32.LoadIconW(None, w.IDI_APPLICATION) or 0)
            self._own_icon = False
        return handle

    def _make_proc(self):
        @w.WNDPROC
        def wndproc(hwnd, msg, wparam, lparam):
            if msg == WM_HOST_QUIT or msg == w.WM_CLOSE:
                self.app.shutdown()
                return 0
            if msg == WM_TRAYICON:
                event = int(lparam) & 0xFFFF
                if event in (w.WM_LBUTTONUP, w.WM_LBUTTONDOWN):
                    if event == w.WM_LBUTTONUP:
                        self.app.open_settings_ui()
                    return 0
                if event == w.WM_RBUTTONUP:
                    self._popup_menu()
                    return 0
                return 0
            if self._taskbar_created and msg == self._taskbar_created:
                self._add_icon(announce=False)
                self._promote_tries = 8
                self.try_promote()
                return 0
            return w.user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        return wndproc

    def _popup_menu(self) -> None:
        menu = w.user32.CreatePopupMenu()
        if not menu:
            return
        try:
            w.user32.AppendMenuW(menu, w.MF_STRING, _ID_SETTINGS, "Settings…")
            w.user32.SetMenuDefaultItem(menu, _ID_SETTINGS, 0)
            w.user32.AppendMenuW(menu, w.MF_SEPARATOR, 0, None)
            auto_flags = w.MF_STRING | (
                w.MF_CHECKED if autostart.is_enabled() else w.MF_UNCHECKED
            )
            w.user32.AppendMenuW(menu, auto_flags, _ID_AUTOSTART, "Start with Windows")
            w.user32.AppendMenuW(menu, w.MF_STRING, _ID_EDIT, "Edit the settings file")
            w.user32.AppendMenuW(menu, w.MF_STRING, _ID_RELOAD, "Reload settings")
            w.user32.AppendMenuW(menu, w.MF_SEPARATOR, 0, None)
            w.user32.AppendMenuW(menu, w.MF_STRING, _ID_QUIT, "Quit Fun Tab")

            point = w.POINT()
            w.user32.GetCursorPos(ctypes.byref(point))
            w.user32.SetForegroundWindow(self.hwnd)
            choice = int(
                w.user32.TrackPopupMenu(
                    menu,
                    w.TPM_RIGHTBUTTON | w.TPM_RETURNCMD,
                    point.x,
                    point.y,
                    0,
                    self.hwnd,
                    None,
                )
            )
            w.user32.PostMessageW(self.hwnd, w.WM_NULL, 0, 0)
        finally:
            w.user32.DestroyMenu(menu)
        self._on_command(choice)

    def _on_command(self, choice: int) -> None:
        if choice == _ID_SETTINGS:
            self.app.open_settings_ui()
        elif choice == _ID_AUTOSTART:
            autostart.toggle()
        elif choice == _ID_EDIT:
            path = config_path()
            if not path.exists():
                self.app.cfg.save(path)
            try:
                os.startfile(str(path))  # noqa: S606 - user-initiated
            except OSError:
                pass
        elif choice == _ID_RELOAD:
            self.app._reload_requested = True
            w.user32.PostThreadMessageW(
                int(w.kernel32.GetCurrentThreadId()), w.WM_NULL, 0, 0
            )
        elif choice == _ID_QUIT:
            self.app.shutdown()
