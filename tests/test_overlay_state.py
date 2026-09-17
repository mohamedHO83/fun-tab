"""Overlay navigation, search and rendering, without creating any windows.

The Overlay only touches Win32 in `create()`/`show()`, so everything below
runs against a plain instance with the app list injected.
"""

from __future__ import annotations

import math
import time

import pytest
from PIL import Image, ImageChops

from fun_tab.config import Config
from fun_tab.overlay import Overlay
from fun_tab.windows_enum import AppWindow


def app(hwnd: int, title: str, exe: str = "app.exe", *, minimized: bool = False) -> AppWindow:
    icon = Image.new("RGBA", (32, 32), (80, 140, 220, 255))
    return AppWindow(
        hwnd=hwnd,
        title=title,
        class_name="TestClass",
        pid=hwnd * 10,
        icon=icon,
        exe_path=rf"C:\apps\{exe}",
        app_name=exe.removesuffix(".exe").title(),
        minimized=minimized,
    )


APPS = [
    app(1, "notes.md", "obsidian.exe"),
    app(2, "Inbox", "chrome.exe"),
    app(3, "Gmail", "chrome.exe"),
    app(4, "standup", "slack.exe"),
    app(5, "Now Playing", "spotify.exe", minimized=True),
]


def wheel(apps=APPS, selected: int = 0, **overrides) -> Overlay:
    # Previews off by default: capturing fake hwnds would spin up a worker
    # thread for nothing.
    overrides.setdefault("preview_enabled", False)
    overrides.setdefault("group_by_app", False)
    cfg = Config(**overrides)
    overlay = Overlay(cfg)
    overlay._apply_metrics(96)
    overlay._all_apps = list(apps)
    overlay._apps = list(apps)
    overlay._selected = selected
    overlay._rebuild_layers()
    return overlay


def titles(overlay: Overlay) -> list[str]:
    return [a.title for a in overlay.apps]


# -- navigation -------------------------------------------------------------


def test_cycle_moves_forward_and_backward():
    o = wheel(selected=1)
    o.cycle(1)
    assert o.selected_index == 2
    o.cycle(-1)
    assert o.selected_index == 1


def test_cycle_wraps_by_default():
    o = wheel(selected=len(APPS) - 1)
    o.cycle(1)
    assert o.selected_index == 0
    o.cycle(-1)
    assert o.selected_index == len(APPS) - 1


def test_cycle_can_be_clamped_instead():
    o = wheel(selected=len(APPS) - 1, wrap_navigation=False)
    o.cycle(1)
    assert o.selected_index == len(APPS) - 1
    o.first()
    o.cycle(-1)
    assert o.selected_index == 0


def test_first_and_last():
    o = wheel(selected=2)
    o.last()
    assert o.selected_index == len(APPS) - 1
    o.first()
    assert o.selected_index == 0


def test_select_clamps_out_of_range_indices():
    o = wheel()
    o.select(999)
    assert o.selected_index == len(APPS) - 1
    o.select(-5)
    assert o.selected_index == 0


def test_jump_uses_one_based_numbers():
    o = wheel()
    assert o.jump(3) is True
    assert o.selected_index == 2
    assert o.jump(9) is False, "only five windows are open"
    assert o.selected_index == 2, "a failed jump must not move the selection"


def test_jump_zero_means_the_tenth_slice():
    o = wheel(apps=[app(i, f"win{i}") for i in range(1, 11)])
    assert o.jump(0) is True
    assert o.selected_index == 9


def test_navigation_on_an_empty_wheel_is_harmless():
    o = wheel(apps=[])
    o.cycle(1)
    o.first()
    o.last()
    assert o.jump(1) is False
    assert o.selected_app() is None


def test_single_app_cycle_stays_put():
    o = wheel(apps=APPS[:1])
    o.cycle(1)
    assert o.selected_index == 0


