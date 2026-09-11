"""Open-shortcut parsing and labels."""

from __future__ import annotations

from fun_tab import win32_types as w
from fun_tab.hotkey import (
    MOUSE4,
    MOUSE5,
    Hotkey,
    is_classic_alt_tab,
    modifiers_match,
    parse_hotkey,
)


def test_alt_tab_is_the_default():
    hk = parse_hotkey("")
    assert hk.alt and hk.kind == "key" and hk.code == w.VK_TAB
    assert is_classic_alt_tab(hk)


def test_mouse_side_buttons_parse():
    assert parse_hotkey("mouse4").code == MOUSE4
    assert parse_hotkey("mouse5").code == MOUSE5
    assert parse_hotkey("xbutton2").code == MOUSE5
    assert parse_hotkey("ctrl+mouse4").ctrl is True


def test_round_trip_keeps_the_chord():
    for text in ("alt+tab", "ctrl+alt+tab", "mouse4", "shift+mouse5", "ctrl+shift+a"):
        assert parse_hotkey(text).text() == parse_hotkey(parse_hotkey(text).text()).text()


def test_labels_are_readable():
    assert "Mouse 4" in parse_hotkey("mouse4").label()
    assert "Ctrl" in parse_hotkey("ctrl+alt+tab").label()
    assert "Alt" in parse_hotkey("ctrl+alt+tab").label()


def test_mouse_opens_are_sticky():
    assert parse_hotkey("mouse4").sticky is True
    assert parse_hotkey("ctrl+a").sticky is True
    assert parse_hotkey("alt+tab").sticky is False


def test_modifiers_match_requires_what_the_chord_asks_for():
    hk = parse_hotkey("ctrl+mouse4")
    assert modifiers_match(hk, ctrl=True, alt=False, shift=False)
    assert not modifiers_match(hk, ctrl=False, alt=False, shift=False)


def test_classic_alt_tab_still_allows_ctrl_for_sticky():
    hk = parse_hotkey("alt+tab")
    assert modifiers_match(hk, ctrl=True, alt=True, shift=False)
