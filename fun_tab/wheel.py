"""Radial wheel geometry and hit testing."""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class SliceGeom:
    index: int
    # Angles in radians using math convention (0 = east, CCW positive), Y-up.
    # Slice occupies the clockwise arc from start_cw to end_cw.
    start_cw: float
    end_cw: float
    mid: float
    icon_x: float
    icon_y: float


@dataclass(frozen=True)
class WheelLayout:
    cx: float
    cy: float
    inner_r: float
    outer_r: float
    slices: tuple[SliceGeom, ...]
    first_edge: float  # angle of slice 0's leading (counter-clockwise) edge
    sweep: float  # radians per slice

    def hit_test(self, x: float, y: float) -> int | None:
        """Slice under a point, bounded to the ring itself (used for clicks)."""
        return self._slice_at(x, y, self.inner_r * 0.5, self.outer_r * 1.1)

    def aim(self, x: float, y: float) -> int | None:
        """Slice the cursor *points at*, however far away it is.

        A weapon wheel is a direction picker: once the cursor leaves the hub
        dead zone, the angle alone decides the selection. Flicking the mouse
        towards a slice selects it without having to land on the ring.
        """
        return self._slice_at(x, y, self.inner_r * 0.62, math.inf)

    def _slice_at(self, x: float, y: float, min_r: float, max_r: float) -> int | None:
        if not self.slices:
            return None
        dx = x - self.cx
        dy = y - self.cy
        dist = math.hypot(dx, dy)
        if dist < min_r or dist > max_r:
            return None

        # Screen -> math angle (flip Y), then divide the clockwise offset by the
        # slice width. Testing each arc's endpoints separately instead leaves
        # one-ULP cracks on the seams where no slice matches at all, and with
        # `aim` a crack is a dead ray reaching the edge of the screen.
        angle = math.atan2(-dy, dx)
        offset = (self.first_edge - angle) % (2 * math.pi)
        return min(int(offset / self.sweep), len(self.slices) - 1)


def build_layout(
    count: int,
    cx: float,
    cy: float,
    outer_r: float,
    inner_r: float,
) -> WheelLayout:
    if count <= 0:
        return WheelLayout(cx, cy, inner_r, outer_r, tuple(), math.pi / 2, 2 * math.pi)

    sweep = (2 * math.pi) / count
    # Slice 0 is *centered* on 12 o'clock, then they run clockwise (the
    # direction Tab advances). Centering matters: the wheel opens with the
    # cursor at the hub, so straight up has to be an unambiguous pick rather
    # than the seam between the first and last app.
    top = math.pi / 2
    edge = top + sweep / 2
    icon_r = (inner_r + outer_r) * 0.52
    slices: list[SliceGeom] = []

    for i in range(count):
        start_cw = (edge - i * sweep) % (2 * math.pi)
        end_cw = (edge - (i + 1) * sweep) % (2 * math.pi)
        mid = (top - i * sweep) % (2 * math.pi)
        ix = cx + math.cos(mid) * icon_r
        iy = cy - math.sin(mid) * icon_r
        slices.append(
            SliceGeom(
                index=i,
                start_cw=start_cw,
                end_cw=end_cw,
                mid=mid,
                icon_x=ix,
                icon_y=iy,
            )
        )

    return WheelLayout(cx, cy, inner_r, outer_r, tuple(slices), edge, sweep)


def pie_points(
    cx: float,
    cy: float,
    inner_r: float,
    outer_r: float,
    start_cw: float,
    end_cw: float,
    steps: int = 48,
) -> list[tuple[float, float]]:
    """Polygon points for a donut slice (clockwise from start_cw to end_cw)."""
    span = (start_cw - end_cw) % (2 * math.pi)
    if span < 1e-6:
        span = 2 * math.pi
    # Densify for large arcs / high-res supersampling.
    steps = max(16, int(steps * span / (math.pi / 4)))

    pts: list[tuple[float, float]] = []
    for i in range(steps + 1):
        t = i / steps
        a = (start_cw - t * span) % (2 * math.pi)
        pts.append((cx + math.cos(a) * outer_r, cy - math.sin(a) * outer_r))
    for i in range(steps + 1):
        t = i / steps
        a = (end_cw + t * span) % (2 * math.pi)
        pts.append((cx + math.cos(a) * inner_r, cy - math.sin(a) * inner_r))
    return pts
