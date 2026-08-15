"""Diagram facades: kind-shaped nodes, routed edges, containers, and the reflow that binds them.

A facade is a ``<g>`` that draws itself from a spec stored on it — a node is a shape plus a
centered label, an edge is a routed path plus an optional label, a container is a box drawn
around the members it names. The spec lives in a compact ``data-*`` JSON attribute and is the
single source of truth: ids and names belong to the caller, so nothing here is ever derived
from them.

Where the theme engine says what a node is PAINTED like, the manifest's ``[kinds]`` says what it
is SHAPED like, and its tokens say how much room to leave around it. Both are read through
:func:`~svg_mcp.ops.themes.serving_theme`, so a document that loaded no theme still gets the
bundled default's answer.

The routing engine below is deliberately pure: boxes, sides, tokens and any points to thread
through in, path data out. It is the same code whether an edge is being created, edited, or
reflowed after a node moved — and the same code whether the points it threads are the lanes a
layout reserved for a long edge or a route the author pinned by hand.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from functools import lru_cache
from typing import Literal, cast

import inkex
from inkex import BaseElement
from pydantic import BaseModel, Field

from ..model.document import Document
from ..model.errors import InvalidArgument, SvgMcpError
from ..model.handles import NodeRef, names_node
from ..query.outline import _bbox_xywh, _to_local_box, _to_local_point
from ..typeset import FontNotFound, measure_text
from .construct import (
    _CATEGORY_ATTR,
    _PRIM_ATTR,
    add_circle,
    add_ellipse,
    add_pill,
    add_polygon,
    add_rect,
    add_squircle,
    add_text,
    edit_pill,
    edit_squircle,
)
from .geometry import edit_shape
from .modify import to_back
from .paint import resolve_paint_refs as _resolve_paint_refs
from .resources import _class_list, _set_class_list
from .themes import (
    ServingTheme,
    attach_dressing,
    resolve_auto_styles,
    resolve_dressing,
    serving_theme,
)

Style = dict[str, str]
Point = tuple[float, float]

Side = Literal["N", "S", "E", "W"]
"""Which face of a node an edge leaves from or arrives at."""

AnchorPref = Literal["auto", "N", "S", "E", "W"]
"""A caller's side preference — ``auto`` lets the geometry decide."""

RouteStyle = Literal["orthogonal", "straight", "spline"]
"""How an edge gets from one anchor to the other."""

SHAPES: tuple[str, ...] = ("rect", "squircle", "pill", "polygon", "circle", "ellipse")
"""The primitives a diagram node can be drawn as; anything else a theme names falls back to rect."""

# The facade specs, stored where they cannot be lost or guessed at (never on the id or name).
_NODE_ATTR = "data-diagram-node"
_EDGE_ATTR = "data-diagram-edge"
_CONTAINER_ATTR = "data-diagram-container"
# The chart and annotation facades' specs live here. They are declared alongside their siblings
# (and not in ``ops.chart`` / ``ops.annotate``, which import this module) so the ONE list of what
# auto-placement stacks under can name them without a cycle.
_CHART_ATTR = "data-chart"
_LEGEND_ATTR = "data-legend"
_CALLOUT_ATTR = "data-callout"
_TABLE_ATTR = "data-table"
_CARD_ATTR = "data-callout-card"
# What a new auto-placed facade stacks below: another node, a chart, or an annotation. An edge is
# routed and a container is fitted, so neither takes part in the flow.
_STACKABLE_ATTRS: tuple[str, ...] = (
    _NODE_ATTR,
    _CHART_ATTR,
    _LEGEND_ATTR,
    _CALLOUT_ATTR,
    _TABLE_ATTR,
    _CARD_ATTR,
)
# The one arrowhead every edge shares, marked so it is found again without trusting its id.
_ARROW_ATTR = "data-diagram-arrow"
_ARROW_ID = "diagram-arrow"

_DEFAULT_PAD = 12.0
_DEFAULT_PAD_CONTAINER = 16.0
_DEFAULT_GAP = 24.0
_DEFAULT_STUB = 12.0
_DEFAULT_RADIUS = 8.0
_DEFAULT_CORNER = 6.0
_DEFAULT_CANVAS = "#ffffff"
_DEFAULT_FONT = "sans-serif"
_LABEL_SIZE = 12.0
# The clear air a container leaves above its members for its own label to sit in.
_LABEL_GAP = 8.0

# The corner an auto-placed facade takes when there is nothing above it to stack under. Read in
# the PARENT's frame, because it stands in for the x/y the caller did not pass — see
# ``auto_origin``, which is where the difference between a default and a measurement is drawn.
_AUTO_ORIGIN = 20.0

_MIN_W, _MIN_H = 60.0, 36.0
_DIAMOND_MIN_W, _DIAMOND_MIN_H = 80.0, 48.0

# Two points closer than this are the same point; two stub ends this close on the free axis are
# already lined up, so the route is one straight run rather than a Z with a zero-width jog.
_ALIGN_TOL = 0.5
_EPS = 1e-9

_NORMALS: dict[Side, Point] = {"N": (0.0, -1.0), "S": (0.0, 1.0), "E": (1.0, 0.0), "W": (-1.0, 0.0)}

# Generic CSS families are not font names, so the metric lookup needs real candidates to try.
_GENERIC_FONTS: dict[str, tuple[str, ...]] = {
    "sans-serif": ("Helvetica", "Arial", "DejaVu Sans", "Liberation Sans", "Verdana"),
    "serif": ("Times New Roman", "Times", "DejaVu Serif", "Liberation Serif", "Georgia"),
    "monospace": ("Menlo", "Courier New", "DejaVu Sans Mono", "Liberation Mono", "Consolas"),
}


# --- results -----------------------------------------------------------------


class Reflow(BaseModel):
    """What a reflow moved: edges re-routed, edges it could not, containers re-fitted or not."""

    edges_rerouted: int = 0
    skipped: list[str] = Field(default_factory=list)
    containers_refit: int = 0
    skipped_containers: list[str] = Field(default_factory=list)
    callouts_reanchored: int = 0


@dataclass(frozen=True, slots=True)
class PlacedNode:
    """A new diagram node: its handle plus the geometry the facade actually chose for it."""

    ref: NodeRef
    x: float
    y: float
    w: float
    h: float


@dataclass(frozen=True, slots=True)
class PlacedEdge:
    """A new diagram edge: its handle plus how many edges the reroute it triggered touched."""

    ref: NodeRef
    edges_rerouted: int


@dataclass(frozen=True, slots=True)
class PlacedContainer:
    """A new diagram container: its handle, the box it took, and whether that box is derived."""

    ref: NodeRef
    x: float
    y: float
    w: float
    h: float
    auto: bool


@dataclass(frozen=True, slots=True)
class ContainerEdit:
    """A patched diagram container: its membership after the edit, and whether it re-fitted."""

    ref: NodeRef
    members: list[str]
    refit: bool = False


@dataclass(frozen=True, slots=True)
class NodeEdit:
    """A patched diagram node: whether the label was re-measured, and the shape left alone."""

    ref: NodeRef
    remeasured: bool = False
    shape_unchanged: bool = False


@dataclass(frozen=True, slots=True)
class EdgeEdit:
    """A patched diagram edge and the reroute that followed it."""

    ref: NodeRef
    edges_rerouted: int = 0


# --- geometry primitives -----------------------------------------------------


@dataclass(frozen=True, slots=True)
class Box:
    """An axis-aligned rectangle in world coordinates — a node, as the router sees it."""

    x: float
    y: float
    w: float
    h: float

    @property
    def cx(self) -> float:
        return self.x + self.w / 2.0

    @property
    def cy(self) -> float:
        return self.y + self.h / 2.0

    @property
    def center(self) -> Point:
        return (self.cx, self.cy)


def side_towards(box: Box, at: Point) -> Side:
    """Which face of ``box`` faces the point ``at``: the dominant axis of the gap to it."""
    dx, dy = at[0] - box.cx, at[1] - box.cy
    if abs(dx) >= abs(dy):
        return "E" if dx > 0 else "W"
    return "S" if dy > 0 else "N"


def auto_side(source: Box, target: Box) -> Side:
    """Which face of ``source`` faces ``target``: the dominant axis of the center-to-center gap."""
    return side_towards(source, target.center)


def resolve_sides(
    source: Box,
    target: Box,
    source_pref: AnchorPref,
    target_pref: AnchorPref,
    via: Sequence[Point] | None = None,
) -> tuple[Side, Side]:
    """The two faces an edge uses — the caller's preference where given, else the facing pair.

    With ``via`` points the facing pair is measured against the NEAREST end of the thread rather
    than the far node's center: a lane may leave and arrive from a different side than the direct
    line would, and an anchor pointing away from its own first segment is what reads as broken.
    """
    towards_target = via[0] if via else target.center
    towards_source = via[-1] if via else source.center
    start = source_pref if source_pref != "auto" else side_towards(source, towards_target)
    end = target_pref if target_pref != "auto" else side_towards(target, towards_source)
    return start, end


def anchor_point(box: Box, side: Side, fraction: float) -> Point:
    """The point at ``fraction`` along one face (left→right on N/S, top→bottom on E/W)."""
    if side == "N":
        return (box.x + fraction * box.w, box.y)
    if side == "S":
        return (box.x + fraction * box.w, box.y + box.h)
    if side == "E":
        return (box.x + box.w, box.y + fraction * box.h)
    return (box.x, box.y + fraction * box.h)


def side_angle(side: Side, dx: float, dy: float) -> float:
    """The bearing of a delta measured in a face's own frame: outward is 0, the port axis is +.

    Sorting a face's edges by this puts them in port order without crossings, whichever face it
    is — a plain global ``atan2`` would run the bottom face right-to-left, which is exactly the
    ordering that makes edges cross.
    """
    nx, ny = _NORMALS[side]
    outward = dx * nx + dy * ny
    along = dx if side in ("N", "S") else dy
    return math.atan2(along, outward)


def spread_fractions(center: Point, side: Side, far_centers: Sequence[Point]) -> list[float]:
    """Where each of a face's edges attaches: ``(i+1)/(n+1)`` in port order (one edge → midpoint).

    Returns a fraction per input, in input order. Ties keep input order, so the result is stable.
    """
    total = len(far_centers)

    def bearing(index: int) -> float:
        far = far_centers[index]
        return side_angle(side, far[0] - center[0], far[1] - center[1])

    order = sorted(range(total), key=bearing)
    fractions = [0.5] * total
    for rank, index in enumerate(order):
        fractions[index] = (rank + 1) / (total + 1)
    return fractions


def _dedupe(points: Sequence[Point]) -> list[Point]:
    out: list[Point] = []
    for point in points:
        if not out or math.dist(out[-1], point) > _EPS:
            out.append(point)
    return out


def _collinear(before: Point, here: Point, after: Point) -> bool:
    """True when ``here`` sits on the straight run from ``before`` to ``after``, within the tol.

    Measured as a perpendicular distance, not an angle: two lanes half a unit apart are the same
    lane as far as the drawing is concerned, and a vertex there buys a corner nobody can see.
    """
    dx, dy = after[0] - before[0], after[1] - before[1]
    span = math.hypot(dx, dy)
    if span <= _EPS:  # the route comes straight back on itself: there is nothing in between
        return True
    return abs((here[0] - before[0]) * dy - (here[1] - before[1]) * dx) / span <= _ALIGN_TOL


