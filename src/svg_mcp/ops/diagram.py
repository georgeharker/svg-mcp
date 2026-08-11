"""Diagram facades: kind-shaped nodes, routed edges, and the reflow that keeps them attached.

A facade is a ``<g>`` that draws itself from a spec stored on it — a node is a shape plus a
centered label, an edge is a routed path plus an optional label. The spec lives in a compact
``data-*`` JSON attribute and is the single source of truth: ids and names belong to the caller,
so nothing here is ever derived from them.

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
from dataclasses import dataclass
from typing import Literal, cast

import inkex
from inkex import BaseElement
from pydantic import BaseModel, Field

from ..model.document import Document
from ..model.errors import InvalidArgument
from ..model.handles import NodeRef, names_node
from ..query.outline import _bbox_xywh
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
from .paint import resolve_paint_refs as _resolve_paint_refs
from .resources import _class_list, _set_class_list
from .themes import ServingTheme, apply_auto_styles, serving_theme

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
# The one arrowhead every edge shares, marked so it is found again without trusting its id.
_ARROW_ATTR = "data-diagram-arrow"
_ARROW_ID = "diagram-arrow"

_DEFAULT_PAD = 12.0
_DEFAULT_GAP = 24.0
_DEFAULT_STUB = 12.0
_DEFAULT_RADIUS = 8.0
_DEFAULT_CORNER = 6.0
_DEFAULT_CANVAS = "#ffffff"
_DEFAULT_FONT = "sans-serif"
_LABEL_SIZE = 12.0

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
    """What a reflow moved: edges re-routed, edges it could not, containers re-fitted."""

    edges_rerouted: int = 0
    skipped: list[str] = Field(default_factory=list)
    containers_refit: int = 0


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
    """What a diagram node is, as stored on its group."""

    kind: str
    label: str
    shape: str
    w: float
    h: float
    auto: bool


@dataclass(frozen=True, slots=True)
class EdgeSpec:
    """What a diagram edge connects, and how, as stored on its group."""

    source: str
    target: str
    kind: str
    sa: AnchorPref
    ta: AnchorPref
    route: RouteStyle
    label: str


def _store(element: BaseElement, attr: str, spec: Mapping[str, str | float | bool]) -> None:
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
        )
    except (ValueError, TypeError, KeyError):
        return None


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
    category: Literal["shape", "connector"],
    prim: str,
    role: str,
    parent: str | None,
    name: str | None,
    style: Style | None,
    styles: list[str] | None,
    themed: bool,
) -> NodeRef:
    """Attach a facade group: parent, id, name, the what-it-is stamp, its hooks, its own style.

    The same sequence ``_place_and_style`` runs for a primitive — the group is the themed node
    here, so the hooks land on it and its children are built plain and inherit.
    """
    doc.resolve_parent(parent).add(element)
    element.set_id(doc.new_id(prefix))
    if name is not None:
        element.label = name
    element.set(_CATEGORY_ATTR, category)
    element.set(_PRIM_ATTR, prim)
    apply_auto_styles(
        doc, element, category=category, prim=prim, role=role, styles=styles, themed=themed
    )
    resolved = _resolve_paint_refs(doc, style)
    if resolved:
        element.style = inkex.Style(resolved)
    return NodeRef(id=str(element.get_id()), tag=str(element.TAG), name=name)


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


def _label_style(halo: str | None) -> Style:
    """A facade label's structural style: centered, and haloed when it sits on top of a line."""
    style: Style = {"text-anchor": "middle", "dominant-baseline": "central"}
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
            style=_label_style(halo),
            themed=False,
        )
        existing = doc.resolve(ref.id)
    else:
        existing.text = text
        existing.set("x", _num(at[0]))
        existing.set("y", _num(at[1]))
    if dy is not None:
        existing.set("dy", _num(dy))


# --- nodes -------------------------------------------------------------------


def _diagram_nodes(parent: BaseElement) -> Iterator[BaseElement]:
    return (child for child in parent if child.get(_NODE_ATTR) is not None)


def _stack_below(parent: BaseElement, gap: float) -> float | None:
    """The y a new node stacks at: under the lowest diagram node already in this parent."""
    bottoms = [
        box[1] + box[3]
        for box in (_bbox_xywh(node) for node in _diagram_nodes(parent))
        if box is not None
    ]
    return max(bottoms) + gap if bottoms else None


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

    stacked = _stack_below(doc.resolve_parent(parent), gap)
    at_x = x if x is not None else 20.0
    at_y = y if y is not None else (20.0 if stacked is None else stacked)

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
    _build_shape(doc, shape, x=at_x, y=at_y, w=w, h=h, parent=ref.id, corner=corner)
    _set_label(doc, group, label, (at_x + w / 2.0, at_y + h / 2.0), halo=None, dy=None)
    _write_node_spec(group, NodeSpec(kind=kind, label=label, shape=shape, w=w, h=h, auto=auto))
    return PlacedNode(ref=ref, x=at_x, y=at_y, w=w, h=h)


def _swap_role(doc: Document, element: BaseElement, old: str, new: str) -> None:
    """Move a facade from one role class to another, leaving everything else it carries alone."""
    owned = {f"{theme}-{old}" for theme in doc.theme_meta}
    remaining = [cls for cls in _class_list(element) if cls not in owned]
    _set_class_list(element, remaining)
    apply_auto_styles(doc, element, category=None, role=new, themed=True)


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
    if kind is not None and kind != spec.kind:
        _swap_role(doc, group, spec.kind, kind)

    _write_node_spec(
        group,
        NodeSpec(
            kind=kind if kind is not None else spec.kind,
            label=label if label is not None else spec.label,
            shape=spec.shape,
            w=w,
            h=h,
            auto=spec.auto,
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
    if kind is not None and kind != spec.kind:
        _swap_role(doc, group, spec.kind, kind)
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
        ),
    )
    result = _reroute(doc)
    return EdgeEdit(
        ref=NodeRef(id=str(group.get_id()), tag=str(group.TAG), name=getattr(group, "label", None)),
        edges_rerouted=result.edges_rerouted,
    )


def reflow(
    doc: Document,
    *,
    edges: bool = True,
    containers: bool = True,
    scope: list[str] | None = None,
) -> Reflow:
    """Re-derive the diagram's derived geometry from what its nodes are doing NOW.

    Call it after moving, resizing, or deleting nodes: every edge is re-routed from the current
    boxes, re-choosing sides and re-spreading ports. An edge whose source or target no longer
    resolves is left exactly as it was and reported in ``skipped``.

    ``containers`` is accepted and currently does nothing — container facades re-fit themselves
    once they exist, and ``containers_refit`` is 0 until then.
    """
    if not edges:
        return Reflow()
    ids = {str(doc.resolve(entry).get_id()) for entry in scope} if scope else None
    return _reroute(doc, scope=ids)
