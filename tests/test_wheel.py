"""Wheel geometry: slice layout, aiming and hit testing."""

from __future__ import annotations

import math

import pytest

from fun_tab.wheel import build_layout, pie_points

CX, CY = 200.0, 200.0
OUTER, INNER = 160.0, 50.0


def layout(count: int):
    return build_layout(count, CX, CY, OUTER, INNER)


def point_at(angle: float, radius: float) -> tuple[float, float]:
    """A screen-space point at a math-convention angle (0 = east, CCW)."""
    return (CX + math.cos(angle) * radius, CY - math.sin(angle) * radius)


def test_empty_layout_has_no_slices():
    assert layout(0).slices == ()


@pytest.mark.parametrize("count", [1, 2, 3, 5, 8, 17])
def test_slices_cover_the_ring_exactly_once(count):
    slices = layout(count).slices
    assert len(slices) == count
    spans = [(sl.start_cw - sl.end_cw) % (2 * math.pi) or 2 * math.pi for sl in slices]
    assert sum(spans) == pytest.approx(2 * math.pi)
    assert max(spans) == pytest.approx(min(spans))


def test_first_slice_is_centered_at_the_top():
    # Straight up must select slice 0, so Tab from the top feels predictable.
    assert layout(6).slices[0].mid == pytest.approx(math.pi / 2)
    assert layout(6).aim(*point_at(math.pi / 2, 120)) == 0


def test_slices_advance_clockwise():
    # Tab advances clockwise, so slice 1 sits to the right of slice 0.
    picked = layout(4).aim(*point_at(0.0, 120))  # due east
    assert picked == 1


@pytest.mark.parametrize("count", [1, 2, 3, 4, 5, 7, 12])
def test_every_direction_selects_some_slice(count):
    wheel = layout(count)
    for step in range(720):
        angle = step * (2 * math.pi / 720)
        assert wheel.aim(*point_at(angle, 120)) is not None


@pytest.mark.parametrize("count", [2, 3, 4, 5, 6, 9])
def test_slice_seams_have_no_dead_rays(count):
    """Aiming exactly along a seam must still pick one of the two neighbours.

    Comparing against each arc's endpoints independently used to leave a
    one-ULP crack here, and `aim` ignores distance, so the crack was a dead
    ray stretching to the edge of the screen.
    """
    wheel = layout(count)
    for sl in wheel.slices:
        for radius in (60.0, 120.0, 4000.0):
            for angle in (sl.start_cw, sl.end_cw, sl.mid):
                for nudge in (-1e-15, 0.0, 1e-15):
                    picked = wheel.aim(*point_at(angle + nudge, radius))
                    assert picked is not None
                    assert 0 <= picked < count


@pytest.mark.parametrize("count", [1, 2, 3, 6, 11])
def test_aim_agrees_with_the_painted_wedge(count):
    """The slice picked at an angle is the one whose wedge is drawn there."""
    wheel = layout(count)
    for sl in wheel.slices:
        span = (sl.start_cw - sl.end_cw) % (2 * math.pi) or 2 * math.pi
        for fraction in (0.02, 0.25, 0.5, 0.75, 0.98):
            angle = sl.start_cw - span * fraction
            assert wheel.aim(*point_at(angle, 120)) == sl.index


def test_aim_ignores_distance_but_hit_test_does_not():
    wheel = layout(5)
    far = point_at(math.pi / 2, 4000)
    assert wheel.aim(*far) == 0, "flicking the mouse should still pick a slice"
    assert wheel.hit_test(*far) is None, "a click that far away is not on the ring"


def test_hub_dead_zone_selects_nothing():
    wheel = layout(5)
    assert wheel.aim(CX, CY) is None
    assert wheel.aim(*point_at(math.pi / 2, INNER * 0.3)) is None


def test_single_app_owns_the_whole_ring():
    wheel = layout(1)
    for step in range(24):
        assert wheel.aim(*point_at(step * math.pi / 12, 100)) == 0


def test_icons_sit_between_the_radii():
    for sl in layout(6).slices:
        radius = math.hypot(sl.icon_x - CX, sl.icon_y - CY)
        assert INNER < radius < OUTER


def test_pie_points_stay_within_the_annulus():
    sl = layout(6).slices[2]
    points = pie_points(CX, CY, INNER, OUTER, sl.start_cw, sl.end_cw)
    radii = [math.hypot(x - CX, y - CY) for x, y in points]
    assert min(radii) == pytest.approx(INNER, abs=0.5)
    assert max(radii) == pytest.approx(OUTER, abs=0.5)


def test_pie_points_densify_with_arc_length():
    wide = layout(2).slices[0]
    narrow = layout(16).slices[0]
    assert len(pie_points(CX, CY, INNER, OUTER, wide.start_cw, wide.end_cw)) > len(
        pie_points(CX, CY, INNER, OUTER, narrow.start_cw, narrow.end_cw)
    )
