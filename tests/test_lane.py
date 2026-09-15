"""The pinned lane: fixed angles at the bottom, elastic MRU slices around them.

The lane's entire value is that a given app is always in the same direction, so
these tests are mostly about what must *not* move.
"""

from __future__ import annotations

import math

import pytest

from fun_tab.wheel import LANE_CENTER, MAX_LANE_SPAN, build_layout, build_wheel

CX = CY = 200.0
OUTER, INNER = 160.0, 50.0
TAU = 2 * math.pi


def ring(mru: int, pins: int, **kwargs):
    return build_wheel(mru, pins, CX, CY, OUTER, INNER, **kwargs)


def at(angle: float, radius: float = 110.0) -> tuple[float, float]:
    """A point ``radius`` from the hub, at a math-convention angle."""
    return (CX + math.cos(angle) * radius, CY - math.sin(angle) * radius)


def sweep_of(slice_geom) -> float:
    return (slice_geom.start_cw - slice_geom.end_cw) % TAU


UP = math.pi / 2


# -- no lane: nothing changes ----------------------------------------------


@pytest.mark.parametrize("count", [1, 2, 3, 6, 9, 12])
def test_no_pins_is_byte_identical_to_the_old_wheel(count):
    """The regression guard: an existing user's wheel is untouched until they pin."""
    plain = build_layout(count, CX, CY, OUTER, INNER)
    laneless = ring(count, 0)
    assert laneless.lane_count == 0
    assert laneless.mru.slices == plain.slices
    assert laneless.mru.sweep == plain.sweep
    assert laneless.bands == (), "one full disc, not two arcs"


# -- straight up ------------------------------------------------------------


@pytest.mark.parametrize("mru", range(1, 13))
@pytest.mark.parametrize("pins", [0, 1, 2, 3, 5, 8])
def test_straight_up_always_picks_the_first_slice(mru, pins):
    """The wheel opens with the cursor at the hub, so 12 o'clock is the one
    direction the user gets for free. It has to be a slice centre for every
    count, never the seam between the newest and oldest window.
    """
    assert ring(mru, pins).aim(*at(UP)) == 0


@pytest.mark.parametrize("mru", range(1, 13))
def test_the_first_slice_is_centred_on_twelve_oclock(mru):
    first = ring(mru, 5).mru.slices[0]
    assert first.mid == pytest.approx(UP, abs=1e-9)


# -- lane geometry ----------------------------------------------------------


@pytest.mark.parametrize("pins", range(1, 9))
def test_the_lane_never_takes_more_than_half_the_wheel(pins):
    """Five 30-degree slots is 150 degrees. Left unbounded, a wheel of twelve
    windows and eight closed pins would give the closed apps wider targets than
    the windows actually in use, which is backwards.
    """
    lane = ring(12, pins).lane
    assert sum(sweep_of(s) for s in lane.slices) <= MAX_LANE_SPAN + 1e-9


def test_a_slot_keeps_its_angle_however_many_windows_are_open():
    """The whole point. If the angle moved with the window count, a pinned app
    would be no more findable than an unpinned one.
    """
    angles = [
        tuple(round(s.mid, 9) for s in ring(mru, 4).lane.slices) for mru in (1, 2, 6, 12)
    ]
    assert len(set(angles)) == 1


def test_the_lane_is_centred_on_six_oclock():
    lane = ring(6, 4).lane
    mids = [s.mid for s in lane.slices]
    span = sum(sweep_of(s) for s in lane.slices)
    assert min(mids) == pytest.approx(LANE_CENTER - span / 2 + lane.sweep / 2)
    assert max(mids) == pytest.approx(LANE_CENTER + span / 2 - lane.sweep / 2)


def test_lane_indices_continue_after_the_mru_ones():
    """One flat index space, so selection, Tab and the digit jumps stay ignorant
    of the lane's existence.
    """
    layout = ring(4, 3)
    assert [s.index for s in layout.mru.slices] == [0, 1, 2, 3]
    assert [s.index for s in layout.lane.slices] == [4, 5, 6]
    assert layout.count == 7
    assert layout.is_lane(4) and not layout.is_lane(3)


def test_aiming_into_the_lane_returns_a_lane_index():
    layout = ring(6, 3)
    hit = layout.aim(*at(LANE_CENTER))
    assert hit is not None and layout.is_lane(hit)


def test_the_gap_belongs_to_neither_region():
    """A gap that silently selected its neighbour would be a lie about where the
    two regions end.
    """
    layout = ring(6, 4, gap_deg=10.0)
    span = sum(sweep_of(s) for s in layout.lane.slices)
    inside_gap = LANE_CENTER + span / 2 + math.radians(5.0)
    assert layout.lane.aim(*at(inside_gap)) is None
    assert layout.mru.aim(*at(inside_gap)) is None


# -- the free arc -----------------------------------------------------------


@pytest.mark.parametrize("mru", [1, 2, 6, 12])
def test_every_mru_slice_is_reachable_by_aiming_at_it(mru):
    layout = ring(mru, 5)
    for geom in layout.mru.slices:
        assert layout.aim(*at(geom.mid)) == geom.index


@pytest.mark.parametrize("mru", [1, 2, 6, 12])
def test_every_lane_slot_is_reachable_by_aiming_at_it(mru):
    layout = ring(mru, 5)
    for geom in layout.lane.slices:
        assert layout.aim(*at(geom.mid)) == geom.index


def test_an_even_window_count_leaves_one_slot_unfilled():
    """Rounding the slot count up is what keeps 12 o'clock a slice centre; the
    spare slot lands beside slice 0, reading as the list's start rather than as a
    missing tooth.
    """
    layout = ring(6, 4)
    assert -1 in layout.mru.slot_index
    assert len(layout.mru.slices) == 6
    assert sum(1 for i in layout.mru.slot_index if i >= 0) == 6


def test_an_odd_window_count_fills_the_free_arc_exactly():
    layout = ring(7, 4)
    assert -1 not in layout.mru.slot_index
    assert len(layout.bands) == 2, "one MRU band plus the lane"


def test_the_unfilled_slot_paints_as_a_gap():
    """It must not get ring fill, or it reads as a slice holding nothing."""
    layout = ring(6, 4)
    assert len(layout.bands) == 3, "the free arc is split in two, plus the lane"


def test_pins_shrink_the_arc_the_windows_share():
    plain = ring(8, 0).mru.sweep
    crowded = ring(8, 5).mru.sweep
    assert crowded < plain, "the lane's arc has to come from somewhere"


def test_all_pinned_and_nothing_else_still_lays_out():
    layout = ring(0, 3)
    assert layout.mru_count == 0
    assert layout.count == 3
    assert layout.aim(*at(LANE_CENTER)) == 1, "six o'clock is the middle of three slots"


def test_no_slices_at_all_is_not_an_error():
    layout = ring(0, 0)
    assert layout.count == 0
    assert layout.aim(*at(UP)) is None
