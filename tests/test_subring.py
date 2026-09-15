"""The sub-ring: overshoot the ring and the aimed app's windows fan out.

Angle picks the application, radial distance picks one of its windows. Distance
was previously measured and thrown away — `aim` used it only to reject the hub
dead zone — so this is the one input dimension the switcher had left.
"""

from __future__ import annotations

import math

import pytest
from PIL import Image

from fun_tab.config import Config
from fun_tab.overlay import Overlay
from fun_tab.wheel import build_fan
from fun_tab.windows_enum import AppWindow

CX = CY = 200.0
OUTER, INNER = 160.0, 50.0
TAU = 2 * math.pi


# -- fan geometry -----------------------------------------------------------


def fan(count: int, mid: float = math.pi / 2, slice_sweep: float = math.radians(17.5), **kw):
    return build_fan(count, CX, CY, OUTER * 1.34, OUTER * 1.07, mid, slice_sweep, **kw)


def sweep_of(geom) -> float:
    return (geom.start_cw - geom.end_cw) % TAU


def test_a_fan_entry_is_never_narrower_than_the_floor():
    """Confining the fan to its slice's own span would give a three-window app on
    a crowded wheel under six degrees per entry, which is not a flick target.
    The fan is legible and usable only because it overhangs.
    """
    layout = fan(3, slice_sweep=math.radians(17.5), min_deg=12.0)
    for geom in layout.slices:
        assert math.degrees(sweep_of(geom)) >= 12.0 - 1e-9


def test_the_fan_overhangs_its_slice():
    slice_sweep = math.radians(17.5)
    layout = fan(3, slice_sweep=slice_sweep)
    assert layout.span > slice_sweep


def test_the_fan_stays_centred_on_the_slice():
    """The direction still has to read as "that app", just further out."""
    mid = math.radians(200.0)
    layout = fan(4, mid=mid)
    centre = (layout.first_edge - layout.span / 2) % TAU
    assert centre == pytest.approx(mid, abs=1e-9)


def test_a_huge_fan_is_capped():
    """Twenty windows at the twelve-degree floor would be 240 degrees, which
    would wrap around and overlap itself.
    """
    layout = fan(20, max_deg=90.0)
    assert math.degrees(layout.span) == pytest.approx(90.0)


def test_aiming_outside_a_bounded_arc_selects_nothing():
    """A bounded arc has to be missable, or the fan would swallow every
    direction on screen and there would be no way back to the ring.
    """
    layout = fan(3, mid=math.pi / 2)
    behind = (CX, CY + 300.0)  # straight down, the far side of the wheel
    assert layout.aim(*behind, min_r=0.0) is None


def test_every_fan_entry_is_reachable():
    layout = fan(5, mid=math.radians(120.0))
    for geom in layout.slices:
        point = (CX + math.cos(geom.mid) * 240.0, CY - math.sin(geom.mid) * 240.0)
        assert layout.aim(*point, min_r=0.0) == geom.index


# -- overlay behaviour ------------------------------------------------------


def app(hwnd: int, title: str, exe: str = "app.exe", *, z_index: int = 0) -> AppWindow:
    return AppWindow(
        hwnd=hwnd,
        title=title,
        class_name="TestClass",
        pid=hwnd * 10,
        icon=Image.new("RGBA", (32, 32), (80, 140, 220, 255)),
        exe_path=rf"C:\apps\{exe}",
        app_name=exe.removesuffix(".exe").title(),
        z_index=z_index,
    )


APPS = [
    app(1, "notes.md", "obsidian.exe"),
    app(2, "Inbox", "chrome.exe", z_index=1),
    app(3, "Gmail", "chrome.exe", z_index=2),
    app(4, "Docs", "chrome.exe", z_index=3),
    app(5, "standup", "slack.exe"),
]


ANCHOR = (960, 540)


def wheel(**overrides) -> Overlay:
    """A live-looking overlay with the cursor parked at the screen centre.

    Aiming only runs while the overlay believes it is visible and has an anchor,
    which is what `show` records; nothing here touches Win32.
    """
    overrides.setdefault("preview_enabled", False)
    overrides.setdefault("group_by_app", True)
    overlay = Overlay(Config(**overrides))
    overlay._apply_metrics(96)
    overlay._all_apps = list(APPS)
    overlay._apps = overlay._present_apps()
    overlay._selected = 1  # the grouped Chrome slice
    overlay._rebuild_layers()
    overlay._visible = True
    overlay._wheel_origin = (
        ANCHOR[0] - overlay.canvas_w // 2,
        ANCHOR[1] - overlay.canvas_h // 2,
    )
    overlay._mouse_anchor = ANCHOR
    overlay._last_cursor = ANCHOR
    overlay._origin_at = ANCHOR
    overlay._origin_live = True
    return overlay


def aim_at(o: Overlay, index: int, distance: float) -> None:
    """Point at a slice's centre from ``distance * outer_r`` away from the hub."""
    geom = o._layers.layout.slice_for(index)
    radius = o.outer_r * distance
    ox, oy = o._wheel_origin
    o._cursor_at(
        (
            int(ox + o.wheel_cx + math.cos(geom.mid) * radius),
            int(oy + o.wheel_cy - math.sin(geom.mid) * radius),
        )
    )


