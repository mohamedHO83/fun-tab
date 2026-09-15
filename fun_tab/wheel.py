"""Radial wheel geometry and hit testing.

Three layouts are built from one primitive. Every one of them is a run of
donut segments over a bounded arc, so ``build_arc_layout`` is the only place
angles are actually computed:

* the **MRU ring** — elastic, divides whatever arc the pinned lane leaves
* the **pinned lane** — a fixed sweep per slot, anchored at 6 o'clock, so a
  given app is always in the same direction whether or not it is running
* the **fan** — one application's windows, over a short arc at a larger radius

The wheel is a direction picker, so angle decides which slice. Radial distance
decides *which layout* is being aimed at, which is the one input dimension the
switcher had left.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

TAU = 2 * math.pi

# Icons sit slightly outside the middle of the band, which reads as centred.
ICON_FRAC = 0.52

# 6 o'clock. The lane grows symmetrically either side of it.
LANE_CENTER = 3 * math.pi / 2

# However many slots are pinned, the lane never takes more than half the wheel.
# Without this a wheel of twelve windows and five closed pins gives the closed
# apps wider targets than the windows actually in use, which is backwards.
MAX_LANE_SPAN = math.pi


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
    span: float = TAU  # arc the layout covers; less than a full turn for a lane or fan
    # Which slice index occupies each angular slot, clockwise from first_edge.
    # Empty means "slot number is the index", which is the full-circle case.
    # ``-1`` marks a slot deliberately left unfilled (see ``build_wheel``).
    slot_index: tuple[int, ...] = ()

    def hit_test(self, x: float, y: float) -> int | None:
        """Slice under a point, bounded to the ring itself (used for clicks)."""
        return self._slice_at(x, y, self.inner_r * 0.5, self.outer_r * 1.1)

    def aim(
        self,
        x: float,
        y: float,
        *,
        min_r: float | None = None,
        max_r: float = math.inf,
    ) -> int | None:
        """Slice the cursor *points at*, however far away it is.

        A weapon wheel is a direction picker: once the cursor leaves the hub
        dead zone, the angle alone decides the selection. Flicking the mouse
        towards a slice selects it without having to land on the ring.

        ``max_r`` bounds that generosity, which is what lets an overshoot mean
        "the fan" rather than another vote for the same slice.
        """
        floor = self.inner_r * 0.62 if min_r is None else min_r
        return self._slice_at(x, y, floor, max_r)

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
        offset = (self.first_edge - angle) % TAU
        # Only bounded arcs can be missed. A full turn must not test this at
        # all: `%` on floats can land exactly on TAU for a tiny negative input,
        # and rejecting that would put a dead ray back on the seam the clamp
        # below exists to close.
        if self.span < TAU and offset >= self.span:
            return None  # outside a lane or a fan, not this layout's business
        if not self.slot_index:
            # The slice's own index, not the slot number: a lane layout numbers
            # its slices from where the MRU ones left off.
            slot = min(int(offset / self.sweep), len(self.slices) - 1)
            return self.slices[slot].index
        slot = min(int(offset / self.sweep), len(self.slot_index) - 1)
        index = self.slot_index[slot]
        return None if index < 0 else index

    def slice_for(self, index: int) -> SliceGeom | None:
        return next((sl for sl in self.slices if sl.index == index), None)


@dataclass(frozen=True)
class RingLayout:
    """The main ring: an elastic MRU arc plus a fixed pinned lane.

    Slice indices are flat across both, MRU first, so everything that already
    walks a single list of slices — selection, Tab, the digit jumps, the label
    and hub caches — keeps working without knowing the lane exists.
    """

    cx: float
    cy: float
    inner_r: float
    outer_r: float
    mru: WheelLayout
    lane: WheelLayout
    mru_count: int = 0
    lane_count: int = 0
    gap: float = 0.0
    # Arc runs that should actually be painted, as (start_cw, end_cw). Empty
    # means the whole circle, which is the no-lane case and paints as one disc.
    bands: tuple[tuple[float, float], ...] = ()

    @property
    def count(self) -> int:
        return self.mru_count + self.lane_count

    @property
    def slices(self) -> tuple[SliceGeom, ...]:
        return self.mru.slices + self.lane.slices

    def aim(self, x: float, y: float, *, max_r: float = math.inf) -> int | None:
        hit = self.mru.aim(x, y, max_r=max_r)
        if hit is None:
            hit = self.lane.aim(x, y, max_r=max_r)
        return hit

    def hit_test(self, x: float, y: float) -> int | None:
        hit = self.mru.hit_test(x, y)
        return self.lane.hit_test(x, y) if hit is None else hit

    def slice_for(self, index: int) -> SliceGeom | None:
        if index < self.mru_count:
            return self.mru.slice_for(index)
        return self.lane.slice_for(index)

    def is_lane(self, index: int) -> bool:
        return index >= self.mru_count


def _empty_layout(cx: float, cy: float, outer_r: float, inner_r: float) -> WheelLayout:
    return WheelLayout(cx, cy, inner_r, outer_r, tuple(), math.pi / 2, TAU)


def _filled_bands(layout: WheelLayout) -> tuple[tuple[float, float], ...]:
    """Maximal runs of occupied slots, so an unfilled slot paints as a gap.

    An unfilled slot that still got ring fill would read as a slice holding
    nothing, which is a different (and wrong) statement from a gap.
    """
    if not layout.slices:
        return ()
    if not layout.slot_index:
        return ((layout.first_edge, (layout.first_edge - layout.span) % TAU),)

    edge, sweep = layout.first_edge, layout.sweep
    bands: list[tuple[float, float]] = []
    run: int | None = None
    for slot, index in enumerate(layout.slot_index):
        if index >= 0 and run is None:
            run = slot
        elif index < 0 and run is not None:
            bands.append(((edge - run * sweep) % TAU, (edge - slot * sweep) % TAU))
            run = None
    if run is not None:
        end = len(layout.slot_index)
        bands.append(((edge - run * sweep) % TAU, (edge - end * sweep) % TAU))
    return tuple(bands)


def build_arc_layout(
    count: int,
    cx: float,
    cy: float,
    outer_r: float,
    inner_r: float,
    first_edge: float,
    span: float,
    *,
    start_index: int = 0,
    slots: tuple[int, ...] = (),
) -> WheelLayout:
    """``count`` slices filling ``span``, running clockwise from ``first_edge``.

    ``slots`` optionally says which angular slot each slice occupies, letting a
    run of slices wrap around a longer arc; the slot count then sets the sweep
    rather than the slice count does.
    """
    if count <= 0:
        return _empty_layout(cx, cy, outer_r, inner_r)

    positions = slots or tuple(range(count))
    slot_count = max(positions) + 1 if slots else count
    sweep = span / slot_count
    icon_r = (inner_r + outer_r) * ICON_FRAC

    geoms: list[SliceGeom] = []
    for i, slot in enumerate(positions[:count]):
        start_cw = (first_edge - slot * sweep) % TAU
        end_cw = (first_edge - (slot + 1) * sweep) % TAU
        mid = (first_edge - (slot + 0.5) * sweep) % TAU
        geoms.append(
            SliceGeom(
                index=start_index + i,
                start_cw=start_cw,
                end_cw=end_cw,
                mid=mid,
                icon_x=cx + math.cos(mid) * icon_r,
                icon_y=cy - math.sin(mid) * icon_r,
            )
        )

    slot_index: tuple[int, ...] = ()
    if slots:
        table = [-1] * slot_count
        for i, slot in enumerate(positions[:count]):
            table[slot] = start_index + i
        slot_index = tuple(table)

    return WheelLayout(
        cx, cy, inner_r, outer_r, tuple(geoms), first_edge % TAU, sweep, span, slot_index
    )


def build_layout(
    count: int,
    cx: float,
    cy: float,
    outer_r: float,
    inner_r: float,
) -> WheelLayout:
    if count <= 0:
        return WheelLayout(cx, cy, inner_r, outer_r, tuple(), math.pi / 2, TAU)

    sweep = TAU / count
    # Slice 0 is *centered* on 12 o'clock, then they run clockwise (the
    # direction Tab advances). Centering matters: the wheel opens with the
    # cursor at the hub, so straight up has to be an unambiguous pick rather
    # than the seam between the first and last app.
    top = math.pi / 2
    return build_arc_layout(count, cx, cy, outer_r, inner_r, top + sweep / 2, TAU)


def lane_span(count: int, slot_deg: float) -> float:
    """Radians the lane occupies for a given number of pinned slots."""
    if count <= 0:
        return 0.0
    per_slot = min(math.radians(max(1.0, slot_deg)), MAX_LANE_SPAN / count)
    return per_slot * count


def build_wheel(
    mru_count: int,
    lane_count: int,
    cx: float,
    cy: float,
    outer_r: float,
    inner_r: float,
    *,
    slot_deg: float = 30.0,
    gap_deg: float = 6.0,
) -> RingLayout:
    """The main ring, with the pinned lane reserving fixed angles at the bottom.

    With ``lane_count == 0`` this is exactly ``build_layout``: the free arc is
    the whole circle, so an existing user's wheel is unchanged until they pin
    something.

    The lane's whole value is that a given app is always in the same direction,
    so its angles cannot depend on how many windows happen to be open. That
    makes the lane the fixed part and the MRU arc the elastic one.
    """
    if lane_count <= 0:
        return RingLayout(
            cx=cx,
            cy=cy,
            inner_r=inner_r,
            outer_r=outer_r,
            mru=build_layout(mru_count, cx, cy, outer_r, inner_r),
            lane=_empty_layout(cx, cy, outer_r, inner_r),
            mru_count=max(0, mru_count),
        )

    span = lane_span(lane_count, slot_deg)
    lane = build_arc_layout(
        lane_count,
        cx,
        cy,
        outer_r,
        inner_r,
        LANE_CENTER + span / 2,
        span,
        start_index=max(0, mru_count),
    )

    # An empty angular gap rather than a drawn divider: a line reads as
    # decoration, a gap reads as two separate regions.
    gap = max(0.0, min(math.radians(gap_deg), (TAU - span) / 4))
    free = TAU - span - 2 * gap
    if mru_count <= 0 or free <= 0:
        return RingLayout(
            cx=cx,
            cy=cy,
            inner_r=inner_r,
            outer_r=outer_r,
            mru=_empty_layout(cx, cy, outer_r, inner_r),
            lane=lane,
            lane_count=lane_count,
            gap=gap,
            bands=_filled_bands(lane),
        )

    # The free arc is symmetric about 12 o'clock, so an odd number of slots puts
    # one slot's centre exactly there and straight up stays an unambiguous pick.
    # For an even window count we round the slot count up and leave one slot
    # unfilled; it lands just counter-clockwise of slice 0, where it reads as
    # the boundary between the newest and the oldest window rather than as a
    # missing tooth.
    slot_count = mru_count if mru_count % 2 else mru_count + 1
    centre_slot = (slot_count - 1) // 2
    positions = tuple((centre_slot + i) % slot_count for i in range(mru_count))
    if slot_count > mru_count:
        # build_arc_layout sizes the sweep from the highest slot used, so the
        # unfilled slot has to be accounted for explicitly.
        positions = positions + ((centre_slot - 1) % slot_count,)

    mru = build_arc_layout(
        mru_count,
        cx,
        cy,
        outer_r,
        inner_r,
        LANE_CENTER - span / 2 - gap,
        free,
        slots=positions,
    )

    return RingLayout(
        cx=cx,
        cy=cy,
        inner_r=inner_r,
        outer_r=outer_r,
        mru=mru,
        lane=lane,
        mru_count=mru_count,
        lane_count=lane_count,
        gap=gap,
        bands=_filled_bands(mru) + _filled_bands(lane),
    )


def build_fan(
    count: int,
    cx: float,
    cy: float,
    outer_r: float,
    inner_r: float,
    mid: float,
    slice_sweep: float,
    *,
    min_deg: float = 12.0,
    max_deg: float = 90.0,
) -> WheelLayout:
    """One application's windows, fanned over its slice at a larger radius.

    The fan deliberately overhangs its slice. Dividing a slice's own span would
    give a three-window app on a crowded wheel under six degrees per entry,
    which is not a flick target — the fan would be legible and unusable at the
    same time. It stays centred on the slice so the direction still reads as
    "that app", just further out.
    """
    if count <= 0:
        return _empty_layout(cx, cy, outer_r, inner_r)

    per_entry = max(slice_sweep / count, math.radians(min_deg))
    span = min(per_entry * count, math.radians(max_deg))
    return build_arc_layout(count, cx, cy, outer_r, inner_r, mid + span / 2, span)


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
    span = (start_cw - end_cw) % TAU
    if span < 1e-6:
        span = TAU
    # Densify for large arcs / high-res supersampling.
    steps = max(16, int(steps * span / (math.pi / 4)))

    pts: list[tuple[float, float]] = []
    for i in range(steps + 1):
        t = i / steps
        a = (start_cw - t * span) % TAU
        pts.append((cx + math.cos(a) * outer_r, cy - math.sin(a) * outer_r))
    for i in range(steps + 1):
        t = i / steps
        a = (end_cw + t * span) % TAU
        pts.append((cx + math.cos(a) * inner_r, cy - math.sin(a) * inner_r))
    return pts