def _simplify(points: Sequence[Point]) -> list[Point]:
    """Drop the points a route merely passes through — a thread must carry no zero-angle jogs."""
    pts = _dedupe(points)
    if len(pts) < 3:
        return pts
    out = [pts[0]]
    for index in range(1, len(pts) - 1):
        if not _collinear(out[-1], pts[index], pts[index + 1]):
            out.append(pts[index])
    out.append(pts[-1])
    return out


def _thread(start: Point, end: Point, *, horizontal: bool) -> list[Point]:
    """The corner pair joining two stations of a threaded route, jogging BETWEEN their bands.

    The cross-axis change is put half way along the main axis — the same split the two-anchor Z
    router makes, and for the same reason: half way between two rank bands is free air, while
    either band's own coordinate is exactly where its boxes are. Two stations already lined up
    on the cross axis need no corner at all, which is what keeps a straight lane straight.
    """
    if horizontal:
        if abs(start[1] - end[1]) <= _ALIGN_TOL:
            return []
        mid = (start[0] + end[0]) / 2.0
        return [(mid, start[1]), (mid, end[1])]
    if abs(start[0] - end[0]) <= _ALIGN_TOL:
        return []
    mid = (start[1] + end[1]) / 2.0
    return [(start[0], mid), (end[0], mid)]


def orthogonal_waypoints(
    a: Point, sa: Side, b: Point, sb: Side, stub: float, via: Sequence[Point] | None = None
) -> list[Point]:
    """The right-angled polyline from anchor ``a`` to anchor ``b``, stubs included.

    Each end leaves its face straight out by ``stub``; between those two stub ends the route is a
    Z (opposite faces, split on the free axis), a single corner (perpendicular faces), or a U
    (the same face, both stubs pushed out to the further of the two).

    ``via`` replaces that middle with a THREAD: the route visits every via point in order, each
    hop jogging across half way to the next one, and the points it ends up running straight
    through are dropped. Each via point is a LANE position — a cross-axis slot the layout
    reserved in one rank — so consecutive points differ mainly along the main axis.
    """
    na, nb = _NORMALS[sa], _NORMALS[sb]
    a2 = (a[0] + na[0] * stub, a[1] + na[1] * stub)
    b2 = (b[0] + nb[0] * stub, b[1] + nb[1] * stub)
    horizontal = sa in ("E", "W")
    if via:
        stations = [a2, *via, b2]
        # Which axis a THREAD runs along is read off the thread, not off the source face: the
        # lanes are strung out along the main axis, and an author who pinned a face the route
        # only leaves by should not thereby transpose the whole middle of it.
        spread_x = max(at[0] for at in stations) - min(at[0] for at in stations)
        spread_y = max(at[1] for at in stations) - min(at[1] for at in stations)
        horizontal = spread_x >= spread_y
        threaded: list[Point] = [a, a2]
        for start, end in zip(stations, stations[1:], strict=False):
            threaded.extend(_thread(start, end, horizontal=horizontal))
            threaded.append(end)
        threaded.append(b)
        return _simplify(threaded)
    middle: list[Point]
    if sa == sb:
        if horizontal:
            x = max(a2[0], b2[0]) if sa == "E" else min(a2[0], b2[0])
            middle = [(x, a2[1]), (x, b2[1])]
        else:
            y = max(a2[1], b2[1]) if sa == "S" else min(a2[1], b2[1])
            middle = [(a2[0], y), (b2[0], y)]
    elif horizontal == (sb in ("E", "W")):  # opposite faces on the same axis
        if horizontal:
            x = (a2[0] + b2[0]) / 2.0
            middle = [] if abs(a2[1] - b2[1]) <= _ALIGN_TOL else [(x, a2[1]), (x, b2[1])]
        else:
            y = (a2[1] + b2[1]) / 2.0
            middle = [] if abs(a2[0] - b2[0]) <= _ALIGN_TOL else [(a2[0], y), (b2[0], y)]
    elif horizontal:  # a leaves sideways, b leaves vertically: corner takes b's x and a's y
        middle = [(b2[0], a2[1])]
    else:
        middle = [(a2[0], b2[1])]
    return _dedupe([a, a2, *middle, b2, b])


def _num(value: float) -> str:
    if abs(value) < 5e-4:
        return "0"
    return f"{value:.3f}".rstrip("0").rstrip(".")


def _pair(point: Point) -> str:
    return f"{_num(point[0])} {_num(point[1])}"


def _towards(origin: Point, other: Point) -> Point:
    length = math.dist(origin, other)
    if length <= _EPS:
        return (0.0, 0.0)
    return ((other[0] - origin[0]) / length, (other[1] - origin[1]) / length)


def rounded_path(points: Sequence[Point], radius: float) -> str:
    """Path data through ``points``, each corner eased by the largest quadratic that fits it.

    A corner's radius is clamped to half of its shorter adjacent segment, so two corners sharing
    a short run can never eat into each other.
    """
    pts = _dedupe(points)
    if len(pts) < 2:
        raise InvalidArgument("a route needs at least two distinct points")
    parts = [f"M {_pair(pts[0])}"]
    for index in range(1, len(pts) - 1):
        previous, here, following = pts[index - 1], pts[index], pts[index + 1]
        incoming, outgoing = _towards(here, previous), _towards(here, following)
        turn = abs(incoming[0] * outgoing[1] - incoming[1] * outgoing[0])
        fit = min(radius, math.dist(previous, here) / 2.0, math.dist(here, following) / 2.0)
        if turn <= _EPS or fit <= _EPS:  # straight through, or no room to round
            parts.append(f"L {_pair(here)}")
            continue
        start = (here[0] + incoming[0] * fit, here[1] + incoming[1] * fit)
        end = (here[0] + outgoing[0] * fit, here[1] + outgoing[1] * fit)
        parts.append(f"L {_pair(start)}")
        parts.append(f"Q {_pair(here)} {_pair(end)}")
    parts.append(f"L {_pair(pts[-1])}")
    return " ".join(parts)


def _longest_midpoint(points: Sequence[Point]) -> Point:
    best, at = -1.0, points[0]
    for first, second in zip(points, points[1:], strict=False):
        length = math.dist(first, second)
        if length > best:
            best, at = length, ((first[0] + second[0]) / 2.0, (first[1] + second[1]) / 2.0)
    return at


@dataclass(frozen=True, slots=True)
class Route:
    """A routed edge: the path data to draw, where its label wants to sit, and the polyline.

    ``points`` is the route BEFORE it was turned into path data — the vertices for an orthogonal
    route, the anchors and any threaded lane for the other two. It is what the label scorer reads
    candidates off and what corridor separation adjusts, so it travels with the drawn result
    rather than being re-derived from the ``d`` string it produced.
    """

    d: str
    label_at: Point
    points: tuple[Point, ...] = ()


def _spline_path(points: Sequence[Point], sa: Side, sb: Side) -> str:
    """Chained cubics through ``points``, both ends leaving along their own face normal.

    With two points this is EXACTLY the one-curve form the router has always drawn: the control
    handles are the anchors pushed out along their normals by 0.4 of the span. Threading only
    adds interior handles, aimed along the Catmull-Rom tangent (the chord across each via point),
    so a threaded spline is the same aesthetic continued rather than a second curve style.
    """
    na, nb = _NORMALS[sa], _NORMALS[sb]
    tangents: list[Point] = [na]
    tangents.extend(
        _towards(before, after) for before, after in zip(points, points[2:], strict=False)
    )
    tangents.append((-nb[0], -nb[1]))
    parts = [f"M {_pair(points[0])}"]
    for index, (start, end) in enumerate(zip(points, points[1:], strict=False)):
        reach = 0.4 * math.dist(start, end)
        out_of, into = tangents[index], tangents[index + 1]
        c1 = (start[0] + out_of[0] * reach, start[1] + out_of[1] * reach)
        c2 = (end[0] - into[0] * reach, end[1] - into[1] * reach)
        parts.append(f"C {_pair(c1)} {_pair(c2)} {_pair(end)}")
    return " ".join(parts)


def route_points(
    a: Point,
    sa: Side,
    b: Point,
    sb: Side,
    *,
    route: RouteStyle,
    stub: float,
    via: Sequence[Point] | None = None,
) -> list[Point]:
    """The polyline one edge follows, before any of it is turned into path data.

    Split out from :func:`route_edge` because the geometry has to be settled for the WHOLE
    diagram before any of it is drawn: corridor separation moves runs that several edges share,
    and a label is scored against the routes its neighbours ended up taking.
    """
    through = list(via) if via else []
    if route in ("straight", "spline"):
        return [a, *through, b]
    return orthogonal_waypoints(a, sa, b, sb, stub, through)


def draw_route(
    points: Sequence[Point], sa: Side, sb: Side, *, route: RouteStyle, radius: float
) -> str:
    """Path data for a settled polyline, in whichever of the three styles was asked for."""
    if route == "straight":
        return " ".join([f"M {_pair(points[0])}", *(f"L {_pair(point)}" for point in points[1:])])
    if route == "spline":
        return _spline_path(points, sa, sb)
    return rounded_path(points, radius)


def route_edge(
    a: Point,
    sa: Side,
    b: Point,
    sb: Side,
    *,
    route: RouteStyle,
    stub: float,
    radius: float,
    via: Sequence[Point] | None = None,
) -> Route:
    """Draw one edge between two anchors, in whichever of the three styles was asked for.

    ``via`` threads the path through a list of points BETWEEN the two anchors — a layout's
    reserved lanes, or a route an author pinned. It never affects which faces the edge uses
    (that is :func:`resolve_sides`' job) and never reaches past the anchors.
    """
    points = route_points(a, sa, b, sb, route=route, stub=stub, via=via)
    return Route(
        draw_route(points, sa, sb, route=route, radius=radius),
        _longest_midpoint(points),
        tuple(points),
    )


# --- label placement ---------------------------------------------------------
#
# An edge label is the one piece of a diagram that has nowhere it MUST go: the path is pinned at
# both ends, but the text may sit anywhere along it. Putting it at the midpoint of the longest
# segment — the old rule — is right only when that midpoint happens to be over free paper, and on
# a real diagram it lands on a node, on another edge's label, or on a crossing edge often enough
# to be the first thing a reader notices. So the placement is SCORED instead: every segment long
# enough to hold the text offers a candidate, each candidate is charged for what it covers, and
# the cheapest wins. Ties go to the first candidate generated, which is the old answer.

# How far above a segment the text sits, and how far to its side when the segment runs vertically.
_LABEL_DY = -4.0
_LABEL_SIDE_GAP = 4.0

# What a candidate is charged for. The two overlap terms are fractions of the label's own area, so
# fully covering a node costs _COST_BOX and a sliver costs proportionally less — which keeps them
# comparable with each other and far above the per-crossing charge. Distance is the tie-breaker
# that keeps a label near the line it names when nothing else separates two candidates.
_COST_BOX = 1000.0
_COST_LABEL = 800.0
_COST_CROSSING = 25.0
_COST_DISTANCE = 1.0

Segment = tuple[Point, Point]


@dataclass(frozen=True, slots=True)
class LabelContext:
    """What an edge label has to stay out of: node boxes, labels already placed, other routes."""

    boxes: tuple[Box, ...] = ()
    placed: tuple[Box, ...] = ()
    segments: tuple[Segment, ...] = ()