def test_overshooting_a_grouped_slice_opens_its_fan():
    o = wheel()
    aim_at(o, 1, 0.9)
    assert o.expanded is None, "on the ring, this still just picks the app"
    aim_at(o, 1, 1.5)
    assert o.expanded == 1
    assert [a.title for a in o.fan_apps] == ["Inbox", "Gmail", "Docs"]


def test_pulling_back_closes_the_fan():
    o = wheel()
    aim_at(o, 1, 1.5)
    assert o.expanded == 1
    aim_at(o, 1, 0.8)
    assert o.expanded is None


def test_the_boundary_does_not_chatter():
    """One threshold would flicker the whole fan open and shut while the cursor
    rests near it, so entering and leaving use different radii.
    """
    o = wheel()
    enter = o.cfg.subring_enter
    exit_at = o.cfg.subring_exit
    assert exit_at < enter, "hysteresis, not a single threshold"

    between = (enter + exit_at) / 2
    aim_at(o, 1, between)
    assert o.expanded is None, "not far enough out to open"
    aim_at(o, 1, enter + 0.05)
    assert o.expanded == 1
    aim_at(o, 1, between)
    assert o.expanded == 1, "and not close enough back in to shut"


def test_the_expanded_app_is_latched_while_in_the_fan():
    """Out in the fan the angle is choosing a window. If it kept choosing the
    app as well, one movement would change both at once.
    """
    o = wheel()
    aim_at(o, 1, 1.5)
    assert o.expanded == 1
    # Sweep round to where a different slice sits, still far out.
    aim_at(o, 0, 1.5)
    assert o.expanded == 1, "still Chrome's fan"
    assert o.selected_index == 1


def test_a_slice_with_one_window_does_not_expand():
    """Overshooting must never be punished, so an app with nothing to fan simply
    stays selected.
    """
    o = wheel()
    aim_at(o, 0, 1.6)  # Obsidian, a single window
    assert o.expanded is None
    assert o.selected_index == 0


def test_the_fan_commits_the_window_it_is_pointing_at():
    o = wheel()
    aim_at(o, 1, 1.5)
    o._fan_selected = 2
    assert o.selected_app().title == "Docs"
    assert o.selected_slice().app_name == "Chrome", "the slice itself is unchanged"


def test_the_subring_can_be_turned_off():
    o = wheel(subring=False)
    aim_at(o, 1, 1.8)
    assert o.expanded is None
    assert o.selected_index == 1


# -- the "new window" entry -------------------------------------------------


def pinned_wheel(**overrides) -> Overlay:
    overrides.setdefault("preview_enabled", False)
    overrides.setdefault("group_by_app", True)
    cfg = Config(**overrides)
    cfg.set_slot_order(["chrome.exe", "steam.exe"])
    cfg.clamp()
    overlay = Overlay(cfg)
    overlay._apply_metrics(96)
    overlay._all_apps = list(APPS)
    overlay._apps = overlay._present_apps()
    overlay._rebuild_layers()
    return overlay


def test_a_pinned_slice_offers_a_new_window_last():
    """Only pinned slots get it: those are the ones with a recorded launch
    target, and spawning a window is what their slot is for.
    """
    o = pinned_wheel()
    chrome = next(i for i, a in enumerate(o.apps) if a.exe_name == "chrome.exe")
    entries = o._fan_entries(chrome)
    assert [a.title for a in entries] == ["Inbox", "Gmail", "Docs", "New window"]
    assert entries[-1].hwnd == 0, "nothing to switch to yet"


def test_a_closed_pin_fans_to_exactly_one_entry():
    """Launching is then not a separate path, just a fan of length one."""
    o = pinned_wheel()
    steam = next(i for i, a in enumerate(o.apps) if a.is_dead)
    entries = o._fan_entries(steam)
    assert len(entries) == 1
    assert entries[0].hwnd == 0


def test_an_unpinned_group_has_no_new_window_entry():
    o = wheel()
    assert [a.title for a in o._fan_entries(1)] == ["Inbox", "Gmail", "Docs"]


def test_the_new_window_entry_can_be_turned_off():
    o = pinned_wheel(subring_new_window=False)
    chrome = next(i for i, a in enumerate(o.apps) if a.exe_name == "chrome.exe")
    assert [a.title for a in o._fan_entries(chrome)] == ["Inbox", "Gmail", "Docs"]


# -- keyboard parity --------------------------------------------------------


def test_backtick_steps_the_fan_without_moving_the_slice():
    o = wheel()
    o.cycle_same_app(1)
    assert o.expanded == 1
    assert o.selected_app().title == "Gmail"
    o.cycle_same_app(1)
    assert o.selected_app().title == "Docs"
    o.cycle_same_app(1)
    assert o.selected_app().title == "Inbox", "wraps inside the fan"
    assert o.selected_index == 1, "and never leaves the slice"


def test_moving_to_another_slice_closes_the_fan():
    """Aiming elsewhere is a statement about which app, so a fan open on the old
    slice is answering a question nobody is asking any more.
    """
    o = wheel()
    o.cycle_same_app(1)
    assert o.expanded == 1
    o.cycle(1)
    assert o.expanded is None


def test_searching_closes_the_fan():
    o = wheel(search_enabled=True)
    o.cycle_same_app(1)
    o.type_query("standup")
    assert o.expanded is None
