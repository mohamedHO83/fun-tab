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

    def hit_test(self, x: float, y: float) -> int | None:
        dx = x - self.cx
        dy = y - self.cy
        dist = math.hypot(dx, dy)
        if dist < self.inner_r * 0.5 or dist > self.outer_r * 1.1:
            return None

        # Screen → math angle (flip Y).
        angle = math.atan2(-dy, dx) % (2 * math.pi)

        for sl in self.slices:
            if _angle_on_clockwise_arc(angle, sl.start_cw, sl.end_cw):
                return sl.index
        return None


def _angle_on_clockwise_arc(angle: float, start: float, end: float) -> bool:
    """True if angle lies on the half-open clockwise arc [start, end)."""
    angle %= 2 * math.pi
    start %= 2 * math.pi
    end %= 2 * math.pi
    if abs((start - end) % (2 * math.pi)) < 1e-9:
        return True  # full circle
    cw_to_angle = (start - angle) % (2 * math.pi)
    cw_span = (start - end) % (2 * math.pi)
    return cw_to_angle < cw_span


def build_layout(
    count: int,
    cx: float,
    cy: float,
    outer_r: float,
    inner_r: float,
) -> WheelLayout:
    if count <= 0:
        return WheelLayout(cx, cy, inner_r, outer_r, tuple())

    sweep = (2 * math.pi) / count
    # First slice centered near the top, then clockwise (Tab advances clockwise).
    top = math.pi / 2
    icon_r = (inner_r + outer_r) * 0.52
    slices: list[SliceGeom] = []

    for i in range(count):
        start_cw = (top - i * sweep) % (2 * math.pi)
        end_cw = (top - (i + 1) * sweep) % (2 * math.pi)
        mid = (top - (i + 0.5) * sweep) % (2 * math.pi)
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

    return WheelLayout(cx, cy, inner_r, outer_r, tuple(slices))


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
