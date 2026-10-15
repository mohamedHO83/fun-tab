"""How the running app notices that settings changed.

The settings window is a separate process, so the only thing joining them is
the file. If this stops working, saving looks like it did nothing.
"""

from __future__ import annotations

import json
import sys

from fun_tab.app import FunTabApp, _config_stamp, main
from fun_tab.config import Config
from fun_tab import win32_types as w


def test_a_missing_file_has_no_stamp(tmp_path):
    assert _config_stamp(tmp_path / "nope.json") == ()


def test_the_stamp_changes_when_the_file_is_rewritten(tmp_path):
    path = tmp_path / "config.json"
    Config().save(path)
    before = _config_stamp(path)
    assert before != ()

    Config(theme="light").save(path)
    assert _config_stamp(path) != before


def test_the_stamp_holds_still_while_the_file_does(tmp_path):
    """It is checked once a second forever, so it must not reload on its own."""
    path = tmp_path / "config.json"
    Config().save(path)
    assert _config_stamp(path) == _config_stamp(path)


def test_a_same_length_edit_is_still_noticed(tmp_path):
    """Two quick saves can share a timestamp on Windows, so content decides."""
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"theme": "dark"}), encoding="utf-8")
    before = _config_stamp(path)
    path.write_text(json.dumps({"theme": "auto"}), encoding="utf-8")
    assert _config_stamp(path) != before


def test_always_on_compat_does_not_need_a_game():
    app = FunTabApp()
    app.cfg.game_compat = "always"
    app.cfg.pause_in_games = False
    app._refresh_compat()
    assert app.hook.compat_active is True
    assert app.overlay.compat_active is True


def test_compat_can_be_switched_off():
    app = FunTabApp()
    app.cfg.game_compat = "off"
    app.cfg.pause_in_games = False
    app._refresh_compat()
    assert app.hook.compat_active is False


def test_pause_in_games_uninstalls_hooks_when_a_game_is_in_front(monkeypatch):
    app = FunTabApp()
    app.cfg.pause_in_games = True
    app.cfg.game_compat = "off"
    app.hook.install = lambda: setattr(app.hook, "_kb_hook", 1)  # type: ignore
    app.hook.uninstall = lambda: setattr(app.hook, "_kb_hook", None)  # type: ignore
    app.hook._kb_hook = 1
    monkeypatch.setattr(
        "fun_tab.game_detect.foreground_looks_like_a_game", lambda _extra=(): True
    )
    monkeypatch.setattr("fun_tab.game_detect.foreground_is_task_switcher", lambda: False)
    app._refresh_compat()
    assert app.hook.installed is False


def test_leaving_a_game_with_alt_held_does_not_rearm_hooks(monkeypatch):
    """Windows Alt+Tab out of a game: stay paused until Alt is up."""
    app = FunTabApp()
    app.cfg.pause_in_games = True
    app.cfg.game_compat = "auto"
    app.hook.install = lambda: setattr(app.hook, "_kb_hook", 1)  # type: ignore
    app.hook.uninstall = lambda: setattr(app.hook, "_kb_hook", None)  # type: ignore
    app.hook._kb_hook = 1
    monkeypatch.setattr("fun_tab.game_detect.foreground_is_task_switcher", lambda: False)

    monkeypatch.setattr(
        "fun_tab.game_detect.foreground_looks_like_a_game", lambda _extra=(): True
    )
    app._refresh_compat()
    assert app.hook.installed is False
    assert app.hook.compat_active is True

    # Focus left the game (or landed on the Windows switcher) while Alt is still down.
    monkeypatch.setattr(
        "fun_tab.game_detect.foreground_looks_like_a_game", lambda _extra=(): False
    )
    monkeypatch.setattr(app, "_alt_held", lambda: True)
    app._refresh_compat()
    assert app.hook.installed is False
    assert app.hook.compat_active is True