def test_cycle_follows_the_wheel_when_an_app_is_pinned():
    """Opera GX in the lane sits at six o'clock. Tab has to pass through it
    rather than jumping from one side of the free arc to the other.
    """
    apps = [
        app(1, "notes.md", "obsidian.exe"),
        app(2, "Inbox", "chrome.exe"),
        app(3, "standup", "slack.exe"),
        app(4, "Now Playing", "spotify.exe"),
        app(5, "GX", "opera.exe"),
    ]
    o = wheel(apps, selected=0, pin_lane=True)
    o.cfg.set_slot_order(["opera.exe"])
    o._apps = o._present_apps()
    o._selected = 0
    seen = [0]
    for _ in range(len(o.apps) - 1):
        o.cycle(1)
        seen.append(o.selected_index)
    ring = o._ring_for(*o._counts())
    assert tuple(seen) == ring.clockwise_indices()
    pin = next(i for i, a in enumerate(o.apps) if a.exe_name.lower() == "opera.exe")
    assert o.apps[pin].is_pinned
    order = list(ring.clockwise_indices())
    o._selected = order[order.index(pin) - 1]
    o.cycle(1)
    assert o.selected_index == pin


# -- same-app cycling ------------------------------------------------------


def test_cycle_same_app_visits_only_that_apps_windows():
    o = wheel(selected=1)  # Chrome "Inbox"
    o.cycle_same_app(1)
    assert o.selected_app().title == "Gmail"
    o.cycle_same_app(1)
    assert o.selected_app().title == "Inbox", "wraps within the app"


def test_cycle_same_app_does_nothing_for_a_lone_window():
    o = wheel(selected=0)  # Obsidian, only one window
    o.cycle_same_app(1)
    assert o.selected_index == 0


def test_grouping_collapses_chrome_into_one_slice():
    o = wheel(group_by_app=True)
    o._apps = o._present_apps()
    assert [a.title for a in o.apps] == ["notes.md", "Inbox", "standup", "Now Playing"]
    chrome = o.apps[1]
    assert chrome.group_count == 2
    assert chrome.peer_hwnds == (2, 3)


def test_grouped_backtick_steps_the_fan_and_leaves_the_slice_alone():
    """`` ` `` moves within the app's windows without disturbing the wheel.

    It used to rotate which peer was the group's face, which meant a grouped
    slice changed its label underneath the user on every cycle. The fan owns
    window choice now, so slice identity stays put and stays learnable.
    """
    o = wheel(group_by_app=True)
    o._apps = o._present_apps()
    o._selected = 1
    assert o.selected_app().title == "Inbox"
    o.cycle_same_app(1)
    assert o.selected_app().title == "Gmail"
    assert o.expanded == 1, "stepped outward into Chrome's fan"
    assert [a.title for a in o.apps] == ["notes.md", "Inbox", "standup", "Now Playing"]
    o.cycle_same_app(1)
    assert o.selected_app().title == "Inbox", "wraps within the fan"
    assert o.selected_index == 1, "and never leaves the slice"


def test_search_ungroups_so_a_title_can_be_picked():
    o = wheel(group_by_app=True)
    o._apps = o._present_apps()
    o.type_query("gmail")
    assert titles(o) == ["Gmail"]
    o.clear_query()
    assert [a.title for a in o.apps] == ["notes.md", "Inbox", "standup", "Now Playing"]


def test_drop_exe_removes_every_window_of_that_app():
    o = wheel(group_by_app=True)
    o._apps = o._present_apps()
    o._selected = 1
    assert o.drop_exe("chrome.exe") is True
    assert all("chrome" not in a.exe_name.lower() for a in o.apps)
    assert "Inbox" not in titles(o)


# -- search ----------------------------------------------------------------


def test_typing_filters_the_wheel():
    o = wheel()
    o.type_query("gm")
    assert titles(o) == ["Gmail"]
    assert o.selected_index == 0
    assert o.query == "gm"


def test_search_matches_the_application_name_too():
    o = wheel()
    o.type_query("chrome")
    assert titles(o) == ["Inbox", "Gmail"]


def test_search_is_case_insensitive():
    o = wheel()
    o.type_query("INBOX".lower())
    assert titles(o) == ["Inbox"]