def _overlap_area(a: Box, b: Box) -> float:
    """The area two boxes share — zero when they merely touch or miss."""
    across = min(a.x + a.w, b.x + b.w) - max(a.x, b.x)
    down = min(a.y + a.h, b.y + b.h) - max(a.y, b.y)
    return across * down if across > 0.0 and down > 0.0 else 0.0


def _segment_hits(start: Point, end: Point, box: Box, inset: float = 0.0) -> bool:
    """True when a segment gets inside ``box`` by more than ``inset`` (Liang–Barsky clipping)."""
    left, top = box.x + inset, box.y + inset
    right, bottom = box.x + box.w - inset, box.y + box.h - inset
    if right <= left or bottom <= top:
        return False
    near, far = 0.0, 1.0
    for delta, from_, low, high in (
        (end[0] - start[0], start[0], left, right),
        (end[1] - start[1], start[1], top, bottom),
    ):
        if abs(delta) < _EPS:
            if from_ < low or from_ > high:
                return False
            continue
        first, second = (low - from_) / delta, (high - from_) / delta
        near, far = max(near, min(first, second)), min(far, max(first, second))
        if near > far:
            return False
    return True


def label_rect(at: Point, dy: float, size: Point) -> Box:
    """The box a label occupies: its measured size, centred on ``at`` shifted down by ``dy``.

    The one place the drawing contract is written down — a facade label is anchored ``middle`` on
    a ``central`` baseline, so the text's centre is the point it was written at plus its ``dy``.
    """
    return Box(at[0] - size[0] / 2.0, at[1] + dy - size[1] / 2.0, size[0], size[1])


def score_label(
    rect: Box,
    boxes: Sequence[Box],
    placed_labels: Sequence[Box],
    segments: Sequence[Segment],
) -> float:
    """What a label placement costs: what it covers, and what it crosses. Lower is better.

    Covering a node or another label is charged by AREA (as a fraction of the label's own), so a
    clipped corner costs less than a direct hit. Crossing an edge is charged per crossing rather
    than by area: a line has no area to overlap, and two crossings of the same run is what a
    reader sees as the label sitting ON the wire.
    """
    area = max(rect.w * rect.h, _EPS)
    covered = sum(_overlap_area(rect, box) for box in boxes) / area * _COST_BOX
    clashing = sum(_overlap_area(rect, other) for other in placed_labels) / area * _COST_LABEL
    crossings = sum(1 for start, end in segments if _segment_hits(start, end, rect))
    return covered + clashing + _COST_CROSSING * crossings


def label_candidates(points: Sequence[Point], size: Point) -> list[tuple[Point, float]]:
    """Every position an edge label is allowed to take, best guess FIRST.

    A segment offers a candidate only if the text fits along it — a label wider than the run it
    labels reads as belonging to nothing. If none qualifies the longest segment is used anyway,
    because the label still has to go somewhere. The longest segment is always tried first (and
    above the line first), so a diagram with nothing to avoid keeps the placement it always had.
    """
    pts = _dedupe(points)
    if len(pts) < 2:
        return [((pts[0] if pts else (0.0, 0.0)), _LABEL_DY)]
    width, height = size
    segments = list(zip(pts, pts[1:], strict=False))
    lengths = [math.dist(start, end) for start, end in segments]
    longest = max(range(len(segments)), key=lambda index: lengths[index])
    qualifying = [index for index, length in enumerate(lengths) if length > width]
    order = [longest, *(index for index in qualifying if index != longest)]
    out: list[tuple[Point, float]] = []
    for index in order:
        (sx, sy), (ex, ey) = segments[index]
        mid = ((sx + ex) / 2.0, (sy + ey) / 2.0)
        if abs(ex - sx) >= abs(ey - sy):  # a horizontal run: above the line, or below it
            out.append((mid, _LABEL_DY))
            out.append((mid, height))
        else:  # a vertical run: beside the line, on either side
            gap = width / 2.0 + _LABEL_SIDE_GAP
            out.append(((mid[0] - gap, mid[1]), _LABEL_DY))
            out.append(((mid[0] + gap, mid[1]), _LABEL_DY))
    return out


def place_label(points: Sequence[Point], size: Point, context: LabelContext) -> tuple[Point, float]:
    """Where an edge's label goes and by how much it is nudged: the cheapest candidate.

    Deterministic by construction — the candidates are generated in a fixed order and the winner
    is chosen by a STRICT improvement, so the same document always produces the same placement.
    """
    best_at, best_dy, best_score = (points[0] if points else (0.0, 0.0)), _LABEL_DY, math.inf
    for at, dy in label_candidates(points, size):
        rect = label_rect(at, dy, size)
        near = math.dist((rect.cx, rect.cy), at)
        score = (
            score_label(rect, context.boxes, context.placed, context.segments)
            + _COST_DISTANCE * near
        )
        if score < best_score:
            best_at, best_dy, best_score = at, dy, score
    return best_at, best_dy


# --- corridor separation -----------------------------------------------------
#
# Two orthogonal routes that happen to run along the same lane are drawn exactly on top of each
# other, and one line where the reader expects two is not a small blemish: it hides a whole
# relation. So after every route is settled, the runs they SHARE are found and fanned out around
# the corridor's own centre line. The offset is applied to a whole run at once — a jog halfway
# along a shared corridor is a worse artifact than the overlap it was trying to fix.

# Two runs this close on the cross axis are in the same corridor, whatever the arithmetic says.
_CORRIDOR_TOL = 1.0
# How deep a run may sit inside a node box before the offset that put it there counts as crossing.
_BOX_INSET = 0.5
# The pitches a group tries, in order: full, then tighter, rather than pushing a run into a box.
_PITCH_STEPS = (1.0, 0.5, 0.25)
_DEFAULT_SEPARATION = 4.0

Axis = Literal["h", "v"]


@dataclass(frozen=True, slots=True)
class Corridor:
    """A maximal straight run of one route: where it lies, how far it goes, and which points it is.

    ``coord`` is the cross-axis position (the y of a horizontal run, the x of a vertical one) and
    ``lo``/``hi`` bound it along its own axis. ``start``/``end`` index into the route's points, so
    an offset can move exactly the vertices that make the run and leave the rest joined to them.
    """

    edge: str
    axis: Axis
    coord: float
    lo: float
    hi: float
    start: int
    end: int


def _axis_of(start: Point, end: Point) -> Axis | None:
    """Which axis a segment runs along — None for a zero-length one or a diagonal."""
    across, down = abs(end[0] - start[0]), abs(end[1] - start[1])
    if across <= _ALIGN_TOL and down <= _ALIGN_TOL:
        return None
    if down <= _ALIGN_TOL:
        return "h"
    if across <= _ALIGN_TOL:
        return "v"
    return None


def _cross(point: Point, axis: Axis) -> float:
    return point[1] if axis == "h" else point[0]


def _along(point: Point, axis: Axis) -> float:
    return point[0] if axis == "h" else point[1]


def collinear_runs(routes: Mapping[str, Sequence[Point]]) -> list[Corridor]:
    """Every maximal axis-aligned run in every route, in edge-id order.

    Consecutive segments on the same axis and the same cross-axis coordinate are ONE run: an
    orthogonal route often leaves its stub and carries straight on down the same lane, and
    offsetting the two halves independently would put a step in the middle of a straight line.
    """
    out: list[Corridor] = []
    for edge in sorted(routes):
        pts = list(routes[edge])
        index, total = 0, len(pts)
        while index < total - 1:
            axis = _axis_of(pts[index], pts[index + 1])
            if axis is None:
                index += 1
                continue
            coord = _cross(pts[index], axis)
            end = index + 1
            while end < total - 1 and _axis_of(pts[end], pts[end + 1]) == axis:
                if abs(_cross(pts[end + 1], axis) - coord) > _ALIGN_TOL:
                    break
                end += 1
            span = [_along(point, axis) for point in pts[index : end + 1]]
            out.append(Corridor(edge, axis, coord, min(span), max(span), index, end))
            index = end
    return out


def corridor_groups(runs: Sequence[Corridor], tol: float = _CORRIDOR_TOL) -> list[list[Corridor]]:
    """The runs of DIFFERENT edges that share a corridor, grouped; groups of one are dropped.

    Sharing means the same axis, the same cross-axis coordinate within ``tol``, and intervals
    that actually overlap — two runs meeting end to end are not on top of each other. Grouping is
    transitive, so three lanes a unit apart are one corridor of three rather than two pairs.
    Each group is ordered by edge id, which is what makes the fan-out deterministic.
    """
    home = list(range(len(runs)))

    def root(index: int) -> int:
        while home[index] != index:
            home[index] = home[home[index]]
            index = home[index]
        return index

    for first, one in enumerate(runs):
        for second in range(first + 1, len(runs)):
            other = runs[second]
            if one.edge == other.edge or one.axis != other.axis:
                continue
            if abs(one.coord - other.coord) > tol:
                continue
            if min(one.hi, other.hi) - max(one.lo, other.lo) <= 0.0:
                continue
            home[root(first)] = root(second)

    grouped: dict[int, list[Corridor]] = {}
    for index, run in enumerate(runs):
        grouped.setdefault(root(index), []).append(run)
    return [
        sorted(members, key=lambda run: (run.edge, run.start))
        for _key, members in sorted(grouped.items())
        if len(members) > 1
    ]


def centred_offsets(count: int, pitch: float) -> list[float]:
    """``count`` offsets spaced by ``pitch``, centred on zero — the corridor keeps its own line."""
    return [(index - (count - 1) / 2.0) * pitch for index in range(count)]


def offset_run(points: Sequence[Point], run: Corridor, offset: float) -> list[Point]:
    """Shift one whole run perpendicular to itself, keeping the route joined at both ends.

    The vertices the run is made of move together, so the perpendicular segments either side of
    it simply get longer or shorter — no step appears mid-run. A run that reaches an ANCHOR is
    the exception: that point belongs to the node's face and cannot move, so it is left where it
    is and the segment leaving it absorbs the shift as a taper.
    """
    if abs(offset) <= _EPS:
        return list(points)
    moved = list(points)
    first = run.start + 1 if run.start == 0 else run.start
    last = run.end - 1 if run.end == len(moved) - 1 else run.end
    for index in range(first, last + 1):
        x, y = moved[index]
        moved[index] = (x, y + offset) if run.axis == "h" else (x + offset, y)
    return moved


def _run_offends(points: Sequence[Point], run: Corridor, boxes: Sequence[Box]) -> set[int]:
    """Which of ``boxes`` a run — and the two segments it hangs off — is currently inside."""
    low = max(0, run.start - 1)
    high = min(len(points) - 1, run.end + 1)
    return {
        which
        for which, box in enumerate(boxes)
        for index in range(low, high)
        if _segment_hits(points[index], points[index + 1], box, _BOX_INSET)
    }


def _no_worse(
    shifted: Sequence[Point], before: Sequence[Point], run: Corridor, boxes: Sequence[Box]
) -> bool:
    """True when an offset put the run inside no box it was not already inside.

    Measured as a CHANGE rather than as an absolute, because a route may already be crossing
    something — an unlaid-out long edge does it routinely — and refusing to separate it from its
    twin would leave the reader with one line through the box instead of two. What the offset
    must never do is take a run that was clear and put it through a node.
    """
    return not (_run_offends(shifted, run, boxes) - _run_offends(before, run, boxes))


