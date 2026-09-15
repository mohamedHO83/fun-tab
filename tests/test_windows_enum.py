"""Window labelling and search text."""

from __future__ import annotations

import pytest

from fun_tab.windows_enum import AppWindow


def app(title: str, app_name: str = "", exe: str = "", **kwargs) -> AppWindow:
    return AppWindow(
        hwnd=1,
        title=title,
        class_name=kwargs.pop("class_name", "TestClass"),
        pid=1,
        exe_path=exe,
        app_name=app_name,
        **kwargs,
    )


def test_exe_name_is_just_the_file():
    assert app("x", exe=r"C:\Program Files\Chrome\chrome.exe").exe_name == "chrome.exe"
    assert app("x").exe_name == ""


@pytest.mark.parametrize(
    "title, app_name, expected",
    [
        ("notes.md - Obsidian", "Obsidian", "notes.md"),
        ("Inbox — Google Chrome", "Google Chrome", "Inbox"),
        ("doc – Word", "Word", "doc"),
        ("channel | Slack", "Slack", "channel"),
        ("Spotify Premium", "Spotify", "Spotify Premium"),
        ("Calculator", "Calculator", "Calculator"),
    ],
)
def test_display_name_drops_a_redundant_app_suffix(title, app_name, expected):
    assert app(title, app_name).display_name == expected


def test_display_name_keeps_a_title_that_is_only_the_suffix():
    # Stripping here would leave an empty label.
    assert app(" - Obsidian", "Obsidian").display_name == "- Obsidian"


def test_display_name_falls_back_when_there_is_no_title():
    assert app("", "Spotify").display_name == "Spotify"
    assert app("", class_name="ConsoleWindowClass").display_name == "ConsoleWindowClass"
    assert AppWindow(hwnd=77, title="", class_name="", pid=1).display_name == "Window 77"


def test_display_name_trims_whitespace():
    assert app("  notes.md - Obsidian  ", "Obsidian").display_name == "notes.md"


def test_subtitle_names_the_application():
    assert app("notes.md - Obsidian", "Obsidian").subtitle == "Obsidian"


def test_subtitle_is_empty_when_it_would_repeat_the_title():
    assert app("Calculator", "Calculator").subtitle == ""
    assert app("calculator", "Calculator").subtitle == ""
    assert app("Untitled", "").subtitle == ""


def test_search_text_covers_title_app_and_executable():
    text = app("Inbox — Chrome", "Google Chrome", r"C:\x\chrome.exe").search_text
    assert "inbox" in text
    assert "google chrome" in text
    assert "chrome.exe" in text
    assert text == text.lower(), "queries are lower-cased before matching"


def win(hwnd: int, title: str, exe: str, app_name: str = "") -> AppWindow:
    return AppWindow(
        hwnd=hwnd,
        title=title,
        class_name="TestClass",
        pid=hwnd,
        exe_path=rf"C:\apps\{exe}",
        app_name=app_name or exe.removesuffix(".exe").title(),
    )


def test_group_windows_keeps_one_face_per_exe():
    from fun_tab.windows_enum import group_windows

    apps = [
        win(1, "notes.md", "obsidian.exe"),
        win(2, "Inbox", "chrome.exe"),
        win(3, "Gmail", "chrome.exe"),
        win(4, "standup", "slack.exe"),
    ]
    grouped = group_windows(apps)
    assert [a.title for a in grouped] == ["notes.md", "Inbox", "standup"]
    chrome = grouped[1]
    assert chrome.group_count == 2
    assert chrome.peer_hwnds == (2, 3)


def test_group_windows_pins_after_the_current_app():
    from fun_tab.windows_enum import group_windows

    apps = [
        win(1, "notes.md", "obsidian.exe"),
        win(2, "Inbox", "chrome.exe"),
        win(3, "standup", "slack.exe"),
        win(4, "Now Playing", "spotify.exe"),
    ]
    grouped = group_windows(apps, pinned_exes=["spotify.exe"])
    assert [a.exe_name.lower() for a in grouped] == [
        "obsidian.exe",
        "spotify.exe",
        "chrome.exe",
        "slack.exe",
    ]


def test_present_windows_expand_app_does_not_group():
    from fun_tab.windows_enum import present_windows

    apps = [
        win(1, "Inbox", "chrome.exe"),
        win(2, "Gmail", "chrome.exe"),
    ]
    shown = present_windows(apps, group_by_app=True, expand_app=True)
    assert [a.title for a in shown] == ["Inbox", "Gmail"]


def test_pin_windows_leaves_the_first_item_alone():
    from fun_tab.windows_enum import pin_windows

    apps = [
        win(1, "a", "one.exe"),
        win(2, "b", "two.exe"),
        win(3, "c", "three.exe"),
    ]
    pinned = pin_windows(apps, ["three.exe"])
    assert [a.exe_name.lower() for a in pinned] == ["one.exe", "three.exe", "two.exe"]