def test_filtering_keeps_the_selected_window_selected():
    o = wheel(selected=2)  # Gmail
    o.type_query("chrome")
    assert o.selected_app().title == "Gmail", "the highlight should not jump away"


def test_backspace_restores_the_wider_list():
    o = wheel()
    o.type_query("g")
    o.type_query("m")
    assert titles(o) == ["Gmail"]
    o.backspace_query()
    assert o.query == "g"
    assert len(o.apps) > 1


def test_clearing_the_query_restores_everything():
    o = wheel()
    o.type_query("gmail")
    o.clear_query()
    assert o.query == ""
    assert titles(o) == titles(wheel())


def test_a_query_with_no_matches_keeps_the_last_good_list():
    o = wheel()
    o.type_query("chrome")
    o.type_query("zzz")
    assert o.query == "chromezzz"
    assert titles(o) == ["Inbox", "Gmail"], "the wheel must not blank out mid-type"
    assert o._query_matched is False


def test_backspacing_out_of_a_dead_end_matches_again():
    o = wheel()
    for char in "chromez":
        o.type_query(char)
    assert o._query_matched is False
    o.backspace_query()
    assert o._query_matched is True
    assert titles(o) == ["Inbox", "Gmail"]


def test_backspace_on_an_empty_query_is_a_no_op():
    o = wheel()
    o.backspace_query()
    assert o.query == ""
    assert len(o.apps) == len(APPS)


# -- closing ---------------------------------------------------------------


def test_closing_removes_the_window_and_returns_its_handle():
    o = wheel(selected=1)
    assert o.request_close_selected() == 2
    assert titles(o) == ["notes.md", "Gmail", "standup", "Now Playing"]


def test_closing_the_last_slice_moves_the_selection_back():
    o = wheel(selected=len(APPS) - 1)
    o.request_close_selected()
    assert o.selected_index == len(APPS) - 2
    assert o.selected_app().title == "standup"


def test_closing_also_forgets_the_window_for_searches():
    o = wheel(selected=1)
    o.request_close_selected()
    o.type_query("inbox")
    assert o._query_matched is False, "a closed window must not come back via search"


def test_closing_the_only_window_cancels_the_wheel():
    cancelled = []
    o = wheel(apps=APPS[:1])
    o.set_callbacks(on_commit=lambda: None, on_cancel=lambda: cancelled.append(True))
    assert o.request_close_selected() == 1
    assert cancelled == [True]


def test_closing_an_empty_wheel_returns_nothing():
    assert wheel(apps=[]).request_close_selected() == 0


# -- rendering -------------------------------------------------------------


@pytest.mark.parametrize("count", [1, 2, 3, 5, 9, 14])
def test_compose_renders_any_number_of_apps(count):
    o = wheel(apps=[app(i, f"window {i}") for i in range(1, count + 1)], selected=0)
    frame = o._compose(time.perf_counter())
    assert frame.size == (o.canvas_w, o.canvas_h)
    assert frame.mode == "RGBA"
    assert frame.getbbox() is not None, "the wheel should not render blank"


def test_settled_frames_are_served_from_cache():
    o = wheel(selected=1)
    now = time.perf_counter()
    first = o._compose(now)
    assert o._compose(now) is first, "a static wheel must not repaint"


def test_transition_ends_on_the_target_frame():
    o = wheel(selected=1)
    settled = o._compose(time.perf_counter())
    o.select(2)
    o._compose(time.perf_counter())  # mid-crossfade: a fresh blend
    later = time.perf_counter() + o.cfg.transition_duration + 0.01
    assert o._compose(later) is not settled
    assert o._compose(later) is o._compose(later), "back to a cached frame"


def test_search_and_close_rebuild_the_layers():
    o = wheel()
    before = o._layers
    o.type_query("chrome")
    assert o._layers is not before
    after_query = o._layers
    o.request_close_selected()
    assert o._layers is not after_query


def test_higher_dpi_produces_a_bigger_canvas():
    o = wheel()
    small = (o.canvas_w, o.canvas_h)
    o._apply_metrics(192)
    assert (o.canvas_w, o.canvas_h) > small


