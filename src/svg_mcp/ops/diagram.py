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

The routing engine below is deliberately pure: boxes, sides and tokens in, path data out. It is
the same code whether an edge is being created, edited, or reflowed after a node moved.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
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


def auto_side(source: Box, target: Box) -> Side:
    """Which face of ``source`` faces ``target``: the dominant axis of the center-to-center gap."""
    dx, dy = target.cx - source.cx, target.cy - source.cy
    if abs(dx) >= abs(dy):
        return "E" if dx > 0 else "W"
    return "S" if dy > 0 else "N"


def resolve_sides(
    source: Box, target: Box, source_pref: AnchorPref, target_pref: AnchorPref
) -> tuple[Side, Side]:
    """The two faces an edge uses — the caller's preference where given, else the facing pair."""
    start = source_pref if source_pref != "auto" else auto_side(source, target)
    end = target_pref if target_pref != "auto" else auto_side(target, source)
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


def orthogonal_waypoints(a: Point, sa: Side, b: Point, sb: Side, stub: float) -> list[Point]:
    """The right-angled polyline from anchor ``a`` to anchor ``b``, stubs included.

    Each end leaves its face straight out by ``stub``; between those two stub ends the route is a
    Z (opposite faces, split on the free axis), a single corner (perpendicular faces), or a U
    (the same face, both stubs pushed out to the further of the two).
    """
    na, nb = _NORMALS[sa], _NORMALS[sb]
    a2 = (a[0] + na[0] * stub, a[1] + na[1] * stub)
    b2 = (b[0] + nb[0] * stub, b[1] + nb[1] * stub)
    horizontal = sa in ("E", "W")
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
    """A routed edge: the path data to draw and where its label wants to sit."""

    d: str
    label_at: Point


