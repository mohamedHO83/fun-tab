"""Safer activate / close paths and capture guards."""

from __future__ import annotations

from ctypes import wintypes

from fun_tab import win32_types as w
from fun_tab.windows_enum import activate_window, capture_thumbnail, close_window


def _set_pid(pid_ptr, value: int) -> None:
    if pid_ptr:
        pid_ptr._obj.value = value


def test_activate_prefers_switch_without_asfw(monkeypatch):
    allowed = []
    monkeypatch.setattr(w.user32, "IsWindow", lambda hwnd: True)
    monkeypatch.setattr(w.user32, "IsIconic", lambda hwnd: False)
    monkeypatch.setattr(
        w.user32,
        "GetWindowThreadProcessId",
        lambda hwnd, pid_ptr: (_set_pid(pid_ptr, 7) or 1),
    )
    monkeypatch.setattr(w.user32, "SwitchToThisWindow", lambda hwnd, alt: None)
    monkeypatch.setattr(w.user32, "GetForegroundWindow", lambda: 55)
    monkeypatch.setattr(
        w.user32,
        "AllowSetForegroundWindow",
        lambda pid: allowed.append(int(pid)) or True,
    )
    assert activate_window(55) is True
    assert allowed == []
    assert w.ASFW_ANY not in allowed


def test_activate_uses_target_pid_not_asfw_any(monkeypatch):
    allowed = []
    monkeypatch.setattr(w.user32, "IsWindow", lambda hwnd: True)
    monkeypatch.setattr(w.user32, "IsIconic", lambda hwnd: False)

    def get_tid(hwnd, pid_ptr):
        if pid_ptr is not None:
            import ctypes

            ctypes.cast(pid_ptr, ctypes.POINTER(wintypes.DWORD)).contents.value = 4242
        return 9

    monkeypatch.setattr(w.user32, "GetWindowThreadProcessId", get_tid)
    monkeypatch.setattr(w.user32, "SwitchToThisWindow", lambda hwnd, alt: None)

    def foreground():
        # Fail until ASFW has been granted, then claim focus.
        if allowed:
            return 77
        return 0

    monkeypatch.setattr(w.user32, "GetForegroundWindow", foreground)
    monkeypatch.setattr(
        w.user32,
        "AllowSetForegroundWindow",
        lambda pid: allowed.append(int(pid)) or True,
    )
    monkeypatch.setattr(w.user32, "AttachThreadInput", lambda *a: False)
    monkeypatch.setattr(w.user32, "BringWindowToTop", lambda hwnd: True)
    monkeypatch.setattr(w.user32, "ShowWindow", lambda hwnd, cmd: True)
    monkeypatch.setattr(w.user32, "SetForegroundWindow", lambda hwnd: True)
    monkeypatch.setattr(w.user32, "SetActiveWindow", lambda hwnd: True)
    monkeypatch.setattr(w.kernel32, "GetCurrentThreadId", lambda: 1)

    activate_window(77)
    assert w.ASFW_ANY not in allowed
    assert allowed == [4242]


def test_close_refuses_elevated_windows(monkeypatch):
    monkeypatch.setattr(w.user32, "IsWindow", lambda hwnd: True)
    monkeypatch.setattr("fun_tab.windows_enum.window_is_elevated", lambda hwnd: True)
    posted = []
    monkeypatch.setattr(
        w.user32,
        "PostMessageW",
        lambda *a: posted.append(a) or True,
    )
    assert close_window(12) is False
    assert posted == []


def test_capture_skips_excluded_affinity(monkeypatch):
    monkeypatch.setattr(w.user32, "IsWindow", lambda hwnd: True)
    monkeypatch.setattr(w, "window_excludes_capture", lambda hwnd: True)
    assert capture_thumbnail(1) is None


def test_capture_skips_elevated(monkeypatch):
    monkeypatch.setattr(w.user32, "IsWindow", lambda hwnd: True)
    monkeypatch.setattr(w, "window_excludes_capture", lambda hwnd: False)
    monkeypatch.setattr("fun_tab.windows_enum.window_is_elevated", lambda hwnd: True)
    assert capture_thumbnail(1) is None