def separate_corridors(
    routes: Mapping[str, Sequence[Point]],
    *,
    pitch: float,
    obstacles: Mapping[str, Sequence[Box]] | None = None,
    pinned: Sequence[str] = (),
) -> dict[str, list[Point]]:
    """Fan out every shared corridor, and hand back each route's adjusted points.

    Runs shorter than twice the pitch are left alone: a short jog has no corridor to speak of,
    and moving it would only bend the two corners around it. A group whose fan-out would push a
    run into a node box tries a tighter pitch instead, and keeps its overlap rather than crossing
    a box — a doubled line is a blemish, an edge through a node is a lie.

    A ``pinned`` route still takes part in a corridor but never moves: it holds its slot, so the
    routes around it step aside by their own offsets and the overlap still clears. Geometry an
    author typed is not something a tidying pass gets to nudge by two units.
    """
    working = {edge: list(points) for edge, points in routes.items()}
    if pitch <= 0.0:
        return working
    held = set(pinned)
    runs = [run for run in collinear_runs(routes) if run.hi - run.lo > 2.0 * pitch]
    for group in corridor_groups(runs):
        for scale in _PITCH_STEPS:
            trial: dict[str, list[Point]] = {}
            failed = False
            for run, offset in zip(group, centred_offsets(len(group), pitch * scale), strict=True):
                if run.edge in held:
                    continue
                before = trial.get(run.edge, working[run.edge])
                shifted = offset_run(before, run, offset)
                if not _no_worse(shifted, before, run, (obstacles or {}).get(run.edge, ())):
                    failed = True
                    break
                trial[run.edge] = shifted
            if not failed:
                working.update(trial)
                break
    return working


# --- specs -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class NodeSpec:
    """What a diagram node is, as stored on its group.

    ``themed`` records the DRESSING INTENT the facade was built with, which is not the same
    question as whether it currently wears a class: a facade built ``themed=False`` is meant to
    stay bare, so an edit that merely echoes its kind back must not dress it. Absent from a spec
    written before this was recorded, where it reads as True — every such facade was dressed.

    ``pinned`` says this node keeps the coordinates it has: ``layout_diagram`` computes its rank
    like everything else, but places nothing — the layout packs the rest of the drawing AROUND
    it. False (an absent key, which is what every node written before this existed carries) means
    the layout owns its position, which is the normal case.
    """

    kind: str
    label: str
    shape: str
    w: float
    h: float
    auto: bool
    themed: bool = True
    pinned: bool = False


@dataclass(frozen=True, slots=True)
class EdgeSpec:
    """What a diagram edge connects, and how, as stored on its group.

    ``themed`` is the dressing intent — see :class:`NodeSpec`.

    ``waypoints`` is an AUTHOR-PINNED middle: the world points the route threads between its two
    anchors. None (an absent key, which is what every edge written before this existed carries)
    means route freely. Present, it survives every reflow — the endpoints re-anchor to the nodes'
    current faces, the middle is drawn exactly as it was typed. Layout-derived lanes are NOT
    stored here; they are scaffolding, recomputed by each layout pass.
    """

    source: str
    target: str
    kind: str
    sa: AnchorPref
    ta: AnchorPref
    route: RouteStyle
    label: str
    themed: bool = True
    waypoints: tuple[Point, ...] | None = None


@dataclass(frozen=True, slots=True)
class ContainerSpec:
    """What a diagram container encloses, and whether its box is derived from that membership.

    ``themed`` is the dressing intent — see :class:`NodeSpec`.
    """

    kind: str
    label: str
    members: tuple[str, ...]
    auto: bool
    themed: bool = True


SpecValue = str | float | bool | list[str] | list[list[float]]
"""What a facade spec may hold: scalars, a member list, or a list of points."""


def _store(element: BaseElement, attr: str, spec: Mapping[str, SpecValue]) -> None:
    element.set(attr, json.dumps(dict(spec), separators=(",", ":")))


def _side_pref(value: str) -> AnchorPref:
    return cast(AnchorPref, value) if value in ("auto", "N", "S", "E", "W") else "auto"


_ROUTES = ("orthogonal", "straight", "spline")


def _route_style(value: str) -> RouteStyle:
    return cast(RouteStyle, value) if value in _ROUTES else "orthogonal"


def read_node_spec(element: BaseElement) -> NodeSpec | None:
    """The node spec stored on ``element``, or None if it is not a diagram node (or is corrupt)."""
    raw = element.get(_NODE_ATTR)
    if raw is None:
        return None
    try:
        spec = json.loads(raw)
        return NodeSpec(
            kind=str(spec["kind"]),
            label=str(spec["label"]),
            shape=str(spec["shape"]),
            w=float(spec["w"]),
            h=float(spec["h"]),
            auto=bool(spec.get("auto", False)),
            themed=bool(spec.get("themed", True)),
            pinned=bool(spec.get("pinned", False)),
        )
    except (ValueError, TypeError, KeyError):
        return None


def read_edge_spec(element: BaseElement) -> EdgeSpec | None:
    """The edge spec stored on ``element``, or None if it is not a diagram edge (or is corrupt)."""
    raw = element.get(_EDGE_ATTR)
    if raw is None:
        return None
    try:
        spec = json.loads(raw)
        pinned = spec.get("waypoints")
        return EdgeSpec(
            source=str(spec["source"]),
            target=str(spec["target"]),
            kind=str(spec["kind"]),
            sa=_side_pref(str(spec.get("sa", "auto"))),
            ta=_side_pref(str(spec.get("ta", "auto"))),
            route=_route_style(str(spec.get("route", "orthogonal"))),
            label=str(spec.get("label", "")),
            themed=bool(spec.get("themed", True)),
            # An empty list reads as "no pinned route" — that is what clearing one writes, and
            # what an edge that never had one means.
            waypoints=(None if not pinned else tuple((float(x), float(y)) for x, y in pinned)),
        )
    except (ValueError, TypeError, KeyError):
        return None


def read_container_spec(element: BaseElement) -> ContainerSpec | None:
    """The container spec on ``element``, or None if it is not a container (or is corrupt)."""
    raw = element.get(_CONTAINER_ATTR)
    if raw is None:
        return None
    try:
        spec = json.loads(raw)
        members = spec["members"]
        if not isinstance(members, list):
            return None
        return ContainerSpec(
            kind=str(spec["kind"]),
            label=str(spec["label"]),
            members=tuple(str(member) for member in members),
            auto=bool(spec.get("auto", False)),
            themed=bool(spec.get("themed", True)),
        )
    except (ValueError, TypeError, KeyError):
        return None


def _write_container_spec(element: BaseElement, spec: ContainerSpec) -> None:
    _store(
        element,
        _CONTAINER_ATTR,
        {
            "kind": spec.kind,
            "label": spec.label,
            "members": list(spec.members),
            "auto": spec.auto,
            "themed": spec.themed,
        },
    )


def _write_node_spec(element: BaseElement, spec: NodeSpec) -> None:
    stored: dict[str, SpecValue] = {
        "kind": spec.kind,
        "label": spec.label,
        "shape": spec.shape,
        "w": spec.w,
        "h": spec.h,
        "auto": spec.auto,
        "themed": spec.themed,
    }
    # A node nobody pinned writes no ``pinned`` key at all — the same rule an edge's waypoints
    # follow. An absent key is how a spec says "the layout owns this", and it keeps every node
    # written before pinning existed byte-identical.
    if spec.pinned:
        stored["pinned"] = True
    _store(element, _NODE_ATTR, stored)


def _write_edge_spec(element: BaseElement, spec: EdgeSpec) -> None:
    stored: dict[str, SpecValue] = {
        "source": spec.source,
        "target": spec.target,
        "kind": spec.kind,
        "sa": spec.sa,
        "ta": spec.ta,
        "route": spec.route,
        "label": spec.label,
        "themed": spec.themed,
    }
    # An edge nobody pinned a route on writes no ``waypoints`` key at all: an absent key is the
    # only honest way to say "route this freely", and it keeps every existing edge byte-identical.
    if spec.waypoints:
        stored["waypoints"] = [[x, y] for x, y in spec.waypoints]
    _store(element, _EDGE_ATTR, stored)


# --- theme lookups -----------------------------------------------------------


def _token(theme: ServingTheme, name: str, default: float) -> float:
    """A numeric token from the serving theme, tolerating a unit suffix and a missing entry."""
    raw = theme.tokens.get(name)
    if raw is None:
        return default
    text = raw.strip().removesuffix("px").strip()
    try:
        return float(text)
    except ValueError:
        return default


def _shape_for(theme: ServingTheme, kind: str) -> str:
    """The primitive a kind wants — the manifest's answer, normalized to one we can draw."""
    shape = theme.kinds.get(kind, "rect")
    return shape if shape in SHAPES else "rect"


def _label_font(theme: ServingTheme) -> str:
    return (theme.tokens.get("--font") or _DEFAULT_FONT).split(",")[0].strip().strip("'\"")


@lru_cache(maxsize=4096)
def measure_label(text: str, family: str, size: float) -> tuple[float, float]:
    """Measure a label, falling back through the generic family and then to a metric-free guess.

    ``sans-serif`` is a CSS family, not a font, and a headless box may have none of the usual
    faces installed — so a facade that could not size its own box is not an acceptable outcome.

    Cached, because measuring re-opens the font file and a reroute now asks for every edge label
    in the document: the answer depends only on the arguments and on which fonts are installed,
    and the font scan behind it is already cached for the life of the process.
    """
    for candidate in (family, *_GENERIC_FONTS.get(family.lower(), ())):
        try:
            return measure_text(text, font_family=candidate, font_size=size)
        except FontNotFound:
            continue
    return 0.6 * size * len(text), 1.2 * size


# --- facade plumbing ---------------------------------------------------------


@names_node
def _place_facade(
    doc: Document,
    element: BaseElement,
    *,
    prefix: str,
    category: Literal["shape", "connector", "container"],
    prim: str,
    role: str,
    parent: str | None,
    name: str | None,
    style: Style | None,
    styles: list[str] | None,
    themed: bool,
    stamp_prim: bool = True,
) -> NodeRef:
    """Attach a facade group: parent, id, name, the what-it-is stamp, its hooks, its own style.

    The same sequence ``_place_and_style`` runs for a primitive — the group is the themed node
    here, so the hooks land on it and its children are built plain and inherit.

    ``stamp_prim`` is False for a facade whose category has only one shape to be: a container is
    always a box, so ``data-prim`` would restate ``data-category`` and buy nothing.

    As in ``_place_and_style``, everything that can fail is resolved BEFORE the group joins the
    tree — an unservable role must not leave a half-built facade behind.
    """
    parent_element = doc.resolve_parent(parent)
    dressing = resolve_dressing(
        doc, category=category, prim=prim, role=role, styles=styles, themed=themed
    )
    resolved = _resolve_paint_refs(doc, style)

    parent_element.add(element)
    element.set_id(doc.new_id(prefix))
    if name is not None:
        element.label = name
    element.set(_CATEGORY_ATTR, category)
    if stamp_prim:
        element.set(_PRIM_ATTR, prim)
    attach_dressing(doc, element, dressing)
    if resolved:
        element.style = inkex.Style(resolved)
    return NodeRef(id=str(element.get_id()), tag=str(element.TAG), name=name)