def test_after_alt_up_hooks_return_only_after_grace(monkeypatch):
    app = FunTabApp()
    app.cfg.pause_in_games = True
    app.cfg.game_compat = "auto"
    installed = {"n": 0}

    def _install():
        app.hook._kb_hook = 1
        installed["n"] += 1

    app.hook.install = _install  # type: ignore
    app.hook.uninstall = lambda: setattr(app.hook, "_kb_hook", None)  # type: ignore
    app.hook._kb_hook = 1
    monkeypatch.setattr("fun_tab.game_detect.foreground_is_task_switcher", lambda: False)
    monkeypatch.setattr(
        "fun_tab.game_detect.foreground_looks_like_a_game", lambda _extra=(): True
    )
    app._refresh_compat()

    monkeypatch.setattr(
        "fun_tab.game_detect.foreground_looks_like_a_game", lambda _extra=(): False
    )
    monkeypatch.setattr(app, "_alt_held", lambda: False)
    # First clear starts the grace window; hooks must stay off.
    app._refresh_compat()
    assert app.hook.installed is False
    assert installed["n"] == 0

    # Still inside the grace window.
    app._resume_hooks_at = __import__("time").perf_counter() + 5.0
    app._refresh_compat()
    assert app.hook.installed is False

    # Grace elapsed → re-arm Fun Tab for normal desktop Alt+Tab.
    app._resume_hooks_at = __import__("time").perf_counter() - 0.1
    app._refresh_compat()
    assert app.hook.installed is True
    assert app.hook.compat_active is False
    assert installed["n"] == 1


def test_soft_compat_stays_on_while_alt_held_after_a_game(monkeypatch):
    """Without pause: still leave Windows Alt+Tab alone until Alt comes up."""
    app = FunTabApp()
    app.cfg.pause_in_games = False
    app.cfg.game_compat = "auto"
    monkeypatch.setattr("fun_tab.game_detect.foreground_is_task_switcher", lambda: False)
    monkeypatch.setattr(
        "fun_tab.game_detect.foreground_looks_like_a_game", lambda _extra=(): True
    )
    app._refresh_compat()
    assert app.hook.compat_active is True

    monkeypatch.setattr(
        "fun_tab.game_detect.foreground_looks_like_a_game", lambda _extra=(): False
    )
    monkeypatch.setattr(app, "_alt_held", lambda: True)
    app._refresh_compat()
    assert app.hook.compat_active is True

    monkeypatch.setattr(app, "_alt_held", lambda: False)
    app._refresh_compat()
    assert app.hook.compat_active is False


def test_rewriting_the_same_settings_is_not_a_change(tmp_path):
    """Pressing Save without editing anything should not churn every cache."""
    path = tmp_path / "config.json"
    Config(theme="light").save(path)
    before = _config_stamp(path)
    Config(theme="light").save(path)
    assert _config_stamp(path) == before


def test_settings_flag_skips_the_switcher(monkeypatch):
    """A packed exe launches settings as FunTab.exe --settings, not a second hook."""
    called = []
    monkeypatch.setattr(sys, "argv", ["FunTab.exe", "--settings"])
    monkeypatch.setattr("fun_tab.settings_ui.main", lambda: called.append(True) or 0)
    assert main() == 0
    assert called == [True]


def test_close_selected_asks_once_per_session(monkeypatch):
    from fun_tab.app import FunTabApp
    from fun_tab.windows_enum import AppWindow

    app = FunTabApp()
    app.cfg.close_confirm = True
    target = AppWindow(
        hwnd=9,
        title="Notes",
        class_name="Notepad",
        pid=1,
        app_name="Notepad",
    )
    app.overlay._visible = True
    app.overlay._apps = [target]
    app.overlay._all_apps = [target]
    app.overlay._selected = 0

    asked = []
    closed = []

    monkeypatch.setattr(
        w,
        "message_box",
        lambda text, title="Fun Tab", flags=0: asked.append(text) or w.IDYES,
    )
    monkeypatch.setattr(
        "fun_tab.app.close_window",
        lambda hwnd: closed.append(hwnd) or True,
    )

    app._close_selected()
    assert len(asked) == 1
    assert closed == [9]
    assert app._close_confirmed_session is True

    # Second close skips the prompt.
    app.overlay._apps = [target]
    app.overlay._all_apps = [target]
    app.overlay._selected = 0
    app.overlay._visible = True
    app._close_selected()
    assert len(asked) == 1
    assert closed == [9, 9]


def test_closing_a_window_remembers_it_for_undo(monkeypatch):
    from fun_tab.app import FunTabApp
    from fun_tab.windows_enum import AppWindow

    app = FunTabApp()
    app.cfg = Config(close_confirm=False)
    target = AppWindow(
        hwnd=9, title="Notes", class_name="Notepad", pid=1, app_name="Notepad"
    )
    app.overlay._visible = True
    app.overlay._apps = [target]
    app.overlay._all_apps = [target]
    app.overlay._selected = 0

    monkeypatch.setattr("fun_tab.app.close_window", lambda hwnd: True)
    app._close_selected()

    assert app._last_closed is target


