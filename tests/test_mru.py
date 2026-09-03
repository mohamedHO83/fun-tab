"""Most-recently-used tracking and the resulting window order."""

from __future__ import annotations

from PIL import Image

from fun_tab.mru import ForegroundTracker
from fun_tab.windows_enum import AppWindow, order_windows


def app(hwnd: int, *, minimized: bool = False) -> AppWindow:
    return AppWindow(
        hwnd=hwnd,
        title=f"window {hwnd}",
        class_name="TestClass",
        pid=hwnd,
        icon=Image.new("RGBA", (8, 8)),
        minimized=minimized,
    )


# -- the tracker ------------------------------------------------------------


def test_most_recent_window_ranks_first():
    tracker = ForegroundTracker()
    tracker.note(10)
    tracker.note(20)
    assert tracker.rank(20) < tracker.rank(10)


def test_refocusing_moves_a_window_back_to_the_front():
    tracker = ForegroundTracker()
    for hwnd in (10, 20, 30):
        tracker.note(hwnd)
    tracker.note(10)
    assert tracker.snapshot()[:3] == [10, 30, 20]


def test_repeated_notes_do_not_duplicate():
    tracker = ForegroundTracker()
    for _ in range(5):
        tracker.note(42)
    assert tracker.snapshot() == [42]


def test_unknown_windows_rank_behind_everything():
    tracker = ForegroundTracker()
    tracker.note(10)
    assert tracker.rank(999) > tracker.rank(10)


def test_forgetting_a_window_removes_it():
    tracker = ForegroundTracker()
    tracker.note(10)
    tracker.note(20)
    tracker.forget(10)
    assert tracker.snapshot() == [20]
    assert tracker.rank(10) > tracker.rank(20)


def test_forgetting_an_unknown_window_is_harmless():
    tracker = ForegroundTracker()
    tracker.forget(123)
    assert tracker.snapshot() == []


def test_history_is_bounded():
    tracker = ForegroundTracker(limit=4)
    for hwnd in range(1, 20):
        tracker.note(hwnd)
    assert len(tracker.snapshot()) == 4
    assert tracker.snapshot() == [19, 18, 17, 16]


def test_zero_is_never_tracked():
    tracker = ForegroundTracker()
    tracker.note(0)
    assert tracker.snapshot() == []


def test_snapshot_is_a_copy():
    tracker = ForegroundTracker()
    tracker.note(10)
    tracker.snapshot().clear()
    assert tracker.snapshot() == [10]


# -- ordering ---------------------------------------------------------------


def test_order_follows_the_tracker():
    tracker = ForegroundTracker()
    for hwnd in (3, 1, 2):  # 2 is the current foreground
        tracker.note(hwnd)
    ordered = order_windows([app(1), app(2), app(3)], [1, 2, 3], rank=tracker.rank)
    assert [a.hwnd for a in ordered] == [2, 1, 3]


def test_windows_the_tracker_never_saw_keep_z_order():
    tracker = ForegroundTracker()
    tracker.note(5)
    apps = [app(9), app(7), app(5)]
    ordered = order_windows(apps, [7, 9, 5], rank=tracker.rank)
    assert [a.hwnd for a in ordered] == [5, 7, 9], "known first, then z-order"


def test_without_a_rank_the_order_is_untouched():
    apps = [app(3), app(1), app(2)]
    assert order_windows(apps, [1, 2, 3]) == apps


def test_minimized_windows_sink_to_the_end():
    apps = [app(1, minimized=True), app(2), app(3, minimized=True), app(4)]
    ordered = order_windows(apps, [1, 2, 3, 4], minimized_last=True)
    assert [a.hwnd for a in ordered] == [2, 4, 1, 3]


def test_minimized_last_preserves_the_mru_order_within_each_group():
    tracker = ForegroundTracker()
    for hwnd in (4, 3, 2, 1):
        tracker.note(hwnd)
    apps = [app(1), app(2, minimized=True), app(3), app(4, minimized=True)]
    ordered = order_windows(
        apps, [1, 2, 3, 4], rank=tracker.rank, minimized_last=True
    )
    assert [a.hwnd for a in ordered] == [1, 3, 2, 4]


def test_ordering_does_not_mutate_the_input():
    apps = [app(2), app(1)]
    order_windows(apps, [1, 2], rank=lambda hwnd: hwnd)
    assert [a.hwnd for a in apps] == [2, 1]