@contextmanager
def _facade_body(doc: Document, ref: NodeRef) -> Iterator[None]:
    """Build a facade's children, removing the WHOLE group again if any part of the build fails.

    ``_place_facade`` validates its own dressing up front, but the children a facade goes on to
    draw are built incrementally; a half-drawn facade left in the tree by a raising build is
    worse than none at all, and nothing outside the group can be holding a handle on it yet.
    """
    try:
        yield
    except Exception:
        element = doc.svg.getElementById(ref.id)
        if element is not None:
            element.delete()
        raise


def _diamond(x: float, y: float, w: float, h: float) -> list[Point]:
    return [(x + w / 2.0, y), (x + w, y + h / 2.0), (x + w / 2.0, y + h), (x, y + h / 2.0)]


def _build_shape(
    doc: Document, shape: str, *, x: float, y: float, w: float, h: float, parent: str, corner: float
) -> NodeRef:
    """Draw the node's body inside its group — plain, so the group's hooks style it by descent."""
    if shape == "squircle":
        return add_squircle(
            doc, x=x, y=y, width=w, height=h, radius=corner, parent=parent, themed=False
        )
    if shape == "pill":
        return add_pill(doc, x=x, y=y, width=w, height=h, parent=parent, themed=False)
    if shape == "polygon":
        return add_polygon(doc, points=_diamond(x, y, w, h), parent=parent, themed=False)
    if shape == "circle":
        return add_circle(
            doc, cx=x + w / 2.0, cy=y + h / 2.0, r=min(w, h) / 2.0, parent=parent, themed=False
        )
    if shape == "ellipse":
        return add_ellipse(
            doc,
            cx=x + w / 2.0,
            cy=y + h / 2.0,
            rx=w / 2.0,
            ry=h / 2.0,
            parent=parent,
            themed=False,
        )
    return add_rect(doc, x=x, y=y, width=w, height=h, parent=parent, themed=False)


def _resize_shape(
    doc: Document, element: BaseElement, shape: str, *, x: float, y: float, w: float, h: float
) -> None:
    """Re-size a node's body in place, by the parameters the primitive was created from."""
    target = str(element.get_id())
    if shape == "squircle":
        edit_squircle(doc, target, x=x, y=y, width=w, height=h)
    elif shape == "pill":
        edit_pill(doc, target, x=x, y=y, width=w, height=h)
    elif shape == "polygon":
        element.set("points", " ".join(f"{px},{py}" for px, py in _diamond(x, y, w, h)))
    elif shape == "circle":
        edit_shape(
            doc,
            target,
            expect_tag="circle",
            attrs={"cx": x + w / 2.0, "cy": y + h / 2.0, "r": min(w, h) / 2.0},
        )
    elif shape == "ellipse":
        edit_shape(
            doc,
            target,
            expect_tag="ellipse",
            attrs={"cx": x + w / 2.0, "cy": y + h / 2.0, "rx": w / 2.0, "ry": h / 2.0},
        )
    else:
        edit_shape(doc, target, expect_tag="rect", attrs={"x": x, "y": y, "width": w, "height": h})


def _children(element: BaseElement) -> list[BaseElement]:
    return [child for child in element if isinstance(child.tag, str)]


def _first_text(group: BaseElement) -> BaseElement | None:
    return next((c for c in group if isinstance(c, inkex.TextElement)), None)


def _first_path(group: BaseElement) -> BaseElement | None:
    return next((c for c in group if isinstance(c, inkex.PathElement)), None)


def _body(group: BaseElement) -> BaseElement | None:
    """A node's shape child: the first child that is not its label."""
    return next((c for c in _children(group) if not isinstance(c, inkex.TextElement)), None)


def _local_origin(element: BaseElement) -> Point:
    """Where a node's body sits in its group's own frame — the one shape-blind way to ask."""
    try:
        box = element.bounding_box()
    except Exception:
        return (0.0, 0.0)
    if box is None:
        return (0.0, 0.0)
    return (float(box.left), float(box.top))


def _label_style(halo: str | None, anchor: str = "middle") -> Style:
    """A facade label's structural style: anchored, and haloed when it sits on top of a line."""
    style: Style = {"text-anchor": anchor, "dominant-baseline": "central"}
    if halo is not None:
        style |= {"paint-order": "stroke", "stroke": halo, "stroke-width": "3"}
    return style


def _set_label(
    doc: Document,
    group: BaseElement,
    text: str,
    at: Point,
    *,
    halo: str | None,
    dy: float | None,
    anchor: str = "middle",
) -> None:
    """Create, move, or remove a facade's label so the group matches its spec."""
    existing = _first_text(group)
    if not text:
        if existing is not None:
            existing.delete()
        return
    if existing is None:
        ref = add_text(
            doc,
            x=at[0],
            y=at[1],
            content=text,
            parent=str(group.get_id()),
            style=_label_style(halo, anchor),
            themed=False,
        )
        existing = doc.resolve(ref.id)
    else:
        existing.text = text
        existing.set("x", _num(at[0]))
        existing.set("y", _num(at[1]))
        # Re-bake the structural style: the halo is an inline (pinned) prop, so a variant
        # switch only reaches it through the next reflow — this is that re-bake.
        style = existing.style
        for key, value in _label_style(halo, anchor).items():
            style[key] = value
        if halo is None:
            for key in ("paint-order", "stroke", "stroke-width"):
                style.pop(key, None)
        existing.style = style
    if dy is not None:
        existing.set("dy", _num(dy))


# --- nodes -------------------------------------------------------------------


def _stackable(parent: BaseElement) -> Iterator[BaseElement]:
    return (
        child for child in parent if any(child.get(attr) is not None for attr in _STACKABLE_ATTRS)
    )


def _stack_below(parent: BaseElement, gap: float) -> float | None:
    """The WORLD y a new facade stacks at: under the lowest stackable facade in this parent.

    World, because that is the only frame the boxes it measures agree in — cross it with
    :func:`auto_position` before writing the answer onto a child of ``parent``.
    """
    bottoms = [
        box[1] + box[3]
        for box in (_bbox_xywh(node) for node in _stackable(parent))
        if box is not None
    ]
    return max(bottoms) + gap if bottoms else None


def auto_position(parent: BaseElement, world: Point) -> Point:
    """A position the facade MEASURED, expressed in the frame its parent reads coordinates in.

    The frame contract every ``add_*`` op keeps. An x/y the CALLER gave is parent-local already —
    the same promise ``add_rect`` makes — and is written verbatim. A position DERIVED from other
    nodes' boxes (a stack's lowest edge, the face of a callout's target) can only be computed in
    world, because that is the one frame two nodes' boxes agree in, and so has to be crossed back
    before it is written: otherwise the facade lands displaced by every transform between it and
    the root, and the gap a stack measured is not the gap it draws.
    """
    return _to_local_point(parent, world)


def auto_origin(parent: BaseElement, stacked: float | None, origin: float = _AUTO_ORIGIN) -> Point:
    """Where an auto-placed facade goes, in the frame ``parent`` reads coordinates in.

    Two different things wear the name "automatic". A stack is MEASURED off other nodes, so its y
    is a world number and crosses back through :func:`auto_position`. The corner a facade takes
    when there is nothing above it to stack under is not measured off anything: it is a plain
    default, and a default stands in for the argument the caller did not pass — which would have
    been parent-local. Treating it as world instead would make an auto-placed facade ignore the
    transform of the very layer it was added to, which is not what putting it there asked for.

    Only the y of a stack is meaningful (stacking is a statement about vertical order), so that
    is the only component taken from the crossing.
    """
    if stacked is None:
        return (origin, origin)
    return (origin, auto_position(parent, (origin, stacked))[1])


def auto_size(doc: Document, kind: str, label: str, *, shape: str | None = None) -> Point:
    """The box a kind-shaped node needs to hold ``label``, padded from the serving theme.

    The one place a facade's measured size is decided, so that everything which needs to know
    what a node WOULD be sized to — creating one, re-labelling one, or scaling one from data
    without letting its text overflow — asks the same question and gets the same answer.

    ``shape`` overrides the kind's own primitive, for a node that was drawn before its kind
    changed: geometry follows what is on the canvas, not what the spec was re-pointed at.
    """
    theme = serving_theme(doc, kind)
    drawn = shape if shape is not None else _shape_for(theme, kind)
    pad = _token(theme, "--pad-node", _DEFAULT_PAD)
    # A blank label reserves no text box: measuring "" still yields a full line-height,
    # so the host's font metrics would leak into what should be exactly the minimum box.
    if label.strip():
        text_w, text_h = measure_label(label, _label_font(theme), _LABEL_SIZE)
    else:
        text_w, text_h = 0.0, 0.0
    if drawn == "polygon":  # a diamond's inscribed text box is half its extents
        return (
            max(_DIAMOND_MIN_W, text_w * 1.6 + 2 * pad),
            max(_DIAMOND_MIN_H, text_h * 2 + 2 * pad),
        )
    return (max(_MIN_W, text_w + 2 * pad), max(_MIN_H, text_h + 2 * pad))


def add_diagram_node(
    doc: Document,
    *,
    kind: str,
    label: str = "",
    x: float | None = None,
    y: float | None = None,
    width: float | None = None,
    height: float | None = None,
    parent: str | None = None,
    name: str | None = None,
    style: Style | None = None,
    styles: list[str] | None = None,
    themed: bool = True,
    pinned: bool = False,
) -> PlacedNode:
    """Add a diagram node — a shape and its centered label, as one themed group.

    The ``kind`` is the whole design: the theme serving it says which primitive to draw (its
    manifest's ``[kinds]``) and paints the group, and the label is measured and padded from that
    theme's tokens. Omit ``width``/``height`` to size the box to its label, and ``x``/``y`` to
    stack it under the last diagram node in the same parent.

    ``pinned`` exempts this node from :func:`layout_diagram`'s placement: it keeps the ``x``/``y``
    it has and the layout packs everything else around it. Its rank is still COMPUTED from its
    edges, so pinning it far from where its rank lands gives it doubling-back edges — the pin
    overrides where the node is drawn, never what the graph says it is.
    """
    theme = serving_theme(doc, kind)
    shape = _shape_for(theme, kind)
    gap = _token(theme, "--gap-node", _DEFAULT_GAP)
    corner = _token(theme, "--radius", _DEFAULT_CORNER)

    auto = width is None and height is None
    measured_w, measured_h = auto_size(doc, kind, label, shape=shape)
    w = width if width is not None else measured_w
    h = height if height is not None else measured_h

    parent_element = doc.resolve_parent(parent)
    stacked = _stack_below(parent_element, gap)
    auto_x, auto_y = auto_origin(parent_element, stacked)
    at_x = x if x is not None else auto_x
    at_y = y if y is not None else auto_y

    group = inkex.Group()
    ref = _place_facade(
        doc,
        group,
        prefix="diagram-node",
        category="shape",
        prim=shape,
        role=kind,
        parent=parent,
        name=name,
        style=style,
        styles=styles,
        themed=themed,
    )
    with _facade_body(doc, ref):
        _build_shape(doc, shape, x=at_x, y=at_y, w=w, h=h, parent=ref.id, corner=corner)
        _set_label(doc, group, label, (at_x + w / 2.0, at_y + h / 2.0), halo=None, dy=None)
        _write_node_spec(
            group,
            NodeSpec(
                kind=kind,
                label=label,
                shape=shape,
                w=w,
                h=h,
                auto=auto,
                themed=themed,
                pinned=pinned,
            ),
        )
    return PlacedNode(ref=ref, x=at_x, y=at_y, w=w, h=h)