def test_an_elevated_window_that_refuses_to_close_is_not_remembered(monkeypatch):
    from fun_tab.app import FunTabApp
    from fun_tab.windows_enum import AppWindow

    app = FunTabApp()
    app.cfg = Config(close_confirm=False)
    target = AppWindow(
        hwnd=9, title="Notes", class_name="Notepad", pid=1, app_name="Notepad"
    )
    app.overlay._visible = True
    app.overlay._apps = [target]
    app.overlay._all_apps = [target]
    app.overlay._selected = 0

    monkeypatch.setattr("fun_tab.app.close_window", lambda hwnd: False)
    monkeypatch.setattr(w, "message_box", lambda *a, **k: w.IDOK)
    app._close_selected()

    assert app._last_closed is None


def test_ctrl_z_relaunches_whatever_was_last_closed(monkeypatch):
    from fun_tab.app import FunTabApp
    from fun_tab.windows_enum import AppWindow

    app = FunTabApp()
    target = AppWindow(
        hwnd=0,
        title="Notes",
        class_name="Notepad",
        pid=1,
        app_name="Notepad",
        exe_path=r"C:\Windows\notepad.exe",
    )
    app._last_closed = target
    app.overlay._visible = True

    launched = []
    monkeypatch.setattr(
        "fun_tab.app.launch_app", lambda target, fallback="": launched.append(target) or True
    )

    app._reopen_last_closed()

    assert launched == [r"C:\Windows\notepad.exe"]
    assert app.overlay.visible is False, "reopening hides the wheel, like a commit does"


def test_ctrl_z_does_nothing_when_nothing_has_been_closed(monkeypatch):
    app = FunTabApp()
    assert app._last_closed is None
    launched = []
    monkeypatch.setattr(
        "fun_tab.app.launch_app", lambda target, fallback="": launched.append(target) or True
    )

    app._reopen_last_closed()  # must not raise

    assert launched == []


def test_ensure_consent_accepts_and_persists(tmp_path, monkeypatch):
    from fun_tab.app import _ensure_consent
    from fun_tab.privacy import CONSENT_VERSION

    path = tmp_path / "config.json"
    monkeypatch.setattr("fun_tab.app.config_path", lambda: path)
    monkeypatch.setattr("fun_tab.config.config_path", lambda: path)
    monkeypatch.setattr(
        w,
        "message_box",
        lambda text, title="Fun Tab", flags=0: w.IDOK,
    )
    assert _ensure_consent() is True
    assert Config.load(path).consent_version == CONSENT_VERSION


def test_ensure_consent_cancel_aborts(tmp_path, monkeypatch):
    from fun_tab.app import _ensure_consent

    path = tmp_path / "config.json"
    monkeypatch.setattr("fun_tab.app.config_path", lambda: path)
    monkeypatch.setattr("fun_tab.config.config_path", lambda: path)
    Config(consent_version=0).save(path)
    monkeypatch.setattr(
        w,
        "message_box",
        lambda text, title="Fun Tab", flags=0: w.IDCANCEL,
    )
    assert _ensure_consent() is False


def test_hide_selected_app_writes_exclude_exes():
    from fun_tab.windows_enum import AppWindow

    app = FunTabApp()
    app.cfg = Config()
    app.overlay.cfg = app.cfg
    target = AppWindow(
        hwnd=9,
        title="Inbox",
        class_name="Chrome_WidgetWin_1",
        pid=1,
        exe_path=r"C:\Program Files\Google\Chrome\chrome.exe",
        app_name="Google Chrome",
    )
    app.overlay._visible = True
    app.overlay._apps = [target]
    app.overlay._all_apps = [target]
    app.overlay._selected = 0
    saved = []
    app.cfg.save = lambda path=None: saved.append(list(app.cfg.exclude_exes)) or True  # type: ignore
    cancelled = []
    app.overlay.set_callbacks(on_commit=lambda: None, on_cancel=lambda: cancelled.append(True))

    app._hide_selected_app()
    assert "chrome.exe" in app.cfg.exclude_exes
    assert saved
    assert cancelled == [True], "hiding the only app should close the wheel"


