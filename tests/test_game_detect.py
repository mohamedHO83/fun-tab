"""Rules that decide a foreground window is a game."""

from __future__ import annotations

from fun_tab.game_detect import WS_CAPTION, is_task_switcher_class, looks_like_game

MONITOR = (0, 0, 1920, 1080)
WORK = (0, 0, 1920, 1040)  # taskbar along the bottom


def test_rocket_league_is_a_game_even_in_a_window():
    assert looks_like_game(
        exe_name="RocketLeague.exe",
        style=WS_CAPTION,
        window=(100, 100, 500, 400),
        monitor=MONITOR,
        work=WORK,
    )


def test_an_extra_exe_from_config_counts():
    assert looks_like_game(
        exe_name="my-indie.exe",
        style=WS_CAPTION,
        window=(100, 100, 500, 400),
        monitor=MONITOR,
        work=WORK,
        extra_exes=["my-indie.exe"],
    )


def test_explorer_is_never_a_game():
    assert not looks_like_game(
        exe_name="explorer.exe",
        style=0,
        window=MONITOR,
        monitor=MONITOR,
        work=WORK,
    )


def test_true_fullscreen_counts_as_a_game():
    assert looks_like_game(
        exe_name="unknown.exe",
        style=0,
        window=MONITOR,
        monitor=MONITOR,
        work=WORK,
    )


def test_borderless_over_the_work_area_counts():
    assert looks_like_game(
        exe_name="unknown.exe",
        style=0,
        window=WORK,
        monitor=MONITOR,
        work=WORK,
    )


def test_a_maximised_browser_does_not():
    assert not looks_like_game(
        exe_name="chrome.exe",
        style=WS_CAPTION,
        window=WORK,
        monitor=MONITOR,
        work=WORK,
    )


def test_a_normal_window_does_not():
    assert not looks_like_game(
        exe_name="Code.exe",
        style=WS_CAPTION,
        window=(80, 40, 1200, 800),
        monitor=MONITOR,
        work=WORK,
    )


def test_a_maximised_window_does_not_count_just_for_covering_the_monitor():
    """Windows pads a maximised window's rect a few px past the monitor edge
    for its invisible resize border, so a normal maximised app can satisfy
    the "covers the monitor" check while still being an ordinary window."""
    assert not looks_like_game(
        exe_name="opera.exe",
        style=WS_CAPTION,
        window=(-8, -8, 1928, 1088),
        monitor=MONITOR,
        work=WORK,
    )


def test_task_switcher_classes_are_recognised():
    assert is_task_switcher_class("MultitaskingViewFrame")
    assert not is_task_switcher_class("Chrome_WidgetWin_1")