def _swap_role(doc: Document, element: BaseElement, old: str, new: str) -> None:
    """Move a facade from one role class to another, leaving everything else it carries alone.

    The NEW dressing is resolved in full FIRST — including the facade's own category and
    primitive, read off the stamps, so a routed theme's category context is honoured exactly as
    it was at construction. Only once that has succeeded is the old role class taken off: a kind
    nothing can serve leaves the facade wearing precisely what it wore before.
    """
    category = str(element.get(_CATEGORY_ATTR) or "") or None
    prim = str(element.get(_PRIM_ATTR) or "") or None
    dressing = resolve_dressing(doc, category=category, prim=prim, role=new, themed=True)
    owned = {f"{theme}-{old}" for theme in doc.theme_meta}
    remaining = [cls for cls in _class_list(element) if cls not in owned]
    _set_class_list(element, remaining)
    attach_dressing(doc, element, dressing)


def _wears_its_dressing(doc: Document, element: BaseElement, role: str) -> bool:
    """True when the facade already wears EXACTLY what a fresh dressing for ``role`` would give it.

    The dressed state, not the spec, is what says whether re-naming a facade's own kind has work
    to do — a facade left undressed by an interrupted edit reads as the right kind and looks like
    the wrong one. A role nothing can serve counts as worn: the swap would only raise, and it was
    never the caller's request to change anything.
    """
    category = str(element.get(_CATEGORY_ATTR) or "") or None
    prim = str(element.get(_PRIM_ATTR) or "") or None
    with suppress(InvalidArgument):
        wanted = resolve_auto_styles(doc, category=category, prim=prim, role=role, themed=True)
        worn = set(_class_list(element))
        return all(cls in worn for cls in wanted)
    return True


def _rekind(
    doc: Document, element: BaseElement, old: str, new: str | None, *, themed: bool
) -> bool:
    """Re-dress a facade for ``new`` when the caller named a kind; True when anything changed.

    Naming the kind the facade already claims is NOT a no-op when the facade is not wearing that
    kind's dressing: it is the way to repair one, so the short-circuit reads the classes, not the
    spec. It IS a no-op for a facade built ``themed=False``, whose spec records that it is meant
    to be undressed — echoing its kind back at it must not quietly dress it after all.
    """
    if new is None:
        return False
    if new == old and (not themed or _wears_its_dressing(doc, element, old)):
        return False
    _swap_role(doc, element, old, new)
    return True


def set_spec_themed(element: BaseElement, themed: bool) -> bool:
    """Record dressing intent on whichever facade spec ``element`` carries; False if it has none.

    A scoped ``apply_theme``/``clear_theme`` changes how a facade LOOKS, and the spec is where a
    facade keeps what it MEANS to look like — leaving the two disagreeing is what makes a later
    kind-echo edit either re-dress a deliberately bare facade or refuse to repair a dressed one.
    """
    for attr in (_NODE_ATTR, _EDGE_ATTR, _CONTAINER_ATTR, _CALLOUT_ATTR, _CARD_ATTR):
        raw = element.get(attr)
        if raw is None:
            continue
        try:
            spec = json.loads(raw)
        except ValueError:
            return False
        if not isinstance(spec, dict):
            return False
        spec["themed"] = themed
        element.set(attr, json.dumps(spec, separators=(",", ":")))
        return True
    return False


def edit_diagram_node(
    doc: Document,
    target: str,
    *,
    label: str | None = None,
    kind: str | None = None,
    pinned: bool | None = None,
) -> NodeEdit:
    """Edit a diagram node by its SPEC: re-label it, or move it to another kind.

    A new ``label`` re-centers the text, and re-measures the box only if the node was auto-sized
    (an explicit width/height is a decision, not a guess). A new ``kind`` swaps the role class the
    group wears but does NOT redraw its shape — changing geometry under an existing node would
    throw away whatever has been laid out around it, so it is reported as ``shape_unchanged``.

    ``pinned`` sets or clears the layout exemption (None leaves it as it is): a pinned node keeps
    its coordinates through :func:`layout_diagram`, which packs the rest of the drawing around it.
    Its rank is still computed from its edges, so a node pinned far from where its rank lands has
    edges that double back to reach it.
    """
    group = doc.resolve(target)
    spec = read_node_spec(group)
    if spec is None:
        raise InvalidArgument(f"{target!r} is not a diagram node (no {_NODE_ATTR} spec)")
    body = _body(group)
    if body is None:
        raise InvalidArgument(f"diagram node {target!r} has lost its shape child")

    # The re-kind goes FIRST because it is the only step that can refuse: it resolves the new
    # dressing in full before it changes anything, so a bogus kind leaves the node's geometry,
    # its label and its spec exactly as they were rather than half-edited. Its siblings
    # (edit_diagram_edge, edit_callout, edit_callout_card) are ordered the same way.
    _rekind(doc, group, spec.kind, kind, themed=spec.themed)

    w, h, remeasured = spec.w, spec.h, False
    origin = _local_origin(body)
    if label is not None and spec.auto:
        w, h = auto_size(doc, kind or spec.kind, label, shape=spec.shape)
        remeasured = True
        _resize_shape(doc, body, spec.shape, x=origin[0], y=origin[1], w=w, h=h)

    if label is not None:
        _set_label(
            doc,
            group,
            label,
            (origin[0] + w / 2.0, origin[1] + h / 2.0),
            halo=None,
            dy=None,
        )

    _write_node_spec(
        group,
        NodeSpec(
            kind=kind if kind is not None else spec.kind,
            label=label if label is not None else spec.label,
            shape=spec.shape,
            w=w,
            h=h,
            auto=spec.auto,
            themed=spec.themed,
            pinned=spec.pinned if pinned is None else pinned,
        ),
    )
    return NodeEdit(
        ref=NodeRef(id=str(group.get_id()), tag=str(group.TAG), name=getattr(group, "label", None)),
        remeasured=remeasured,
        shape_unchanged=kind is not None and kind != spec.kind,
    )


# --- edges -------------------------------------------------------------------


def _arrow_marker(doc: Document) -> str:
    """The one arrowhead every diagram edge shares, created on first need."""
    for node in doc.svg.defs:
        if node.get(_ARROW_ATTR) is not None:
            return str(node.get_id())
    marker = inkex.Marker()
    for key, value in (
        ("refX", "10"),
        ("refY", "5"),
        ("markerWidth", "8"),
        ("markerHeight", "8"),
        ("orient", "auto"),
        ("markerUnits", "strokeWidth"),
        ("viewBox", "0 0 10 10"),
    ):
        marker.set(key, value)
    marker.set(_ARROW_ATTR, "1")
    doc.svg.defs.add(marker)
    marker.set_id(_ARROW_ID if doc.svg.getElementById(_ARROW_ID) is None else doc.new_id("marker"))
    head = inkex.PathElement.new("M 0 0 L 10 5 L 0 10 Z")
    head.style = inkex.Style({"fill": "#333333", "stroke": "none"})
    marker.add(head)
    head.set_id(doc.new_id("arrowhead"))
    return str(marker.get_id())


def _box_of(doc: Document, target: str) -> Box | None:
    try:
        element = doc.resolve(target)
    except Exception:
        return None
    box = _bbox_xywh(element)
    return None if box is None else Box(box[0], box[1], box[2], box[3])


@dataclass(frozen=True, slots=True)
class _Leg:
    """One resolvable edge, pinned to real geometry and ready to have its ports assigned.

    ``via`` is the middle the route threads — the author's pinned waypoints, else the lane this
    pass was handed, else nothing.
    """

    element: BaseElement
    spec: EdgeSpec
    source: Box
    target: Box
    sa: Side
    ta: Side
    via: tuple[Point, ...] = ()


def _edge_groups(doc: Document) -> Iterator[tuple[BaseElement, EdgeSpec]]:
    for node in doc.svg.iter():
        if isinstance(node.tag, str) and node.get(_EDGE_ATTR) is not None:
            spec = read_edge_spec(node)
            if spec is not None:
                yield node, spec


def _node_ids(doc: Document) -> list[str]:
    """Every diagram node's id, in document order."""
    return [
        str(node.get_id())
        for node in doc.svg.iter()
        if isinstance(node.tag, str) and node.get(_NODE_ATTR) is not None
    ]


def _segments_of(points: Sequence[Point]) -> list[Segment]:
    return list(zip(points, points[1:], strict=False))


def _bounds(points: Sequence[Point], margin: Point) -> Box:
    """The box a polyline occupies, grown by ``margin`` on each axis.

    A route only has to be scored against what is NEAR it: everything else contributes zero to
    every candidate, and walking the whole diagram per edge is what turns a reroute quadratic.
    """
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    return Box(
        min(xs) - margin[0],
        min(ys) - margin[1],
        max(xs) - min(xs) + 2 * margin[0],
        max(ys) - min(ys) + 2 * margin[1],
    )


def _touches(a: Box, b: Box) -> bool:
    return a.x <= b.x + b.w and b.x <= a.x + a.w and a.y <= b.y + b.h and b.y <= a.y + a.h


def _write_route(
    doc: Document, leg: _Leg, points: Sequence[Point], label_at: Point, label_dy: float
) -> None:
    """Draw one settled route and put its label where the scorer said it should go."""
    theme = serving_theme(doc, leg.spec.kind)
    drawn = draw_route(
        points,
        leg.sa,
        leg.ta,
        route=leg.spec.route,
        radius=_token(theme, "--edge-radius", _DEFAULT_RADIUS),
    )
    path = _first_path(leg.element)
    if path is None:
        path = inkex.PathElement.new(drawn)
        leg.element.add(path)
        path.set_id(doc.new_id("edge-path"))
    path.set("d", drawn)
    path.style = inkex.Style({"fill": "none", "marker-end": f"url(#{_arrow_marker(doc)})"})
    canvas = theme.tokens.get("--canvas", _DEFAULT_CANVAS)
    _set_label(doc, leg.element, leg.spec.label, label_at, halo=canvas, dy=label_dy)