def test_reconfiguring_drops_the_cached_pixels():
    o = wheel()
    o._compose(time.perf_counter())
    assert o._layer_cache
    o.apply_config(Config(preview_enabled=False, theme="light"))
    assert not o._layer_cache
    assert o.theme.is_dark is False


def test_theme_change_does_not_serve_stale_frames():
    o = wheel()
    dark_frame = o._compose(time.perf_counter())
    o.apply_config(Config(preview_enabled=False, theme="light"))
    o._rebuild_layers()
    light_frame = o._compose(time.perf_counter())
    assert light_frame.tobytes() != dark_frame.tobytes()


def test_preview_card_is_built_and_reused():
    o = wheel(selected=1, preview_enabled=True)
    card = o._build_card(APPS[1])
    assert card.size == (
        o.preview_w + o.preview_pad * 2,
        o.preview_h + o.preview_pad * 2,
    )
    assert o._placeholder_card(APPS[1]) is o._placeholder_card(APPS[1])


# -- mouse aiming -----------------------------------------------------------
#
# `_aim_from` is fed screen coordinates directly, the same as the frame loop
# does, so none of this needs a cursor or a window.


class Pointer:
    """The physical cursor, which the overlay only learns about by polling.

    Kept separate from the overlay's own state on purpose: the pointer does
    not move just because the keyboard was used, and that gap is where the
    override bug lived.
    """

    def __init__(self, overlay: Overlay, pos: tuple[int, int]):
        self.overlay = overlay
        self.pos = pos

    def flick(self, dx: int, dy: int) -> None:
        self.pos = (self.pos[0] + dx, self.pos[1] + dy)
        self.overlay._cursor_at(self.pos)

    def move_to(self, pos: tuple[int, int]) -> None:
        self.pos = pos
        self.overlay._cursor_at(pos)

    def frames(self, count: int = 8) -> None:
        """Let the frame loop run with the pointer sitting exactly where it is."""
        for _ in range(count):
            self.overlay._cursor_at(self.pos)


