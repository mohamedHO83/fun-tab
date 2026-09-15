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


def win(
    hwnd: int,
    title: str,
    exe: str,
    app_name: str = "",
    *,
    aumid: str = "",
    z_index: int = 0,
) -> AppWindow:
    return AppWindow(
        hwnd=hwnd,
        title=title,
        class_name="TestClass",
        pid=hwnd,
        exe_path=rf"C:\apps\{exe}",
        app_name=app_name or exe.removesuffix(".exe").title(),
        aumid=aumid,
        z_index=z_index,
    )


# -- grouping ---------------------------------------------------------------


def test_group_windows_keeps_one_face_per_exe():
    from fun_tab.windows_enum import group_windows

    apps = [
        win(1, "notes.md", "obsidian.exe"),
        win(2, "Inbox", "chrome.exe", z_index=1),
        win(3, "Gmail", "chrome.exe", z_index=2),
        win(4, "standup", "slack.exe"),
    ]
    grouped = group_windows(apps)
    assert [a.title for a in grouped] == ["notes.md", "Inbox", "standup"]
    chrome = grouped[1]
    assert chrome.group_count == 2
    assert chrome.peer_hwnds == (2, 3)


def test_group_windows_orders_peers_by_z_index_not_by_recency():
    """The fan steps through peers, so their order must not shift underneath it.

    The face stays the most recent window — that is what you want to land on —
    but the peers behind it are ordered by Z-order, which does not change just
    because you looked at one of them.
    """
    from fun_tab.windows_enum import group_windows

    apps = [
        win(7, "third", "chrome.exe", z_index=2),
        win(5, "first", "chrome.exe", z_index=0),
        win(6, "second", "chrome.exe", z_index=1),
    ]
    face = group_windows(apps)[0]
    assert face.title == "third", "the face is still the most recent window"
    assert face.peer_hwnds == (5, 6, 7)


def test_aumid_splits_apps_that_share_an_executable():
    """Two Chrome profiles are two taskbar buttons, so they are two slices."""
    from fun_tab.windows_enum import group_windows

    apps = [
        win(1, "Work", "chrome.exe", aumid="Chrome.Work"),
        win(2, "Home", "chrome.exe", aumid="Chrome.Home"),
        win(3, "Work too", "chrome.exe", aumid="Chrome.Work"),
    ]
    grouped = group_windows(apps)
    assert [a.title for a in grouped] == ["Work", "Home"]
    assert grouped[0].peer_hwnds == (1, 3)


def test_windows_without_an_aumid_still_group_by_executable():
    from fun_tab.windows_enum import group_key

    plain = win(1, "a", "notepad.exe")
    assert group_key(plain) == group_key(win(2, "b", "notepad.exe"))
    assert "aumid" not in group_key(plain), "no property store, no AUMID key"


def test_group_key_falls_back_to_the_window_class():
    from fun_tab.windows_enum import group_key

    orphan = AppWindow(hwnd=1, title="x", class_name="Ghost", pid=0)
    assert group_key(orphan) == "class:Ghost"


# -- the pinned lane --------------------------------------------------------


def test_present_windows_without_slots_is_the_old_flat_list():
    from fun_tab.windows_enum import present_windows

    apps = [win(1, "Inbox", "chrome.exe"), win(2, "standup", "slack.exe")]
    lane, mru = present_windows(apps, group_by_app=True)
    assert lane == []
    assert [a.title for a in mru] == ["Inbox", "standup"]


def test_a_running_pinned_app_leaves_the_mru_list():
    """Or the same app would get two slices, one of them a lie."""
    from fun_tab.windows_enum import present_windows

    apps = [
        win(1, "notes.md", "obsidian.exe"),
        win(2, "Now Playing", "spotify.exe"),
        win(3, "standup", "slack.exe"),
    ]
    lane, mru = present_windows(
        apps, group_by_app=True, slots=[{"index": 0, "exe": "spotify.exe"}]
    )
    assert [a.exe_name.lower() for a in lane] == ["spotify.exe"]
    assert [a.exe_name.lower() for a in mru] == ["obsidian.exe", "slack.exe"]
    assert lane[0].running is True
    assert lane[0].slot == 0


def test_a_pinned_app_that_is_closed_still_gets_its_slot():
    from fun_tab.windows_enum import assign_slots

    lane, mru = assign_slots(
        [win(1, "notes.md", "obsidian.exe")],
        [{"index": 0, "exe": "spotify.exe", "label": "Spotify"}],
    )
    assert len(lane) == 1
    assert lane[0].is_dead is True
    assert lane[0].hwnd == 0
    assert lane[0].display_name == "Spotify"
    assert [a.exe_name.lower() for a in mru] == ["obsidian.exe"]


def test_a_pin_keeps_its_slot_when_the_app_starts():
    """The slot index is the promise; whether the app is running is not."""
    from fun_tab.windows_enum import assign_slots

    slots = [{"index": 0, "exe": "steam.exe"}, {"index": 1, "exe": "spotify.exe"}]
    closed, _ = assign_slots([win(1, "notes", "obsidian.exe")], slots)
    running, _ = assign_slots(
        [win(1, "notes", "obsidian.exe"), win(2, "Now Playing", "spotify.exe")], slots
    )
    assert [a.slot for a in closed] == [0, 1]
    assert [a.slot for a in running] == [0, 1]
    assert closed[1].slot == running[1].slot == 1


def test_slots_match_on_aumid_as_well_as_executable():
    from fun_tab.windows_enum import assign_slots

    apps = [win(1, "Work", "chrome.exe", aumid="Chrome.Work")]
    lane, mru = assign_slots(apps, [{"index": 0, "aumid": "Chrome.Work"}])
    assert lane[0].hwnd == 1
    assert mru == []


def test_a_pinned_group_takes_all_its_peers_out_of_the_mru_list():
    from fun_tab.windows_enum import present_windows

    apps = [
        win(1, "Inbox", "chrome.exe", z_index=0),
        win(2, "Gmail", "chrome.exe", z_index=1),
        win(3, "standup", "slack.exe"),
    ]
    lane, mru = present_windows(
        apps, group_by_app=True, slots=[{"index": 0, "exe": "chrome.exe"}]
    )
    assert lane[0].group_count == 2
    assert [a.exe_name.lower() for a in mru] == ["slack.exe"]


def test_launch_target_prefers_a_real_executable():
    """An AUMID is often a grouping label rather than a shell identity.

    Chrome reports ``Chrome``, which names nothing in AppsFolder, so trusting
    the AUMID first would break launching for ordinary desktop apps.
    """
    from fun_tab.windows_enum import launch_target

    app = win(1, "Inbox", "chrome.exe", aumid="Chrome")
    assert launch_target(app) == r"C:\apps\chrome.exe"


def test_launch_target_uses_the_shell_namespace_for_packaged_apps():
    from fun_tab.windows_enum import launch_target

    packaged = AppWindow(
        hwnd=1,
        title="WhatsApp",
        class_name="X",
        pid=1,
        exe_path=r"C:\Program Files\WindowsApps\WhatsApp_1.0\WhatsApp.exe",
        aumid="WhatsAppDesktop!App",
    )
    assert launch_target(packaged) == "shell:AppsFolder\\WhatsAppDesktop!App"