def _reroute(
    doc: Document,
    *,
    scope: set[str] | None = None,
    via: Mapping[str, Sequence[Point]] | None = None,
) -> Reflow:
    """Re-derive every diagram edge's path from the CURRENT node boxes, ports and all.

    Port spreading is computed over ALL edges even when ``scope`` narrows what gets rewritten —
    a node's ports are shared, so an edge's anchor depends on its neighbours whether or not the
    caller asked about them.

    ``via`` supplies lanes for THIS pass only, keyed by edge id: the points a layout reserved for
    a rank-spanning edge. They are never stored. An edge whose spec carries author-pinned
    ``waypoints`` ignores the lane it was offered — geometry somebody typed beats geometry the
    layout worked out.

    The pass runs in three stages, because the last two need the WHOLE picture: every route's
    polyline is settled first, then the corridors several routes share are fanned apart, and only
    then is anything drawn and its label scored against what its neighbours ended up doing.
    Geometry is computed for every edge, not just the ones in ``scope`` — a narrowed reroute must
    place its edges exactly where a full one would, or scoping would change the drawing.
    """

    def wanted(element: BaseElement, spec: EdgeSpec) -> bool:
        return scope is None or bool({str(element.get_id()), spec.source, spec.target} & scope)

    def threaded(element: BaseElement, spec: EdgeSpec) -> tuple[Point, ...]:
        if spec.waypoints:
            return spec.waypoints
        lane = (via or {}).get(str(element.get_id()))
        return tuple(lane) if lane else ()

    # One bounding box per element per pass: measuring a box is the expensive part of routing,
    # and every node is asked about at least twice (its own edges, and everyone else's obstacles).
    measured: dict[str, Box | None] = {}

    def box_for(target: str) -> Box | None:
        if target not in measured:
            measured[target] = _box_of(doc, target)
        return measured[target]

    legs: list[_Leg] = []
    chosen: list[bool] = []
    skipped: list[str] = []
    for element, spec in _edge_groups(doc):
        source, target = box_for(spec.source), box_for(spec.target)
        if source is None or target is None:
            if wanted(element, spec):
                skipped.append(str(element.get_id()))
            continue
        thread = threaded(element, spec)
        sa, ta = resolve_sides(source, target, spec.sa, spec.ta, thread or None)
        legs.append(_Leg(element, spec, source, target, sa, ta, thread))
        chosen.append(wanted(element, spec))

    ends: dict[tuple[str, Side], list[tuple[int, bool]]] = {}
    for index, leg in enumerate(legs):
        ends.setdefault((leg.spec.source, leg.sa), []).append((index, True))
        ends.setdefault((leg.spec.target, leg.ta), []).append((index, False))
    fractions: dict[tuple[int, bool], float] = {}
    for (_node, side), members in ends.items():
        home = legs[members[0][0]].source if members[0][1] else legs[members[0][0]].target
        far = [(legs[i].target if is_source else legs[i].source).center for i, is_source in members]
        for member, fraction in zip(members, spread_fractions(home.center, side, far), strict=True):
            fractions[member] = fraction

    themes = {leg.spec.kind: serving_theme(doc, leg.spec.kind) for leg in legs}
    routes: list[list[Point]] = []
    for index, leg in enumerate(legs):
        theme = themes[leg.spec.kind]
        routes.append(
            route_points(
                anchor_point(leg.source, leg.sa, fractions[(index, True)]),
                leg.sa,
                anchor_point(leg.target, leg.ta, fractions[(index, False)]),
                leg.ta,
                route=leg.spec.route,
                stub=_token(theme, "--edge-stub", _DEFAULT_STUB),
                via=leg.via or None,
            )
        )

    # Only orthogonal routes take part: a spline or a straight line between two spread ports
    # coincides with another only by accident, and nudging a curve sideways changes its shape
    # rather than its lane. The pitch is the TIGHTEST any serving theme asks for, so a corridor
    # shared by two kinds separates by one number whichever edge is looked at first.
    boxes = {
        node_id: box
        for node_id, box in ((node_id, box_for(node_id)) for node_id in _node_ids(doc))
        if box is not None
    }
    ids = [str(leg.element.get_id()) for leg in legs]
    pitch = min(
        (_token(theme, "--edge-separation", _DEFAULT_SEPARATION) for theme in themes.values()),
        default=0.0,
    )
    # What each route has to be scored and clamped against is only what lies NEAR it, so every
    # route's own extent is measured once — grown by the furthest its label could be put and by
    # the widest the corridor fan could push it — and used to narrow both lists.
    sizes = [
        measure_label(leg.spec.label, _label_font(themes[leg.spec.kind]), _LABEL_SIZE)
        if leg.spec.label
        else (0.0, 0.0)
        for leg in legs
    ]
    reach = [
        _bounds(
            points,
            (
                max(2.0 * pitch, size[0] + _LABEL_SIDE_GAP),
                max(2.0 * pitch, 1.5 * size[1] + _LABEL_SIDE_GAP),
            ),
        )
        for points, size in zip(routes, sizes, strict=True)
    ]
    orthogonal = {
        ids[index]: points
        for index, (leg, points) in enumerate(zip(legs, routes, strict=True))
        if leg.spec.route == "orthogonal"
    }
    obstacles = {
        ids[index]: [
            box
            for node_id, box in boxes.items()
            if node_id not in (leg.spec.source, leg.spec.target) and _touches(reach[index], box)
        ]
        for index, leg in enumerate(legs)
    }
    separated = separate_corridors(
        orthogonal,
        pitch=pitch,
        obstacles=obstacles,
        pinned=[ids[index] for index, leg in enumerate(legs) if leg.spec.waypoints],
    )
    routes = [separated.get(ids[index], points) for index, points in enumerate(routes)]

    # Labels are placed against every route in the diagram and against the labels already put
    # down this pass, so the order matters — it is document order, which is stable.
    segments = [_segments_of(points) for points in routes]
    placed: list[Box] = []
    rerouted = 0
    for index, leg in enumerate(legs):
        points = routes[index]
        if leg.spec.label:
            size = sizes[index]
            near = reach[index]
            others = tuple(
                segment
                for other, run in enumerate(segments)
                if other != index and _touches(near, reach[other])
                for segment in run
            )
            at, dy = place_label(
                points,
                size,
                LabelContext(
                    boxes=tuple(box for box in boxes.values() if _touches(near, box)),
                    placed=tuple(rect for rect in placed if _touches(near, rect)),
                    segments=others,
                ),
            )
            placed.append(label_rect(at, dy, size))
        else:
            at, dy = _longest_midpoint(points), _LABEL_DY
        if not chosen[index]:
            continue
        _write_route(doc, leg, points, at, dy)
        rerouted += 1
    return Reflow(edges_rerouted=rerouted, skipped=skipped, containers_refit=0)


def add_diagram_edge(
    doc: Document,
    *,
    source: str,
    target: str,
    kind: str = "data",
    source_anchor: AnchorPref = "auto",
    target_anchor: AnchorPref = "auto",
    route: RouteStyle = "orthogonal",
    label: str | None = None,
    waypoints: list[Point] | None = None,
    parent: str | None = None,
    name: str | None = None,
    styles: list[str] | None = None,
    themed: bool = True,
) -> PlacedEdge:
    """Connect two nodes with a routed, themed edge — and re-spread every port it shares.

    The edge stores what it connects, not where it runs, so it can be re-derived at any time:
    the path is computed now from both boxes and re-computed by ``reflow`` after they move. Sides
    default to whichever faces each other; several edges on one face fan out across it.

    ``waypoints`` PINS the middle of the route: the endpoints keep tracking the nodes' faces, but
    the points in between are drawn exactly as given, through every later reflow and layout pass.
    Omit them (or pass an empty list) to let the router decide.

    Every edge shares ONE arrowhead marker, so an edge's head does not follow its kind's colour
    yet — per-kind markers are a later refinement.
    """
    source_id = str(doc.resolve(source).get_id())
    target_id = str(doc.resolve(target).get_id())
    group = inkex.Group()
    ref = _place_facade(
        doc,
        group,
        prefix="diagram-edge",
        category="connector",
        prim="edge",
        role=kind,
        parent=parent,
        name=name,
        style=None,
        styles=styles,
        themed=themed,
    )
    with _facade_body(doc, ref):
        _write_edge_spec(
            group,
            EdgeSpec(
                source=source_id,
                target=target_id,
                kind=kind,
                sa=source_anchor,
                ta=target_anchor,
                route=route,
                label=label or "",
                themed=themed,
                waypoints=tuple((float(x), float(y)) for x, y in waypoints or ()) or None,
            ),
        )
        result = _reroute(doc)
    return PlacedEdge(ref=ref, edges_rerouted=result.edges_rerouted)


def edit_diagram_edge(
    doc: Document,
    target: str,
    *,
    kind: str | None = None,
    route: RouteStyle | None = None,
    source_anchor: AnchorPref | None = None,
    target_anchor: AnchorPref | None = None,
    label: str | None = None,
    waypoints: list[Point] | None = None,
) -> EdgeEdit:
    """Edit a diagram edge by its SPEC — kind, route style, anchors, label — and re-route it.

    Changing an anchor re-spreads the faces it leaves and joins, so the edges sharing them move
    too; that is the point of storing the spec rather than the path.

    ``waypoints`` REPLACES the pinned middle wholesale — there is no per-point edit, because a
    route is one shape rather than a list of independent decisions. An EMPTY list clears the pin
    and hands the edge back to the router; None (the default) leaves whatever it had alone.
    """
    group = doc.resolve(target)
    spec = read_edge_spec(group)
    if spec is None:
        raise InvalidArgument(f"{target!r} is not a diagram edge (no {_EDGE_ATTR} spec)")
    _rekind(doc, group, spec.kind, kind, themed=spec.themed)
    _write_edge_spec(
        group,
        EdgeSpec(
            source=spec.source,
            target=spec.target,
            kind=kind if kind is not None else spec.kind,
            sa=source_anchor if source_anchor is not None else spec.sa,
            ta=target_anchor if target_anchor is not None else spec.ta,
            route=route if route is not None else spec.route,
            label=label if label is not None else spec.label,
            themed=spec.themed,
            waypoints=(
                spec.waypoints
                if waypoints is None
                else tuple((float(x), float(y)) for x, y in waypoints) or None
            ),
        ),
    )
    result = _reroute(doc)
    return EdgeEdit(
        ref=NodeRef(id=str(group.get_id()), tag=str(group.TAG), name=getattr(group, "label", None)),
        edges_rerouted=result.edges_rerouted,
    )


# --- containers --------------------------------------------------------------


def _container_groups(doc: Document) -> Iterator[tuple[BaseElement, ContainerSpec]]:
    for node in doc.svg.iter():
        if isinstance(node.tag, str) and node.get(_CONTAINER_ATTR) is not None:
            spec = read_container_spec(node)
            if spec is not None:
                yield node, spec


def _encloses(ancestor: BaseElement, node: BaseElement) -> bool:
    """True when ``node`` IS ``ancestor`` or sits underneath it."""
    current: BaseElement | None = node
    while current is not None:
        if current is ancestor:
            return True
        current = current.getparent()
    return False


def _member_ids(doc: Document, members: Sequence[str], *, anchor: BaseElement) -> list[str]:
    """Resolve member handles to ids, rejecting anything that would enclose the container.

    A container draws itself around its members' boxes, so a member that CONTAINS the container
    (the parent it is being added to, or one of that parent's ancestors) would make the fit feed
    on its own output. Duplicates collapse; order is the caller's.
    """
    out: list[str] = []
    for entry in members:
        try:
            element = doc.resolve(entry)
        except SvgMcpError as exc:
            raise InvalidArgument(f"container member {entry!r} does not resolve: {exc}") from exc
        if _encloses(element, anchor):
            raise InvalidArgument(
                f"container member {entry!r} encloses the container itself; a container is a "
                "SIBLING of its members, so it cannot be fitted around its own ancestor"
            )
        node_id = str(element.get_id())
        if node_id not in out:
            out.append(node_id)
    return out


def _container_pad(doc: Document, kind: str) -> float:
    return _token(serving_theme(doc, kind), "--pad-container", _DEFAULT_PAD_CONTAINER)


def _fit_bounds(
    doc: Document, members: Sequence[str], *, pad: float, label: str
) -> tuple[float, float, float, float] | None:
    """The box that holds every resolvable member, padded, with headroom for a label.

    None when nothing in ``members`` resolves any more — there is no honest box to draw then,
    so the caller leaves the old one alone and reports it.
    """
    boxes = [box for box in (_box_of(doc, member) for member in members) if box is not None]
    if not boxes:
        return None
    left = min(box.x for box in boxes) - pad
    top = min(box.y for box in boxes) - pad
    right = max(box.x + box.w for box in boxes) + pad
    bottom = max(box.y + box.h for box in boxes) + pad
    if label:
        top -= _LABEL_SIZE + _LABEL_GAP
    return (left, top, right - left, bottom - top)