def aiming(selected: int = 0, at=(1500, 900), **overrides) -> tuple[Overlay, Pointer]:
    """A visible wheel centred on screen, with the pointer parked off to one side."""
    o = wheel(selected=selected, **overrides)
    o._visible = True
    o._wheel_origin = (960 - o.canvas_w // 2, 540 - o.canvas_h // 2)
    o._mouse_anchor = at
    o._last_cursor = at
    # What `show` records: the cursor's position at open is both the anchor to
    # aim from and the spot the marker sits on.
    o._origin_at = at
    o._origin_live = True
    return o, Pointer(o, at)


CORNERS = [(1500, 900), (60, 1000), (1850, 40), (300, 300)]


def test_flicking_up_selects_the_top_slice_from_anywhere():
    """Slice 0 sits at 12 o'clock, and up must mean up wherever the cursor is."""
    for at in CORNERS + [(960, 540)]:
        o, pointer = aiming(selected=2, at=at)
        pointer.flick(0, -200)
        assert o._selected == 0, f"aiming up from {at}"


def test_flick_direction_maps_to_slice_from_any_corner():
    """The same gesture has to pick the same app regardless of cursor position."""
    gestures = {(0, -200): 0, (200, 0): 1, (0, 200): 3, (-200, 0): 4}
    for delta, expected in gestures.items():
        picks = set()
        for at in CORNERS:
            o, pointer = aiming(selected=2, at=at)
            pointer.flick(*delta)
            picks.add(o._selected)
        assert picks == {expected}, f"flick {delta} picked {picks}"


def test_a_tiny_twitch_changes_nothing():
    o, pointer = aiming(selected=2)
    pointer.flick(3, -3)
    assert o._selected == 2


def on_wheel(o: Overlay, offset_x: float, offset_y: float) -> tuple[int, int]:
    """A screen point at a given offset from the wheel's hub."""
    layout = o._layers.layout
    ox, oy = o._wheel_origin
    return (int(ox + layout.cx + offset_x), int(oy + layout.cy + offset_y))


def test_pointing_at_the_ring_selects_what_is_under_the_cursor():
    """Near the wheel the pointer is literal: absolute position wins."""
    o, pointer = aiming(selected=3)
    pointer.move_to(on_wheel(o, 0, -o._layers.layout.outer_r * 0.8))  # above the hub
    assert o._selected == 0


def test_the_hub_is_a_dead_zone():
    o, pointer = aiming(selected=2)
    pointer.move_to(on_wheel(o, 2, 2))
    assert o._selected == 2, "resting on the hub must not pick anything"


# -- mouse versus keyboard --------------------------------------------------


def test_a_still_mouse_does_not_override_the_keyboard():
    """The reported bug: Tab was undone by the next frame re-reading the cursor."""
    o, pointer = aiming(selected=0)
    pointer.flick(200, 0)  # take control with the mouse
    assert o._selected == 1

    o.cycle(1)
    assert o._selected == 2
    pointer.frames(10)  # frames pass, pointer untouched
    assert o._selected == 2, "a stationary cursor kept overriding the keyboard"


def test_a_twitch_does_not_undo_a_keyboard_pick():
    o, pointer = aiming(selected=0)
    pointer.flick(200, 0)
    o.cycle(2)
    assert o._selected == 3
    pointer.frames(2)  # the loop re-reads the pointer where it now rests
    pointer.flick(4, 4)  # nudged the desk
    assert o._selected == 3


def test_a_deliberate_flick_takes_control_back():
    o, pointer = aiming(selected=0)
    o.cycle(1)
    pointer.frames(2)
    pointer.flick(0, -200)
    assert o._selected == 0, "up should still aim at the top slice"


def test_scrolling_counts_as_a_discrete_pick():
    """The scroll wheel is keyboard-like: it must not be re-aimed away."""
    o, pointer = aiming(selected=0)
    pointer.flick(200, 0)
    o.cycle(1)  # what WM_MOUSEWHEEL does
    settled = o._selected
    pointer.frames()
    assert o._selected == settled


def test_searching_releases_the_pointer():
    o, pointer = aiming(selected=0)
    pointer.flick(200, 0)
    o.type_query("chrome")
    picked = o._selected
    pointer.frames()
    assert o._selected == picked


# -- aim needle -------------------------------------------------------------


def _dib_bytes(surface) -> bytes:
    """The surface's pixels as premultiplied BGRA, the way GDI holds them."""
    return surface._view.tobytes()


def test_the_needle_points_where_the_cursor_aims():
    o, pointer = aiming(selected=0)
    pointer.flick(0, -200)  # up
    assert o._aim_angle == pytest.approx(math.pi / 2, abs=math.radians(2))
    pointer.flick(200, 200)  # now pointing right
    assert o._aim_angle == pytest.approx(0.0, abs=math.radians(2))


def test_the_needle_can_be_switched_off_without_breaking_aiming():
    o, pointer = aiming(selected=0, aim_needle=False)
    pointer.flick(0, -200)
    assert o._aim_angle is None, "the needle was switched off"
    assert o._selected == 0, "up is slice 0, so aiming itself still worked"
    pointer.flick(200, 200)
    assert o._selected != 0, "flicking right still moves the selection"
    assert o._aim_angle is None


def test_the_needle_appears_only_while_aiming():
    o, pointer = aiming(selected=0)
    assert o._aim_angle is None, "nothing aimed yet at open"
    pointer.flick(0, -200)
    assert o._aim_angle is not None

    o.cycle(1)  # keyboard takes over
    assert o._aim_angle is None, "the needle must not linger after a keypress"


def test_the_needle_is_hidden_in_the_hub_dead_zone():
    o, pointer = aiming(selected=0)
    pointer.flick(0, -200)
    assert o._aim_angle is not None
    pointer.move_to(on_wheel(o, 1, 1))  # back onto the hub
    assert o._aim_angle is None


def test_sub_degree_jitter_does_not_force_a_repaint():
    o, pointer = aiming(selected=0)
    pointer.flick(300, 0)
    o._dirty = False
    settled = o._aim_angle
    pointer.flick(0, 1)  # a hair of rotation, well inside one step
    assert o._aim_angle == settled
    assert o._dirty is False


@pytest.mark.parametrize("count", [2, 3, 5, 8, 12, 20])
@pytest.mark.parametrize("dpi", [96, 120, 192])
def test_the_needle_never_reaches_the_icons(count, dpi):
    """It sits in the gap between hub and icons, which must never collapse."""
    o = wheel(apps=APPS[:1] * count if count > len(APPS) else APPS[:count])
    o._apply_metrics(dpi)
    o._rebuild_layers()
    start, end = o._needle_span()
    icon_inner = (o.inner_r + o.outer_r) * 0.52 - o._icon_size(1) / 2

    assert start >= o.inner_r, "the needle must not cross into the hub"
    assert end > start, "degenerate needle"
    assert end <= icon_inner, "the needle would deface the icon it points at"


def test_the_needle_fits_inside_the_box_that_gets_patched():
    """Every angle must land inside the box, or stale needles would be left behind."""
    o, _ = aiming(selected=1)
    frame = o._compose(time.perf_counter())
    x0, y0, x1, y1 = o._needle_box()

    for deg in range(0, 360, 7):
        marked = frame.copy()
        o._draw_needle(marked, math.radians(deg))
        # alpha_only is Pillow's default and would only notice the needle
        # where it changed opacity, not where it changed colour.
        touched = ImageChops.difference(marked, frame).getbbox(alpha_only=False)
        assert touched is not None, f"nothing drawn at {deg} degrees"
        assert x0 <= touched[0] and y0 <= touched[1], (deg, touched)
        assert touched[2] <= x1 and touched[3] <= y1, (deg, touched)


def test_patching_the_needle_matches_a_full_repaint():
    """The cheap path must produce the same pixels as drawing the whole frame."""
    o, _ = aiming(selected=1)
    frame = o._compose(time.perf_counter())
    o._wheel.ensure(o.canvas_w, o.canvas_h)
    o._wheel.write(frame)

    # Two different angles in a row: the second must erase the first.
    for angle in (math.radians(200), math.radians(20)):
        o._patch_needle(frame, angle)
        expected = frame.copy()
        o._draw_needle(expected, angle)
        assert _dib_bytes(o._wheel) == expected.tobytes("raw", "BGRa")

    # And clearing it must restore the untouched frame.
    o._patch_needle(frame, None)
    assert _dib_bytes(o._wheel) == frame.tobytes("raw", "BGRa")


# -- origin marker ----------------------------------------------------------


def test_the_origin_marks_where_the_wheel_opened():
    o, _ = aiming(selected=0, at=(1700, 950))
    assert o._origin_at == (1700, 950)
    assert o._origin_live is True


def test_the_origin_stays_put_but_dims_when_the_keyboard_takes_over():
    """It answers "where is my aim measured from", which a keypress must not erase."""
    o, _ = aiming(selected=0, at=(1700, 950))
    o.cycle(1)
    assert o._origin_at == (1700, 950), "the marker must not vanish on a keypress"
    assert o._origin_live is False
    assert o._mouse_anchor is None, "but the anchor itself is released"


def test_the_origin_moves_to_wherever_the_mouse_takes_over_again():
    o, pointer = aiming(selected=0, at=(1700, 950))
    o.cycle(1)  # keyboard drives, anchor released
    pointer.move_to((400, 300))  # mouse speaks again: re-anchors here
    assert o._origin_at == (400, 300)
    assert o._origin_live is True


def test_the_origin_is_forgotten_when_the_wheel_closes():
    o, _ = aiming(selected=0)
    o.hide()
    assert o._origin_at is None
    assert o._origin_live is False


def test_the_origin_marker_is_drawn_hollow():
    """It lands on unknown wallpaper, so the middle has to stay see-through."""
    o = wheel()
    plate = o._origin_plate(True)
    assert plate.size == (o._origin_size, o._origin_size)
    corner = plate.getpixel((0, 0))
    assert corner[3] == 0, "the marker must not be a filled square"

    ring = max(plate.getpixel((x, plate.height // 2))[3] for x in range(plate.width))
    assert ring > 200, "the ring itself should be close to opaque"


def test_a_dimmed_origin_marker_is_fainter_than_a_live_one():
    o = wheel()
    live = max(p[3] for p in o._origin_plate(True).getdata())
    idle = max(p[3] for p in o._origin_plate(False).getdata())
    assert idle < live


def test_switching_the_origin_marker_off_hides_it():
    off, _ = aiming(selected=0, at=(1700, 950), aim_origin=False)
    on, _ = aiming(selected=0, at=(1700, 950))
    for o in (off, on):
        o._origin_hwnd = 1  # no real window; presenting still records its decision
        o._origin_shown = ()
        o._present_origin()

    assert off._origin_shown[0] is None, "nothing should be positioned to show"
    assert on._origin_shown[0] == (1700, 950)


def test_the_marker_window_is_excluded_from_the_window_list():
    """Otherwise the wheel would offer you one of its own windows."""
    o = wheel()
    o._dim_hwnd, o.hwnd, o._preview_hwnd, o._origin_hwnd = 11, 12, 13, 14
    assert set(o.own_hwnds()) == {11, 12, 13, 14}


# -- backdrop ---------------------------------------------------------------
#
# The capture itself needs a screen, so these cover the decisions around it:
# what counts as reusable, and that an open never triggers a screen read that
# could catch the overlay in its own backdrop.

RECT = (0, 0, 1920, 1080)


def plated(o: Overlay, rect=RECT, age: float = 0.0) -> Image.Image:
    plate = Image.new("RGBA", (320, 180), (20, 30, 40, 255))
    o._plate = plate
    o._plate_rect = rect
    o._plate_at = time.perf_counter() - age
    return plate


def test_a_recent_plate_is_reused():
    o = wheel()
    plate = plated(o, age=0.2)
    assert o._resolve_plate(RECT) is plate


def test_a_plate_for_another_monitor_is_not_reused(monkeypatch):
    o = wheel()
    plated(o, rect=(1920, 0, 3840, 1080))
    monkeypatch.setattr(o, "_capture_plate", lambda rect: None)
    assert o._resolve_plate(RECT) is None


def test_an_expired_plate_is_recaptured(monkeypatch):
    o = wheel(backdrop_ttl=1.0)
    plated(o, age=5.0)
    fresh = Image.new("RGBA", (320, 180), (1, 2, 3, 255))
    monkeypatch.setattr(o, "_capture_plate", lambda rect: fresh)
    assert o._resolve_plate(RECT) is fresh


def test_prepare_is_skipped_while_the_wheel_is_up(monkeypatch):
    """Capturing with the overlay on screen would blur the wheel into itself."""
    o = wheel()
    started = []
    monkeypatch.setattr(o, "_update_monitor", lambda: None)
    monkeypatch.setattr(o, "_capture_plate", lambda rect: started.append(rect))
    o._visible = True
    o.prepare_backdrop()
    assert started == []


def test_prepare_does_nothing_without_a_backdrop(monkeypatch):
    o = wheel(backdrop="none")
    monkeypatch.setattr(o, "_update_monitor", lambda: None)
    monkeypatch.setattr(
        o, "_capture_plate", lambda rect: pytest.fail("captured for backdrop=none")
    )
    o.prepare_backdrop()
    assert o._capture_thread is None


def test_dim_mode_shares_the_plate_path():
    """dim is the blur path minus the blur, not a layered veil."""
    for mode in ("blur", "dim"):
        o = wheel(backdrop=mode)
        plate = plated(o)
        o._monitor = RECT
        assert o._present_backdrop(0, 0, 1920, 1080) is True
        assert o._backdrop_plate is plate


def test_none_mode_keeps_the_backdrop_window_hidden():
    o = wheel(backdrop="none")
    plated(o)
    assert o._present_backdrop(0, 0, 1920, 1080) is False
    assert o._backdrop_plate is None