def test_pin_selected_app_round_trips():
    from fun_tab.windows_enum import AppWindow

    app = FunTabApp()
    app.cfg = Config()
    app.overlay.cfg = app.cfg
    target = AppWindow(
        hwnd=3,
        title="Slack",
        class_name="Slack",
        pid=1,
        exe_path=r"C:\Slack\slack.exe",
        app_name="Slack",
    )
    extra = AppWindow(
        hwnd=4,
        title="Notes",
        class_name="Notepad",
        pid=2,
        exe_path=r"C:\Windows\notepad.exe",
        app_name="Notepad",
    )
    app.overlay._all_apps = [extra, target]
    app.overlay._apps = [extra, target]
    app.overlay._selected = 1
    app.cfg.save = lambda path=None: True  # type: ignore

    app._toggle_pin_selected()
    assert app.cfg.pinned_exes == ["slack.exe"]
    app._toggle_pin_selected()
    assert app.cfg.pinned_exes == []


def test_manual_pause_keeps_hooks_uninstalled(monkeypatch):
    app = FunTabApp()
    app.cfg = Config(pause_in_games=False, game_compat="off")
    app.hook.install = lambda: setattr(app.hook, "_kb_hook", 1)  # type: ignore
    app.hook.uninstall = lambda: setattr(app.hook, "_kb_hook", None)  # type: ignore
    app.hook._kb_hook = 1
    monkeypatch.setattr("fun_tab.game_detect.foreground_is_task_switcher", lambda: False)
    monkeypatch.setattr(
        "fun_tab.game_detect.foreground_looks_like_a_game", lambda _extra=(): False
    )

    app.set_paused(True)
    assert app._paused_manually is True
    assert app.hook.installed is False

    app._refresh_compat()
    assert app.hook.installed is False, "a tray pause must outlast the game-detect poll"

    app.set_paused(False)
    assert app._paused_manually is False
    assert app.hook.installed is True


class _FakeTray:
    def __init__(self):
        self.calls = []

    def notify_update(self, version, url, notes=""):
        self.calls.append((version, url, notes))


def test_a_confirmed_update_is_announced_once():
    app = FunTabApp()
    tray = _FakeTray()
    app._tray = tray
    app._update = ("0.5.0", "https://example.test/release", "## Highlights\n- a thing")

    app._maybe_announce_update()
    app._maybe_announce_update()  # a second tick must not show it twice

    assert tray.calls == [
        ("0.5.0", "https://example.test/release", "## Highlights\n- a thing")
    ]


def test_nothing_is_announced_without_a_confirmed_update():
    app = FunTabApp()
    tray = _FakeTray()
    app._tray = tray

    app._maybe_announce_update()

    assert tray.calls == []


def test_sound_default_is_off():
    assert Config().sound_enabled is False


def test_sound_is_silent_when_turned_off(monkeypatch):
    app = FunTabApp()
    app.cfg.sound_enabled = False
    calls = []
    monkeypatch.setattr("fun_tab.sound.play", lambda name: calls.append(name))
    app._play_sound("open")
    assert calls == []


def test_sound_plays_when_turned_on(monkeypatch):
    app = FunTabApp()
    app.cfg.sound_enabled = True
    calls = []
    monkeypatch.setattr("fun_tab.sound.play", lambda name: calls.append(name))
    app._play_sound("commit")
    assert calls == ["commit"]


def test_commit_plays_a_sound_only_when_something_is_actually_selected(monkeypatch):
    from fun_tab.windows_enum import AppWindow

    app = FunTabApp()
    app.cfg.sound_enabled = True
    played = []
    monkeypatch.setattr(app, "_play_sound", lambda name: played.append(name))
    monkeypatch.setattr("fun_tab.app.activate_window", lambda hwnd: True)

    # Nothing open: commit() must not make noise for doing nothing.
    app.commit()
    assert played == []

    target = AppWindow(hwnd=9, title="Notes", class_name="Notepad", pid=1)
    app.overlay._visible = True
    app.overlay._apps = [target]
    app.overlay._all_apps = [target]
    app.overlay._selected = 0

    app.commit()
    assert played == ["commit"]