def route_edge(
    a: Point,
    sa: Side,
    b: Point,
    sb: Side,
    *,
    route: RouteStyle,
    stub: float,
    radius: float,
) -> Route:
    """Draw one edge between two anchors, in whichever of the three styles was asked for."""
    if route == "straight":
        return Route(f"M {_pair(a)} L {_pair(b)}", ((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0))
    if route == "spline":
        reach = 0.4 * math.dist(a, b)
        na, nb = _NORMALS[sa], _NORMALS[sb]
        c1 = (a[0] + na[0] * reach, a[1] + na[1] * reach)
        c2 = (b[0] + nb[0] * reach, b[1] + nb[1] * reach)
        return Route(
            f"M {_pair(a)} C {_pair(c1)} {_pair(c2)} {_pair(b)}",
            ((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0),
        )
    points = orthogonal_waypoints(a, sa, b, sb, stub)
    return Route(rounded_path(points, radius), _longest_midpoint(points))


# --- specs -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class NodeSpec:
    """What a diagram node is, as stored on its group.

    ``themed`` records the DRESSING INTENT the facade was built with, which is not the same
    question as whether it currently wears a class: a facade built ``themed=False`` is meant to
    stay bare, so an edit that merely echoes its kind back must not dress it. Absent from a spec
    written before this was recorded, where it reads as True — every such facade was dressed.
    """

    kind: str
    label: str
    shape: str
    w: float
    h: float
    auto: bool
    themed: bool = True


@dataclass(frozen=True, slots=True)
class EdgeSpec:
    """What a diagram edge connects, and how, as stored on its group.

    ``themed`` is the dressing intent — see :class:`NodeSpec`.
    """

    source: str
    target: str
    kind: str
    sa: AnchorPref
    ta: AnchorPref
    route: RouteStyle
    label: str
    themed: bool = True


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


def _store(
    element: BaseElement, attr: str, spec: Mapping[str, str | float | bool | list[str]]
) -> None:
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
        return EdgeSpec(
            source=str(spec["source"]),
            target=str(spec["target"]),
            kind=str(spec["kind"]),
            sa=_side_pref(str(spec.get("sa", "auto"))),
            ta=_side_pref(str(spec.get("ta", "auto"))),
            route=_route_style(str(spec.get("route", "orthogonal"))),
            label=str(spec.get("label", "")),
            themed=bool(spec.get("themed", True)),
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
    _store(
        element,
        _NODE_ATTR,
        {
            "kind": spec.kind,
            "label": spec.label,
            "shape": spec.shape,
            "w": spec.w,
            "h": spec.h,
            "auto": spec.auto,
            "themed": spec.themed,
        },
    )


def _write_edge_spec(element: BaseElement, spec: EdgeSpec) -> None:
    _store(
        element,
        _EDGE_ATTR,
        {
            "source": spec.source,
            "target": spec.target,
            "kind": spec.kind,
            "sa": spec.sa,
            "ta": spec.ta,
            "route": spec.route,
            "label": spec.label,
            "themed": spec.themed,
        },
    )


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


def measure_label(text: str, family: str, size: float) -> tuple[float, float]:
    """Measure a label, falling back through the generic family and then to a metric-free guess.

    ``sans-serif`` is a CSS family, not a font, and a headless box may have none of the usual
    faces installed — so a facade that could not size its own box is not an acceptable outcome.
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
        child
        for child in parent
        if any(child.get(attr) is not None for attr in _STACKABLE_ATTRS)
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
) -> PlacedNode:
    """Add a diagram node — a shape and its centered label, as one themed group.

    The ``kind`` is the whole design: the theme serving it says which primitive to draw (its
    manifest's ``[kinds]``) and paints the group, and the label is measured and padded from that
    theme's tokens. Omit ``width``/``height`` to size the box to its label, and ``x``/``y`` to
    stack it under the last diagram node in the same parent.
    """
    theme = serving_theme(doc, kind)
    shape = _shape_for(theme, kind)
    pad = _token(theme, "--pad-node", _DEFAULT_PAD)
    gap = _token(theme, "--gap-node", _DEFAULT_GAP)
    corner = _token(theme, "--radius", _DEFAULT_CORNER)

    auto = width is None and height is None
    text_w, text_h = measure_label(label, _label_font(theme), _LABEL_SIZE)
    if shape == "polygon":  # a diamond's inscribed text box is half its extents
        w = width if width is not None else max(_DIAMOND_MIN_W, text_w * 1.6 + 2 * pad)
        h = height if height is not None else max(_DIAMOND_MIN_H, text_h * 2 + 2 * pad)
    else:
        w = width if width is not None else max(_MIN_W, text_w + 2 * pad)
        h = height if height is not None else max(_MIN_H, text_h + 2 * pad)

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
            NodeSpec(kind=kind, label=label, shape=shape, w=w, h=h, auto=auto, themed=themed),
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
) -> NodeEdit:
    """Edit a diagram node by its SPEC: re-label it, or move it to another kind.

    A new ``label`` re-centers the text, and re-measures the box only if the node was auto-sized
    (an explicit width/height is a decision, not a guess). A new ``kind`` swaps the role class the
    group wears but does NOT redraw its shape — changing geometry under an existing node would
    throw away whatever has been laid out around it, so it is reported as ``shape_unchanged``.
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
        theme = serving_theme(doc, kind or spec.kind)
        pad = _token(theme, "--pad-node", _DEFAULT_PAD)
        text_w, text_h = measure_label(label, _label_font(theme), _LABEL_SIZE)
        if spec.shape == "polygon":
            w = max(_DIAMOND_MIN_W, text_w * 1.6 + 2 * pad)
            h = max(_DIAMOND_MIN_H, text_h * 2 + 2 * pad)
        else:
            w = max(_MIN_W, text_w + 2 * pad)
            h = max(_MIN_H, text_h + 2 * pad)
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
    """One resolvable edge, pinned to real geometry and ready to have its ports assigned."""

    element: BaseElement
    spec: EdgeSpec
    source: Box
    target: Box
    sa: Side
    ta: Side


def _edge_groups(doc: Document) -> Iterator[tuple[BaseElement, EdgeSpec]]:
    for node in doc.svg.iter():
        if isinstance(node.tag, str) and node.get(_EDGE_ATTR) is not None:
            spec = read_edge_spec(node)
            if spec is not None:
                yield node, spec


def _write_route(doc: Document, leg: _Leg, a: Point, b: Point) -> None:
    theme = serving_theme(doc, leg.spec.kind)
    result = route_edge(
        a,
        leg.sa,
        b,
        leg.ta,
        route=leg.spec.route,
        stub=_token(theme, "--edge-stub", _DEFAULT_STUB),
        radius=_token(theme, "--edge-radius", _DEFAULT_RADIUS),
    )
    path = _first_path(leg.element)
    if path is None:
        path = inkex.PathElement.new(result.d)
        leg.element.add(path)
        path.set_id(doc.new_id("edge-path"))
    path.set("d", result.d)
    path.style = inkex.Style({"fill": "none", "marker-end": f"url(#{_arrow_marker(doc)})"})
    canvas = theme.tokens.get("--canvas", _DEFAULT_CANVAS)
    _set_label(doc, leg.element, leg.spec.label, result.label_at, halo=canvas, dy=-4.0)


def _reroute(doc: Document, *, scope: set[str] | None = None) -> Reflow:
    """Re-derive every diagram edge's path from the CURRENT node boxes, ports and all.

    Port spreading is computed over ALL edges even when ``scope`` narrows what gets rewritten —
    a node's ports are shared, so an edge's anchor depends on its neighbours whether or not the
    caller asked about them.
    """

    def wanted(element: BaseElement, spec: EdgeSpec) -> bool:
        return scope is None or bool({str(element.get_id()), spec.source, spec.target} & scope)

    legs: list[_Leg] = []
    chosen: list[bool] = []
    skipped: list[str] = []
    for element, spec in _edge_groups(doc):
        source, target = _box_of(doc, spec.source), _box_of(doc, spec.target)
        if source is None or target is None:
            if wanted(element, spec):
                skipped.append(str(element.get_id()))
            continue
        sa, ta = resolve_sides(source, target, spec.sa, spec.ta)
        legs.append(_Leg(element, spec, source, target, sa, ta))
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

    rerouted = 0
    for index, leg in enumerate(legs):
        if not chosen[index]:
            continue
        a = anchor_point(leg.source, leg.sa, fractions[(index, True)])
        b = anchor_point(leg.target, leg.ta, fractions[(index, False)])
        _write_route(doc, leg, a, b)
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
    parent: str | None = None,
    name: str | None = None,
    styles: list[str] | None = None,
    themed: bool = True,
) -> PlacedEdge:
    """Connect two nodes with a routed, themed edge — and re-spread every port it shares.

    The edge stores what it connects, not where it runs, so it can be re-derived at any time:
    the path is computed now from both boxes and re-computed by ``reflow`` after they move. Sides
    default to whichever faces each other; several edges on one face fan out across it.

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
) -> EdgeEdit:
    """Edit a diagram edge by its SPEC — kind, route style, anchors, label — and re-route it.

    Changing an anchor re-spreads the faces it leaves and joins, so the edges sharing them move
    too; that is the point of storing the spec rather than the path.
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
            _set_container_label(
                doc, group, updated.label, box, _container_pad(doc, updated.kind)
            )
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
    result = _reroute(doc, scope=ids) if edges else Reflow()
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