def _set_container_label(
    doc: Document,
    group: BaseElement,
    label: str,
    box: tuple[float, float, float, float],
    pad: float,
) -> None:
    """Put the container's label inside its top-left corner, one pad in from each edge."""
    _set_label(doc, group, label, (box[0] + pad, box[1] + pad), halo=None, dy=None, anchor="start")


def _container_box(group: BaseElement) -> tuple[float, float, float, float] | None:
    """The box a container is currently drawn at, read off the rect it owns."""
    rect = _body(group)
    if rect is None:
        return None
    try:
        bbox = rect.bounding_box()
    except Exception:
        return None
    if bbox is None:
        return None
    return (float(bbox.left), float(bbox.top), float(bbox.width), float(bbox.height))


def _refit_container(doc: Document, group: BaseElement, spec: ContainerSpec) -> bool:
    """Re-derive an auto container's box from its members' CURRENT boxes; False if it cannot.

    An auto container's rect is DERIVED state, so this normalizes it: the geometry is written in
    the group's frame (where the label is written too) and the rect's own transform is cleared.
    Give the container an explicit x/y/width/height if its box is meant to be a decision.
    """
    pad = _container_pad(doc, spec.kind)
    box = _fit_bounds(doc, spec.members, pad=pad, label=spec.label)
    rect = _body(group)
    if box is None or rect is None:
        return False
    # The members' union is a WORLD box, and BOTH the rect and the label are children of the
    # group — so both are written in the GROUP's frame, exactly as the add path writes them. A
    # fitted container's geometry is DERIVED state, so a transform somebody hung on the rect is
    # not a decision to preserve here: it would displace the fit by its own translation, and the
    # box is re-derived from the members on every reflow anyway. Clearing it is the normalization
    # that keeps "where the rect is" a single statement in one frame.
    local = _to_local_box(group, box)
    rect.set("transform", None)
    edit_shape(
        doc,
        str(rect.get_id()),
        expect_tag="rect",
        attrs={"x": local[0], "y": local[1], "width": local[2], "height": local[3]},
    )
    _set_container_label(doc, group, spec.label, local, pad)
    return True


def _refit_containers(doc: Document, *, scope: set[str] | None = None) -> tuple[int, list[str]]:
    """Re-fit every auto container in ``scope``; returns (how many, the ones that could not).

    A container is in a narrowed scope when its own handle is named OR any of its members is —
    moving a node is the usual reason to re-fit, and the caller names the node, not the box.
    """
    refit, skipped = 0, []
    for group, spec in _container_groups(doc):
        if not spec.auto:
            continue
        if scope is not None and not ({str(group.get_id()), *spec.members} & scope):
            continue
        if _refit_container(doc, group, spec):
            refit += 1
        else:
            skipped.append(str(group.get_id()))
    return refit, skipped


def add_diagram_container(
    doc: Document,
    *,
    members: list[str],
    kind: str = "cluster",
    label: str = "",
    x: float | None = None,
    y: float | None = None,
    width: float | None = None,
    height: float | None = None,
    parent: str | None = None,
    name: str | None = None,
    style: Style | None = None,
    styles: list[str] | None = None,
    themed: bool = True,
) -> PlacedContainer:
    """Group nodes visually: a themed box drawn BEHIND the members it names, with a corner label.

    The container is a sibling of its members, not their parent — grouping is a statement about
    the picture, not a change to the tree, so nothing is reparented and every member keeps the
    position, edges and styling it already had.

    Omit any of x/y/width/height and the box is derived from the members' current boxes (padded
    by the theme's ``--pad-container``, plus headroom for the label) and re-derived by every
    later ``reflow``. Give all four and the box is yours: it is used verbatim and never re-fitted,
    which is also the only way to draw a container with no members at all.
    """
    parent_element = doc.resolve_parent(parent)
    member_ids = _member_ids(doc, members, anchor=parent_element)
    pad = _container_pad(doc, kind)

    if x is not None and y is not None and width is not None and height is not None:
        box, auto = (x, y, width, height), False
    else:
        auto = True
        if not member_ids:
            raise InvalidArgument(
                "a container with no members needs an explicit x, y, width and height — "
                "there is nothing to fit a box around"
            )
        fitted = _fit_bounds(doc, member_ids, pad=pad, label=label)
        if fitted is None:
            raise InvalidArgument("none of this container's members has a bounding box to fit to")
        box = fitted

    group = inkex.Group()
    ref = _place_facade(
        doc,
        group,
        prefix="diagram-container",
        category="container",
        prim="container",
        role=kind,
        parent=parent,
        name=name,
        style=style,
        styles=styles,
        themed=themed,
        stamp_prim=False,
    )
    with _facade_body(doc, ref):
        # ``box`` is a WORLD union of the members' boxes; the rect's numbers are read in the
        # group's frame, so it has to be crossed over before anything is drawn from it.
        local = _to_local_box(group, box)
        add_rect(
            doc,
            x=local[0],
            y=local[1],
            width=local[2],
            height=local[3],
            parent=ref.id,
            themed=False,
        )
        _set_container_label(doc, group, label, local, pad)
        _write_container_spec(
            group,
            ContainerSpec(
                kind=kind,
                label=label,
                members=tuple(member_ids),
                auto=auto,
                themed=themed,
            ),
        )
        to_back(doc, ref.id)  # a container is scenery: it draws behind everything it encloses
    return PlacedContainer(ref=ref, x=box[0], y=box[1], w=box[2], h=box[3], auto=auto)


def _drop_id(doc: Document, entry: str) -> str:
    """The id a removal names — falling back to the text, since a member may already be gone."""
    try:
        return str(doc.resolve(entry).get_id())
    except SvgMcpError:
        return entry


def edit_diagram_container(
    doc: Document,
    target: str,
    *,
    label: str | None = None,
    kind: str | None = None,
    members: list[str] | None = None,
    add_members: list[str] | None = None,
    remove_members: list[str] | None = None,
) -> ContainerEdit:
    """Edit a container by its SPEC: re-label it, re-kind it, or change what it encloses.

    ``members`` REPLACES the membership; ``add_members``/``remove_members`` adjust it. Combining
    the two in one call is rejected — which of the two was meant is not something to guess at.
    Any change that moves an auto container's box re-fits it on the spot.
    """
    group = doc.resolve(target)
    spec = read_container_spec(group)
    if spec is None:
        raise InvalidArgument(f"{target!r} is not a diagram container (no {_CONTAINER_ATTR} spec)")
    if members is not None and (add_members is not None or remove_members is not None):
        raise InvalidArgument(
            "pass `members` to replace the membership OR `add_members`/`remove_members` to "
            "adjust it — not both in one call"
        )

    current = list(spec.members)
    changed = False
    if members is not None:
        current = _member_ids(doc, members, anchor=group)
        changed = True
    else:
        if remove_members:
            dropped = {_drop_id(doc, entry) for entry in remove_members}
            current = [member for member in current if member not in dropped]
            changed = True
        if add_members:
            for node_id in _member_ids(doc, add_members, anchor=group):
                if node_id not in current:
                    current.append(node_id)
            changed = True
    if changed and spec.auto and not current:
        raise InvalidArgument(
            "an auto-fitted container cannot be emptied — it would have no box to derive; "
            "delete it, or give it an explicit x/y/width/height first"
        )

    _rekind(doc, group, spec.kind, kind, themed=spec.themed)
    updated = ContainerSpec(
        kind=kind if kind is not None else spec.kind,
        label=label if label is not None else spec.label,
        members=tuple(current),
        auto=spec.auto,
        themed=spec.themed,
    )
    _write_container_spec(group, updated)

    refit = False
    if updated.auto and (changed or label is not None or kind is not None):
        refit = _refit_container(doc, group, updated)
    elif label is not None:
        box = _container_box(group)
        if box is not None:
            _set_container_label(doc, group, updated.label, box, _container_pad(doc, updated.kind))
    return ContainerEdit(
        ref=NodeRef(id=str(group.get_id()), tag=str(group.TAG), name=getattr(group, "label", None)),
        members=list(current),
        refit=refit,
    )


# --- reflow ------------------------------------------------------------------


def reflow(
    doc: Document,
    *,
    edges: bool = True,
    containers: bool = True,
    scope: list[str] | None = None,
    _via: Mapping[str, Sequence[Point]] | None = None,
) -> Reflow:
    """Re-derive the diagram's derived geometry from what its nodes are doing NOW.

    Call it after moving, resizing, or deleting nodes: every edge is re-routed from the current
    boxes, re-choosing sides and re-spreading ports, and every auto-fitted container is re-drawn
    around its members. An edge whose source or target no longer resolves is left exactly as it
    was and reported in ``skipped``; a container none of whose members resolves any more is
    likewise left alone and reported in ``skipped_containers``.

    The edge pass also re-anchors every CALLOUT leader — an annotation is derived geometry in the
    same way an edge is, and a callout whose target has gone is reported in ``skipped`` alongside
    them. The cards themselves stay put; only the two ends of each leader move.

    Containers with an explicit box are never touched — that geometry was a decision. Legends are
    never touched at all: what a key says, and where it sits, are both deliberate.

    ``scope`` narrows the pass to the handles it names. Entries are resolved LENIENTLY: deleting
    a node is the commonest reason to reflow at all, so a handle that no longer resolves is
    reported in ``skipped`` (as the caller wrote it) and the rest of the scope still applies.
    An EMPTY ``scope`` is an explicit no-op — it names nothing, so nothing is re-derived; omit
    the argument entirely to reflow the whole document.

    LANES ARE NOT PERSISTENT. ``layout_diagram`` threads the lanes it reserved for rank-spanning
    edges into its own reflow through ``_via`` (an internal channel, one pass only — see the
    recorded decision not to freeze layout scaffolding into the document). A LATER plain
    ``reflow`` — after a ``translate_node``, say — therefore re-derives DIRECT routes with no
    lanes at all, and a long edge may go back to crossing a box. That is the explicit-reflow
    contract working as designed, not a regression: run ``layout_diagram`` again to get the lanes
    back, or pin the route with ``edit_diagram_edge(waypoints=...)`` to make it survive anything.
    """
    ids: set[str] | None = None
    unresolved: list[str] = []
    if scope is not None:
        ids = set()
        for entry in scope:
            try:
                ids.add(str(doc.resolve(entry).get_id()))
            except SvgMcpError:
                unresolved.append(entry)
    result = _reroute(doc, scope=ids, via=_via) if edges else Reflow()
    result.skipped.extend(unresolved)
    if edges:
        # ``ops.annotate`` imports this module, not vice versa — hence the deferred import, the
        # same shape ``ops.themes`` uses to reach back into ``ops.construct``.
        from .annotate import reanchor_callouts

        reanchored, lost = reanchor_callouts(doc, scope=ids)
        result.callouts_reanchored = reanchored
        result.skipped.extend(lost)
    if containers:
        refit, skipped = _refit_containers(doc, scope=ids)
        result.containers_refit = refit
        result.skipped_containers = skipped
    return result