def test_open_wheel_plays_a_sound_only_when_it_actually_opens(monkeypatch):
    app = FunTabApp()
    app.cfg = Config(sound_enabled=True)  # not the real on-disk config and its pins
    played = []
    monkeypatch.setattr(app, "_play_sound", lambda name: played.append(name))

    # Already open: open_wheel() must not fire a second "open" sound.
    app.overlay._visible = True
    app.open_wheel()
    assert played == []

    app.overlay._visible = False
    monkeypatch.setattr("fun_tab.app.enumerate_windows", lambda **_k: [])
    app.open_wheel()
    assert played == [], "no apps to show means nothing actually opened"

    from fun_tab.windows_enum import AppWindow

    target = AppWindow(hwnd=9, title="Notes", class_name="Notepad", pid=1)
    monkeypatch.setattr("fun_tab.app.enumerate_windows", lambda **_k: [target])
    monkeypatch.setattr(app.overlay, "show", lambda *a, **k: None)

    app.open_wheel()
    assert played == ["open"]


def test_update_now_does_nothing_without_a_confirmed_update(monkeypatch):
    app = FunTabApp()
    started = []
    monkeypatch.setattr(
        "threading.Thread",
        lambda *a, **k: started.append(1) or _FakeThread(),
    )
    app.update_now()
    assert started == []


class _FakeThread:
    def start(self):
        pass


def test_update_now_will_not_start_twice_at_once(monkeypatch):
    app = FunTabApp()
    app._update = ("0.6.0", "https://example.test/release", "")
    started = []
    monkeypatch.setattr(
        "threading.Thread", lambda *a, **k: started.append(a) or _FakeThread()
    )
    app.update_now()
    app.update_now()
    assert len(started) == 1


def test_apply_update_now_falls_back_when_running_from_source(monkeypatch):
    app = FunTabApp()
    monkeypatch.setattr("fun_tab.paths.frozen", lambda: False)
    opened = []
    monkeypatch.setattr("os.startfile", lambda url: opened.append(url))

    app._apply_update_now("https://example.test/release")

    assert opened == ["https://example.test/release"]
    assert app._updating_now is False
    assert app._running is True


def test_apply_update_now_falls_back_without_an_installer_asset(monkeypatch):
    app = FunTabApp()
    monkeypatch.setattr("fun_tab.paths.frozen", lambda: True)
    monkeypatch.setattr("fun_tab.updates.fetch_latest_release", lambda repo: None)
    opened = []
    shown = []
    monkeypatch.setattr("os.startfile", lambda url: opened.append(url))
    monkeypatch.setattr(w, "message_box", lambda text, **k: shown.append(text) or 1)

    app._apply_update_now("https://example.test/release")

    assert opened == ["https://example.test/release"]
    assert len(shown) == 1
    assert app._running is True


def test_apply_update_now_falls_back_when_the_download_fails(monkeypatch):
    from fun_tab.updates import Release

    app = FunTabApp()
    monkeypatch.setattr("fun_tab.paths.frozen", lambda: True)
    monkeypatch.setattr(
        "fun_tab.updates.fetch_latest_release",
        lambda repo: Release(version="0.6.0", url="x", installer_url="https://dl/x.exe", installer_size=10),
    )
    monkeypatch.setattr("fun_tab.updates.download_asset", lambda *a, **k: False)
    opened = []
    monkeypatch.setattr("os.startfile", lambda url: opened.append(url))
    monkeypatch.setattr(w, "message_box", lambda *a, **k: 1)

    app._apply_update_now("https://example.test/release")

    assert opened == ["https://example.test/release"]
    assert app._running is True


def test_apply_update_now_falls_back_when_the_installer_wont_start(monkeypatch, tmp_path):
    from fun_tab.updates import Release

    app = FunTabApp()
    monkeypatch.setattr("fun_tab.paths.frozen", lambda: True)
    monkeypatch.setattr(
        "fun_tab.updates.fetch_latest_release",
        lambda repo: Release(version="0.6.0", url="x", installer_url="https://dl/x.exe", installer_size=10),
    )

    def fake_download(url, dest, expected_size=0, timeout=0):
        dest.write_bytes(b"MZ" + b"\x00" * 8)
        return True

    monkeypatch.setattr("fun_tab.updates.download_asset", fake_download)
    monkeypatch.setattr("tempfile.mkdtemp", lambda prefix="": str(tmp_path))

    def boom(*a, **k):
        raise OSError("blocked")

    monkeypatch.setattr("subprocess.Popen", boom)
    opened = []
    monkeypatch.setattr("os.startfile", lambda url: opened.append(url))
    monkeypatch.setattr(w, "message_box", lambda *a, **k: 1)

    app._apply_update_now("https://example.test/release")

    assert opened == ["https://example.test/release"]
    assert app._running is True


