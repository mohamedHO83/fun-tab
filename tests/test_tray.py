"""How a second launch finds and quits the copy that already owns Alt+Tab."""

from __future__ import annotations

from fun_tab import tray
from fun_tab import win32_types as w


def test_find_running_instance_prefers_the_host(monkeypatch):
    monkeypatch.setattr(w.user32, "FindWindowW", lambda cls, title: 11 if cls == tray.HOST_CLASS else 0)
    assert tray.find_running_instance() == 11


def test_find_running_instance_falls_back_to_the_wheel(monkeypatch):
    def find(cls, title):
        if cls == tray.WHEEL_CLASS and title == tray.WHEEL_TITLE:
            return 22
        return 0

    monkeypatch.setattr(w.user32, "FindWindowW", find)
    assert tray.find_running_instance() == 22


def test_find_running_instance_is_zero_when_nothing_is_up(monkeypatch):
    monkeypatch.setattr(w.user32, "FindWindowW", lambda cls, title: 0)
    assert tray.find_running_instance() == 0


def test_ask_quit_posts_to_the_host(monkeypatch):
    posted = []

    def find(cls, title):
        return 33 if cls == tray.HOST_CLASS else 0

    monkeypatch.setattr(w.user32, "FindWindowW", find)
    monkeypatch.setattr(
        w.user32,
        "PostMessageW",
        lambda hwnd, msg, wp, lp: posted.append((int(hwnd), int(msg))) or 1,
    )
    assert tray.ask_running_instance_to_quit() is True
    assert posted == [(33, tray.WM_HOST_QUIT)]


def test_ask_quit_closes_the_wheel_if_the_host_is_missing(monkeypatch):
    posted = []

    def find(cls, title):
        return 44 if cls == tray.WHEEL_CLASS else 0

    monkeypatch.setattr(w.user32, "FindWindowW", find)
    monkeypatch.setattr(
        w.user32,
        "PostMessageW",
        lambda hwnd, msg, wp, lp: posted.append((int(hwnd), int(msg))) or 1,
    )
    assert tray.ask_running_instance_to_quit() is True
    assert posted == [(44, w.WM_CLOSE)]


def test_ask_quit_is_false_when_nothing_is_running(monkeypatch):
    monkeypatch.setattr(w.user32, "FindWindowW", lambda cls, title: 0)
    assert tray.ask_running_instance_to_quit() is False


def test_demote_matches_only_our_executable(tmp_path, monkeypatch):
    import winreg

    calls = []

    class FakeKey:
        def __init__(self, names, values):
            self.names = names
            self.values = values

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    root_names = ["aaa", "bbb"]
    ours = tmp_path / "pythonw.exe"
    other = tmp_path / "other.exe"

    def open_key(hive, path, *rest):
        if path == r"Control Panel\NotifyIconSettings":
            return FakeKey(root_names, {})
        if path == "aaa":
            return FakeKey([], {"ExecutablePath": str(ours)})
        return FakeKey([], {"ExecutablePath": str(other)})

    def enum_key(key, index):
        if index >= len(key.names):
            raise OSError("end")
        return key.names[index]

    def query(key, name):
        if name not in key.values:
            raise OSError("missing")
        return key.values[name], 0

    def set_value(key, name, reserved, typ, value):
        calls.append((name, value))

    monkeypatch.setattr(winreg, "OpenKey", open_key)
    monkeypatch.setattr(winreg, "EnumKey", enum_key)
    monkeypatch.setattr(winreg, "QueryValueEx", query)
    monkeypatch.setattr(winreg, "SetValueEx", set_value)
    monkeypatch.setattr(winreg, "KEY_READ", 1, raising=False)
    monkeypatch.setattr(winreg, "KEY_SET_VALUE", 2, raising=False)
    monkeypatch.setattr(winreg, "HKEY_CURRENT_USER", 0, raising=False)
    monkeypatch.setattr(winreg, "REG_DWORD", 4, raising=False)

    assert tray.demote_notify_icon(str(ours)) is True
    assert calls == [("IsPromoted", 0)]
