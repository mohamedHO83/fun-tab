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