def test_apply_update_now_falls_back_when_the_installer_exits_immediately(monkeypatch, tmp_path):
    from fun_tab.updates import Release

    app = FunTabApp()
    monkeypatch.setattr("fun_tab.paths.frozen", lambda: True)
    monkeypatch.setattr(
        "fun_tab.updates.fetch_latest_release",
        lambda repo: Release(version="0.6.0", url="x", installer_url="https://dl/x.exe", installer_size=10),
    )

    def fake_download(url, dest, expected_size=0, timeout=0):
        dest.write_bytes(b"MZ" + b"\x00" * 8)
        return True

    monkeypatch.setattr("fun_tab.updates.download_asset", fake_download)
    monkeypatch.setattr("tempfile.mkdtemp", lambda prefix="": str(tmp_path))

    class DeadProc:
        returncode = 1

        def poll(self):
            return 1

    monkeypatch.setattr("subprocess.Popen", lambda *a, **k: DeadProc())
    monkeypatch.setattr("time.sleep", lambda s: None)
    opened = []
    monkeypatch.setattr("os.startfile", lambda url: opened.append(url))
    monkeypatch.setattr(w, "message_box", lambda *a, **k: 1)

    app._apply_update_now("https://example.test/release")

    assert opened == ["https://example.test/release"]
    assert app._running is True


def test_apply_update_now_succeeds_and_stops_the_loop(monkeypatch, tmp_path):
    from fun_tab.updates import Release

    app = FunTabApp()
    app._ui_thread_id = 4242
    monkeypatch.setattr("fun_tab.paths.frozen", lambda: True)
    monkeypatch.setattr(
        "fun_tab.updates.fetch_latest_release",
        lambda repo: Release(version="0.6.0", url="x", installer_url="https://dl/x.exe", installer_size=10),
    )

    def fake_download(url, dest, expected_size=0, timeout=0):
        dest.write_bytes(b"MZ" + b"\x00" * 8)
        return True

    monkeypatch.setattr("fun_tab.updates.download_asset", fake_download)
    monkeypatch.setattr("tempfile.mkdtemp", lambda prefix="": str(tmp_path))

    class AliveProc:
        returncode = None

        def poll(self):
            return None

    posted = []
    monkeypatch.setattr("subprocess.Popen", lambda *a, **k: AliveProc())
    monkeypatch.setattr("time.sleep", lambda s: None)
    monkeypatch.setattr(
        "fun_tab.win32_types.user32.PostThreadMessageW",
        lambda tid, msg, wp, lp: posted.append(tid) or 1,
    )

    app._apply_update_now("https://example.test/release")

    assert app._running is False
    assert posted == [4242]


def test_cycling_plays_a_sound(monkeypatch):
    from fun_tab import hook as hook_actions
    from fun_tab.hook import Action
    from fun_tab.windows_enum import AppWindow

    app = FunTabApp()
    app.cfg.sound_enabled = True
    target = AppWindow(hwnd=1, title="A", class_name="C", pid=1)
    app.overlay._visible = True
    app.overlay._apps = [target]
    app.overlay._all_apps = [target]
    app.overlay._selected = 0

    played = []
    monkeypatch.setattr(app, "_play_sound", lambda name: played.append(name))

    app._dispatch(Action(hook_actions.CYCLE, 1))
    app._dispatch(Action(hook_actions.SAME_APP, 1))

    assert played == ["cycle", "cycle"]


def test_first_and_last_do_not_play_the_cycle_sound(monkeypatch):
    """Home/End jump to an endpoint; they aren't "tabbing" in the sense the
    cycle tick is meant to give feedback for."""
    from fun_tab import hook as hook_actions
    from fun_tab.hook import Action
    from fun_tab.windows_enum import AppWindow

    app = FunTabApp()
    app.cfg.sound_enabled = True
    target = AppWindow(hwnd=1, title="A", class_name="C", pid=1)
    app.overlay._visible = True
    app.overlay._apps = [target]
    app.overlay._all_apps = [target]
    app.overlay._selected = 0

    played = []
    monkeypatch.setattr(app, "_play_sound", lambda name: played.append(name))

    app._dispatch(Action(hook_actions.FIRST))
    app._dispatch(Action(hook_actions.LAST))

    assert played == []
