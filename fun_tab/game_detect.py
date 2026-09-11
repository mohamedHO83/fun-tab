"""Guess whether the foreground window is a game.

Used only to decide whether Fun Tab should step aside from Alt+Tab. It does
not hide the hook or the overlay — it just looks at the foreground window's
size, chrome, and executable name.
"""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes

from . import win32_types as w
from .windows_enum import _exe_for_pid

GWL_STYLE = -16
WS_CAPTION = 0x00C00000

# Processes that cover a monitor but are still the desktop shell.
_SHELL_EXES = frozenset(
    {
        "explorer.exe",
        "searchhost.exe",
        "startmenuexperiencehost.exe",
        "shellexperiencehost.exe",
        "lockapp.exe",
        "logonui.exe",
        "dwm.exe",
        "textinputhost.exe",
        "sihost.exe",
    }
)

# Common game executables. Extra names can be added in config as `game_exes`.
_KNOWN_GAMES = frozenset(
    {
        "rocketleague.exe",
        "fortniteclient-win64-shipping.exe",
        "r5apex.exe",
        "cs2.exe",
        "csgo.exe",
        "valorant.exe",
        "valorant-win64-shipping.exe",
        "gta5.exe",
        "gtav.exe",
        "destiny2.exe",
        "overwatch.exe",
        "league of legends.exe",
        "leagueclient.exe",
        "modernwarfare.exe",
        "cod.exe",
        "wow.exe",
        "minecraft.windows.exe",
        "javaw.exe",
        "eldenring.exe",
        "cyberpunk2077.exe",
        "hl2.exe",
        "dota2.exe",
        "deadbydaylight-win64-shipping.exe",
        "rainbowsix.exe",
    }
)


def covers(
    window: tuple[int, int, int, int],
    area: tuple[int, int, int, int],
    slack: int = 8,
) -> bool:
    """Whether `window` fills `area`, allowing a few pixels of mismatch."""
    left, top, right, bottom = window
    al, at, ar, ab = area
    return left <= al + slack and top <= at + slack and right >= ar - slack and bottom >= ab - slack


def looks_like_game(
    *,
    exe_name: str,
    style: int,
    window: tuple[int, int, int, int],
    monitor: tuple[int, int, int, int],
    work: tuple[int, int, int, int],
    extra_exes: list[str] | tuple[str, ...] = (),
) -> bool:
    """Pure rule used by the live check, so the tests can drive it with numbers."""
    exe = (exe_name or "").lower()
    extras = {name.lower() for name in extra_exes if name}
    if exe and (exe in _KNOWN_GAMES or exe in extras):
        return True
    if exe in _SHELL_EXES:
        return False

    # A normal maximised window still has a caption, even though Windows
    # extends its rect a few pixels past the monitor edges for the invisible
    # resize border — that made an ordinary maximised browser or terminal
    # cover `monitor` by the check below and read as "exclusive fullscreen".
    # Real exclusive fullscreen and borderless-windowed games are both
    # caption-less, so bail out before either coverage check.
    if style & WS_CAPTION:
        return False

    # Exclusive / true fullscreen covers the whole monitor, taskbar and all.
    if covers(window, monitor):
        return True

    # Borderless windowed games fill the work area and drop the title bar.
    if covers(window, work):
        return True
    return False


# Windows Alt+Tab / Task View hosts. Seen while leaving a game with Alt held.
_TASK_SWITCHER_CLASSES = frozenset(
    {
        "MultitaskingViewFrame",
        "XamlExplorerHostIslandWindow",
        "ForegroundStaging",
        "TaskListThumbnailWnd",
    }
)


def is_task_switcher_class(class_name: str) -> bool:
    return class_name in _TASK_SWITCHER_CLASSES


def foreground_is_task_switcher() -> bool:
    """True while the native Alt+Tab / Task View UI owns the foreground."""
    hwnd = int(w.user32.GetForegroundWindow() or 0)
    if not hwnd:
        return False
    buf = ctypes.create_unicode_buffer(256)
    w.user32.GetClassNameW(hwnd, buf, 256)
    return is_task_switcher_class(buf.value)


def foreground_looks_like_a_game(extra_exes: list[str] | tuple[str, ...] = ()) -> bool:
    """The live check: whatever is in front right now."""
    hwnd = int(w.user32.GetForegroundWindow() or 0)
    if not hwnd:
        return False

    pid = wintypes.DWORD()
    w.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    exe = os.path.basename(_exe_for_pid(int(pid.value)))

    rect = w.RECT()
    if not w.user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return False
    window = (rect.left, rect.top, rect.right, rect.bottom)

    monitor = w.user32.MonitorFromWindow(hwnd, w.MONITOR_DEFAULTTONEAREST)
    info = w.MONITORINFO()
    info.cbSize = ctypes.sizeof(info)
    if not w.user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
        return False
    mon = (info.rcMonitor.left, info.rcMonitor.top, info.rcMonitor.right, info.rcMonitor.bottom)
    work = (info.rcWork.left, info.rcWork.top, info.rcWork.right, info.rcWork.bottom)
    style = int(w.user32.GetWindowLongW(hwnd, GWL_STYLE))
    return looks_like_game(
        exe_name=exe,
        style=style,
        window=window,
        monitor=mon,
        work=work,
        extra_exes=extra_exes,
    )
