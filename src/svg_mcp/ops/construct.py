"""Construction ops: add primitives, groups, and layers to a document.

Each function mutates the document in place and returns a :class:`NodeRef` handle. Styling is
a plain ``dict[str, str]`` of SVG presentation properties (the schema layer validates and
produces it); transforms are SVG transform strings.
"""

from __future__ import annotations

import base64
import bisect
import json
import math
import mimetypes
from collections.abc import Callable
from pathlib import Path

import inkex
from inkex import BaseElement

from ..geom import (
    Cubic,
    offset_cubic_subpath,
    rounded_polygon_outline,
    squircle_outline,
    superellipse_outline,
    variable_width_outline,
)
from ..model.document import Document
from ..model.errors import InvalidArgument
from ..model.handles import NodeRef
from ..typeset import FontNotFound, glyph_run, is_bold, parse_font_size, text_on_path_d
from .geometry import _merge_style_and_transform
from .paint import resolve_paint_refs as _resolve_paint_refs

Style = dict[str, str]
Point = tuple[float, float]

# A variable-width ribbon is a *generated* fill with no native parametric type, so we stash the
# centerline + widths + options here, the same way stars/arcs carry their sodipodi params, so
# `edit_variable_width_path` can re-derive it. A raw `d` edit strips it (see `_demote_parametric`).
_VWP_ATTR = "data-vwp"

# Generated-fill primitives with no native Inkscape parametric type stash their generator params
# in a custom data-* attribute (the same way stars/arcs carry sodipodi params), so the matching
# `edit_*` op can re-derive the path. A raw `d` edit strips it (see `_demote_parametric`).
_SQUIRCLE_ATTR = "data-squircle"
_ROUNDED_POLYGON_ATTR = "data-rounded-polygon"
_SUPERELLIPSE_ATTR = "data-superellipse"
_PILL_ATTR = "data-pill"


def _node_ref(element: BaseElement) -> NodeRef:
    return NodeRef(
        id=str(element.get_id()), tag=str(element.TAG), name=getattr(element, "label", None)
    )


def _store_param_spec(element: BaseElement, attr: str, spec: dict[str, float | int]) -> None:
    """Stash a generated primitive's parameters as a compact JSON ``data-*`` attribute."""
    element.set(attr, json.dumps(spec, separators=(",", ":")))


def _store_vwp_spec(
    element: BaseElement,
    *,
    points: list[Point],
    widths: list[float],
    closed: bool,
    cap: str,
    interpolation: str,
    samples: int,
) -> None:
    spec = {
        "points": [list(p) for p in points],
        "widths": list(widths),
        "closed": closed,
        "cap": cap,
        "interpolation": interpolation,
        "samples": samples,
    }
    element.set(_VWP_ATTR, json.dumps(spec, separators=(",", ":")))


def _apply_style(element: BaseElement, style: Style | None) -> None:
    if style:
        element.style = inkex.Style(style)


def _place(
    doc: Document,
    element: BaseElement,
    *,
    prefix: str,
    parent: str | None,
    name: str | None,
    style: Style | None,
    transform: str | None,
) -> NodeRef:
    """Attach a freshly built element: parent, id, name, style, transform → handle."""
    doc.resolve_parent(parent).add(element)
    element.set_id(doc.new_id(prefix))
    if name is not None:
        element.label = name
    _apply_style(element, _resolve_paint_refs(doc, style))
    if transform is not None:
        element.transform = inkex.Transform(transform)
    return NodeRef(id=str(element.get_id()), tag=str(element.TAG), name=name)


def add_rect(
    doc: Document,
    *,
    x: float,
    y: float,
    width: float,
    height: float,
    rx: float | None = None,
    ry: float | None = None,
    parent: str | None = None,
    name: str | None = None,
    style: Style | None = None,
    transform: str | None = None,
) -> NodeRef:
    element = inkex.Rectangle.new(x, y, width, height)
    if rx is not None:
        element.set("rx", rx)
    if ry is not None:
        element.set("ry", ry)
    return _place(
        doc, element, prefix="rect", parent=parent, name=name, style=style, transform=transform
    )


def add_circle(
    doc: Document,
    *,
    cx: float,
    cy: float,
    r: float,
    parent: str | None = None,
    name: str | None = None,
    style: Style | None = None,
    transform: str | None = None,
) -> NodeRef:
    element = inkex.Circle.new((cx, cy), r)
    return _place(
        doc, element, prefix="circle", parent=parent, name=name, style=style, transform=transform
    )


def add_ellipse(
    doc: Document,
    *,
    cx: float,
    cy: float,
    rx: float,
    ry: float,
    parent: str | None = None,
    name: str | None = None,
    style: Style | None = None,
    transform: str | None = None,
) -> NodeRef:
    element = inkex.Ellipse.new((cx, cy), (rx, ry))
    return _place(
        doc, element, prefix="ellipse", parent=parent, name=name, style=style, transform=transform
    )


def add_line(
    doc: Document,
    *,
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    parent: str | None = None,
    name: str | None = None,
    style: Style | None = None,
    transform: str | None = None,
) -> NodeRef:
    element = inkex.Line.new((x1, y1), (x2, y2))
    return _place(
        doc, element, prefix="line", parent=parent, name=name, style=style, transform=transform
    )


def _points_str(points: list[Point]) -> str:
    return " ".join(f"{x},{y}" for x, y in points)


def add_polyline(
    doc: Document,
    *,
    points: list[Point],
    parent: str | None = None,
    name: str | None = None,
    style: Style | None = None,
    transform: str | None = None,
) -> NodeRef:
    element = inkex.Polyline.new(_points_str(points))
    return _place(
        doc, element, prefix="polyline", parent=parent, name=name, style=style, transform=transform
    )


def add_polygon(
    doc: Document,
    *,
    points: list[Point],
    parent: str | None = None,
    name: str | None = None,
    style: Style | None = None,
    transform: str | None = None,
) -> NodeRef:
    element = inkex.Polygon.new(_points_str(points))
    return _place(
        doc, element, prefix="polygon", parent=parent, name=name, style=style, transform=transform
    )


def add_path(
    doc: Document,
    *,
    d: str,
    parent: str | None = None,
    name: str | None = None,
    style: Style | None = None,
    transform: str | None = None,
) -> NodeRef:
    element = inkex.PathElement.new(d)
    return _place(
        doc, element, prefix="path", parent=parent, name=name, style=style, transform=transform
    )


def add_variable_width_path(
    doc: Document,
    *,
    points: list[Point],
    widths: list[float],
    closed: bool = False,
    cap: str = "butt",
    interpolation: str = "linear",
    samples: int = 8,
    parent: str | None = None,
    name: str | None = None,
    style: Style | None = None,
    transform: str | None = None,
) -> NodeRef:
    """Expand a polyline centerline with per-vertex widths into a filled variable-width ribbon.

    The classic Power-Stroke operation: SVG strokes are constant-width, so swelling/tapering
    lines are drawn as a fill. ``widths`` is the full stroke width at each point (same length as
    ``points``). A closed centerline yields an annular ribbon (fill-rule evenodd). Set
    ``interpolation="cubic"`` to smooth the centerline and width via a Catmull-Rom spline.
    """
    if len(widths) != len(points):
        raise InvalidArgument("widths must have the same length as points")
    try:
        d = variable_width_outline(
            points, widths, closed=closed, cap=cap, interpolation=interpolation, samples=samples
        )
    except ValueError as exc:
        raise InvalidArgument(str(exc)) from exc
    if closed:
        style = {"fill-rule": "evenodd", **(style or {})}
    element = inkex.PathElement.new(d)
    _store_vwp_spec(
        element,
        points=points,
        widths=widths,
        closed=closed,
        cap=cap,
        interpolation=interpolation,
        samples=samples,
    )
    return _place(
        doc, element, prefix="path", parent=parent, name=name, style=style, transform=transform
    )


def edit_variable_width_path(
    doc: Document,
    target: str,
    *,
    points: list[Point] | None = None,
    widths: list[float] | float | None = None,
    closed: bool | None = None,
    cap: str | None = None,
    interpolation: str | None = None,
    samples: int | None = None,
    style: Style | None = None,
    transform: str | None = None,
) -> NodeRef:
    """Edit a variable-width ribbon by its SOURCE (centerline + widths), re-deriving the fill.

    Re-runs the power-stroke expansion from the stored centerline/widths with your overrides, so the
    fill stays a coherent ribbon (unlike hand-editing its outline ``d``). ``widths`` may be a single
    number (uniform). Errors if ``target`` has no stored spec — i.e. it isn't a variable-width path,
    or a raw ``d`` edit demoted it to a plain path; use ``edit_path`` for those.
    """
    element = doc.resolve(target)
    raw = element.get(_VWP_ATTR)
    if raw is None:
        raise InvalidArgument(
            f"{target!r} is not a variable-width path (or was demoted to a plain path); "
            "use edit_path for plain paths"
        )
    try:
        spec = json.loads(raw)
        stored_points = [(float(x), float(y)) for x, y in spec["points"]]
        stored_widths = [float(w) for w in spec["widths"]]
        stored = (spec["closed"], spec["cap"], spec["interpolation"], spec["samples"])
    except (ValueError, TypeError, KeyError) as exc:
        raise InvalidArgument(
            f"{target!r} has a corrupt variable-width spec ({exc}); "
            "edit it as a plain path with edit_path"
        ) from None
    stored_closed, stored_cap, stored_interp, stored_samples = stored
    new_points = points if points is not None else stored_points
    if widths is None:
        new_widths = stored_widths
    elif isinstance(widths, (int, float)):
        new_widths = [float(widths)] * len(new_points)
    else:
        new_widths = [float(w) for w in widths]
    new_closed = closed if closed is not None else bool(stored_closed)
    new_cap = cap if cap is not None else str(stored_cap)
    new_interp = interpolation if interpolation is not None else str(stored_interp)
    new_samples = samples if samples is not None else int(stored_samples)
    if len(new_widths) != len(new_points):
        raise InvalidArgument("widths must have the same length as points")
    try:
        d = variable_width_outline(
            new_points,
            new_widths,
            closed=new_closed,
            cap=new_cap,
            interpolation=new_interp,
            samples=new_samples,
        )
    except ValueError as exc:
        raise InvalidArgument(str(exc)) from exc
    element.set("d", d)
    _store_vwp_spec(
        element,
        points=new_points,
        widths=new_widths,
        closed=new_closed,
        cap=new_cap,
        interpolation=new_interp,
        samples=new_samples,
    )
    merged = {"fill-rule": "evenodd", **(style or {})} if new_closed else style
    _merge_style_and_transform(doc, element, merged, transform)
    return _node_ref(element)


def _store_squircle_spec(
    element: BaseElement,
    *,
    x: float,
    y: float,
    width: float,
    height: float,
    radius: float,
    smoothness: float,
) -> None:
    spec = {
        "x": x,
        "y": y,
        "width": width,
        "height": height,
        "radius": radius,
        "smoothness": smoothness,
    }
    element.set(_SQUIRCLE_ATTR, json.dumps(spec, separators=(",", ":")))


def add_squircle(
    doc: Document,
    *,
    x: float,
    y: float,
    width: float,
    height: float,
    radius: float,
    smoothness: float = 0.6,
    parent: str | None = None,
    name: str | None = None,
    style: Style | None = None,
    transform: str | None = None,
) -> NodeRef:
    """Add a SQUIRCLE — a rounded rectangle with iOS/Figma corner smoothing (Apple's icon shape).

    Unlike a plain rounded rect (circular corners), the corners ease into the arc with cubic
    Béziers, giving the continuous, organic look of Apple app icons. ``smoothness`` is the
    corner-smoothing fraction in ``[0, 1]``: ``0`` is a plain rounded rect, ``~0.6`` matches
    Apple's icons, ``1`` is maximally smooth. ``radius`` is clamped per corner to the shorter side.
    Stored parametrically so ``edit_squircle`` can re-derive it; a raw ``d`` edit demotes it to a
    plain path.
    """
    try:
        d = squircle_outline(x, y, width, height, radius, smoothness)
    except ValueError as exc:
        raise InvalidArgument(str(exc)) from exc
    element = inkex.PathElement.new(d)
    _store_squircle_spec(
        element, x=x, y=y, width=width, height=height, radius=radius, smoothness=smoothness
    )
    return _place(
        doc, element, prefix="path", parent=parent, name=name, style=style, transform=transform
    )


def edit_squircle(
    doc: Document,
    target: str,
    *,
    x: float | None = None,
    y: float | None = None,
    width: float | None = None,
    height: float | None = None,
    radius: float | None = None,
    smoothness: float | None = None,
    style: Style | None = None,
    transform: str | None = None,
) -> NodeRef:
    """Edit a parametric squircle by its PARAMETERS (mirrors add_squircle), re-deriving the path.

    Changes only the params you pass and regenerates the outline, keeping the node's id/style/
    z-order. Errors if ``target`` has no stored spec — i.e. it isn't a squircle, or a raw ``d``
    edit demoted it to a plain path; use ``edit_path`` for those. Read params with ``get_params``.
    """
    element = doc.resolve(target)
    raw = element.get(_SQUIRCLE_ATTR)
    if raw is None:
        raise InvalidArgument(
            f"{target!r} is not a squircle (or was demoted to a plain path); "
            "use edit_path for plain paths"
        )
    try:
        spec = json.loads(raw)
        cur = (
            float(spec["x"]),
            float(spec["y"]),
            float(spec["width"]),
            float(spec["height"]),
            float(spec["radius"]),
            float(spec["smoothness"]),
        )
    except (ValueError, TypeError, KeyError) as exc:
        raise InvalidArgument(
            f"{target!r} has a corrupt squircle spec ({exc}); "
            "edit it as a plain path with edit_path"
        ) from None
    new_x = x if x is not None else cur[0]
    new_y = y if y is not None else cur[1]
    new_width = width if width is not None else cur[2]
    new_height = height if height is not None else cur[3]
    new_radius = radius if radius is not None else cur[4]
    new_smoothness = smoothness if smoothness is not None else cur[5]
    try:
        d = squircle_outline(new_x, new_y, new_width, new_height, new_radius, new_smoothness)
    except ValueError as exc:
        raise InvalidArgument(str(exc)) from exc
    element.set("d", d)
    _store_squircle_spec(
        element,
        x=new_x,
        y=new_y,
        width=new_width,
        height=new_height,
        radius=new_radius,
        smoothness=new_smoothness,
    )
    _merge_style_and_transform(doc, element, style, transform)
    return _node_ref(element)


def add_rounded_polygon(
    doc: Document,
    *,
    cx: float,
    cy: float,
    radius: float,
    corner_radius: float,
    sides: int = 6,
    smoothness: float = 0.6,
    start_angle: float = -90.0,
    parent: str | None = None,
    name: str | None = None,
    style: Style | None = None,
    transform: str | None = None,
) -> NodeRef:
    """Add a regular N-gon with smoothed corners — the squircle idea generalized to ``sides`` sides.

    A convex regular polygon inscribed in ``radius``, each vertex rounded by ``corner_radius`` and
    eased by ``smoothness`` (0 = crisp circular-ish fillet, 1 = softer/continuous). ``start_angle``
    orients the first vertex (−90° = pointing up). Stored parametrically so ``edit_rounded_polygon``
    can re-derive it; a raw ``d`` edit demotes it to a plain path.
    """
    try:
        d = rounded_polygon_outline(cx, cy, radius, sides, corner_radius, smoothness, start_angle)
    except ValueError as exc:
        raise InvalidArgument(str(exc)) from exc
    element = inkex.PathElement.new(d)
    _store_param_spec(
        element,
        _ROUNDED_POLYGON_ATTR,
        {
            "cx": cx,
            "cy": cy,
            "radius": radius,
            "sides": sides,
            "corner_radius": corner_radius,
            "smoothness": smoothness,
            "start_angle": start_angle,
        },
    )
    return _place(
        doc, element, prefix="path", parent=parent, name=name, style=style, transform=transform
    )


def edit_rounded_polygon(
    doc: Document,
    target: str,
    *,
    cx: float | None = None,
    cy: float | None = None,
    radius: float | None = None,
    sides: int | None = None,
    corner_radius: float | None = None,
    smoothness: float | None = None,
    start_angle: float | None = None,
    style: Style | None = None,
    transform: str | None = None,
) -> NodeRef:
    """Edit a parametric rounded polygon by its PARAMETERS (mirrors add_rounded_polygon).

    Changes only the params you pass and regenerates the outline, keeping the node's id/style/
    z-order. Errors if ``target`` has no stored spec (not a rounded polygon, or a raw ``d`` edit
    demoted it); use ``edit_path`` for those. Read current params with ``get_params``.
    """
    element = doc.resolve(target)
    raw = element.get(_ROUNDED_POLYGON_ATTR)
    if raw is None:
        raise InvalidArgument(
            f"{target!r} is not a rounded polygon (or was demoted to a plain path); "
            "use edit_path for plain paths"
        )
    try:
        spec = json.loads(raw)
        new_cx = cx if cx is not None else float(spec["cx"])
        new_cy = cy if cy is not None else float(spec["cy"])
        new_radius = radius if radius is not None else float(spec["radius"])
        new_sides = sides if sides is not None else int(spec["sides"])
        new_cr = corner_radius if corner_radius is not None else float(spec["corner_radius"])
        new_smooth = smoothness if smoothness is not None else float(spec["smoothness"])
        new_angle = start_angle if start_angle is not None else float(spec["start_angle"])
    except (ValueError, TypeError, KeyError) as exc:
        raise InvalidArgument(
            f"{target!r} has a corrupt rounded-polygon spec ({exc}); "
            "edit it as a plain path with edit_path"
        ) from None
    try:
        d = rounded_polygon_outline(
            new_cx, new_cy, new_radius, new_sides, new_cr, new_smooth, new_angle
        )
    except ValueError as exc:
        raise InvalidArgument(str(exc)) from exc
    element.set("d", d)
    _store_param_spec(
        element,
        _ROUNDED_POLYGON_ATTR,
        {
            "cx": new_cx,
            "cy": new_cy,
            "radius": new_radius,
            "sides": new_sides,
            "corner_radius": new_cr,
            "smoothness": new_smooth,
            "start_angle": new_angle,
        },
    )
    _merge_style_and_transform(doc, element, style, transform)
    return _node_ref(element)


def add_superellipse(
    doc: Document,
    *,
    cx: float,
    cy: float,
    rx: float,
    ry: float,
    exponent: float = 4.0,
    samples: int = 128,
    parent: str | None = None,
    name: str | None = None,
    style: Style | None = None,
    transform: str | None = None,
) -> NodeRef:
    """Add a Lamé SUPERELLIPSE — one continuous curve, no edges or corners (distinct from squircle).

    ``|x/rx|^n + |y/ry|^n = 1``: the ``exponent`` n morphs the whole silhouette — n=1 a diamond, n=2
    an ellipse, n≈4 a classic squircle look, large n toward a rectangle, n<1 a four-pointed astroid.
    Sampled as ``samples`` segments. Stored parametrically so ``edit_superellipse`` re-derives it;
    a raw ``d`` edit demotes it to a plain path.
    """
    try:
        d = superellipse_outline(cx, cy, rx, ry, exponent, samples)
    except ValueError as exc:
        raise InvalidArgument(str(exc)) from exc
    element = inkex.PathElement.new(d)
    _store_param_spec(
        element,
        _SUPERELLIPSE_ATTR,
        {"cx": cx, "cy": cy, "rx": rx, "ry": ry, "exponent": exponent, "samples": samples},
    )
    return _place(
        doc, element, prefix="path", parent=parent, name=name, style=style, transform=transform
    )


def edit_superellipse(
    doc: Document,
    target: str,
    *,
    cx: float | None = None,
    cy: float | None = None,
    rx: float | None = None,
    ry: float | None = None,
    exponent: float | None = None,
    samples: int | None = None,
    style: Style | None = None,
    transform: str | None = None,
) -> NodeRef:
    """Edit a parametric superellipse by its PARAMETERS (mirrors add_superellipse), re-deriving it.

    Changes only the params you pass and regenerates the curve, keeping the node's id/style/z-order.
    Errors if ``target`` has no stored spec (not a superellipse, or a raw ``d`` edit demoted it);
    use ``edit_path`` for those. Read current params with ``get_params``.
    """
    element = doc.resolve(target)
    raw = element.get(_SUPERELLIPSE_ATTR)
    if raw is None:
        raise InvalidArgument(
            f"{target!r} is not a superellipse (or was demoted to a plain path); "
            "use edit_path for plain paths"
        )
    try:
        spec = json.loads(raw)
        new_cx = cx if cx is not None else float(spec["cx"])
        new_cy = cy if cy is not None else float(spec["cy"])
        new_rx = rx if rx is not None else float(spec["rx"])
        new_ry = ry if ry is not None else float(spec["ry"])
        new_exp = exponent if exponent is not None else float(spec["exponent"])
        new_samples = samples if samples is not None else int(spec["samples"])
    except (ValueError, TypeError, KeyError) as exc:
        raise InvalidArgument(
            f"{target!r} has a corrupt superellipse spec ({exc}); "
            "edit it as a plain path with edit_path"
        ) from None
    try:
        d = superellipse_outline(new_cx, new_cy, new_rx, new_ry, new_exp, new_samples)
    except ValueError as exc:
        raise InvalidArgument(str(exc)) from exc
    element.set("d", d)
    _store_param_spec(
        element,
        _SUPERELLIPSE_ATTR,
        {
            "cx": new_cx,
            "cy": new_cy,
            "rx": new_rx,
            "ry": new_ry,
            "exponent": new_exp,
            "samples": new_samples,
        },
    )
    _merge_style_and_transform(doc, element, style, transform)
    return _node_ref(element)


def add_pill(
    doc: Document,
    *,
    x: float,
    y: float,
    width: float,
    height: float,
    smoothness: float = 0.0,
    parent: str | None = None,
    name: str | None = None,
    style: Style | None = None,
    transform: str | None = None,
) -> NodeRef:
    """Add a PILL / stadium — a rectangle whose short sides are fully rounded into semicircles.

    The corner radius is fixed at half the shorter side (so the short ends are exact semicircles).
    ``smoothness`` > 0 gives the iOS/Figma corner-smoothed variant (a softer "super-pill"); default
    0 is the classic stadium. Stored parametrically so ``edit_pill`` can re-derive it; a raw ``d``
    edit demotes it to a plain path.
    """
    try:
        d = squircle_outline(x, y, width, height, min(width, height) / 2.0, smoothness)
    except ValueError as exc:
        raise InvalidArgument(str(exc)) from exc
    element = inkex.PathElement.new(d)
    _store_param_spec(
        element,
        _PILL_ATTR,
        {"x": x, "y": y, "width": width, "height": height, "smoothness": smoothness},
    )
    return _place(
        doc, element, prefix="path", parent=parent, name=name, style=style, transform=transform
    )


def edit_pill(
    doc: Document,
    target: str,
    *,
    x: float | None = None,
    y: float | None = None,
    width: float | None = None,
    height: float | None = None,
    smoothness: float | None = None,
    style: Style | None = None,
    transform: str | None = None,
) -> NodeRef:
    """Edit a parametric pill/stadium by its PARAMETERS (mirrors add_pill), re-deriving the path.

    Changes only the params you pass; the corner radius stays half the shorter side. Errors if
    ``target`` has no stored spec (not a pill, or a raw ``d`` edit demoted it); use ``edit_path``
    for those. Read current params with ``get_params``.
    """
    element = doc.resolve(target)
    raw = element.get(_PILL_ATTR)
    if raw is None:
        raise InvalidArgument(
            f"{target!r} is not a pill (or was demoted to a plain path); "
            "use edit_path for plain paths"
        )
    try:
        spec = json.loads(raw)
        new_x = x if x is not None else float(spec["x"])
        new_y = y if y is not None else float(spec["y"])
        new_width = width if width is not None else float(spec["width"])
        new_height = height if height is not None else float(spec["height"])
        new_smooth = smoothness if smoothness is not None else float(spec["smoothness"])
    except (ValueError, TypeError, KeyError) as exc:
        raise InvalidArgument(
            f"{target!r} has a corrupt pill spec ({exc}); edit it as a plain path with edit_path"
        ) from None
    try:
        d = squircle_outline(
            new_x, new_y, new_width, new_height, min(new_width, new_height) / 2.0, new_smooth
        )
    except ValueError as exc:
        raise InvalidArgument(str(exc)) from exc
    element.set("d", d)
    _store_param_spec(
        element,
        _PILL_ATTR,
        {
            "x": new_x,
            "y": new_y,
            "width": new_width,
            "height": new_height,
            "smoothness": new_smooth,
        },
    )
    _merge_style_and_transform(doc, element, style, transform)
    return _node_ref(element)


def _sibling_context(doc: Document, element: BaseElement) -> tuple[str | None, Style]:
    """Parent id (None = root) and a copy of the style, for placing an offset beside ``element``."""
    parent = element.getparent()
    parent_id = (
        str(parent.get_id())
        if parent is not None and not isinstance(parent, inkex.SvgDocumentElement)
        else None
    )
    style = {str(k): str(v) for k, v in element.style.items()}
    return parent_id, style


def _offset_parametric(
    doc: Document,
    element: BaseElement,
    distance: float,
    parent: str | None,
    style: Style,
    name: str | None,
) -> NodeRef | None:
    """Tier A — exact offset of a squircle/pill/rounded-polygon by regenerating its params.

    Returns a new parametric shape of the SAME kind (so it stays editable), or None if ``element``
    isn't one of those (then the caller falls back to the general path offset).
    """
    raw = element.get(_SQUIRCLE_ATTR)
    if raw is not None:
        s = json.loads(raw)
        return add_squircle(
            doc,
            x=float(s["x"]) - distance,
            y=float(s["y"]) - distance,
            width=float(s["width"]) + 2 * distance,
            height=float(s["height"]) + 2 * distance,
            radius=max(0.0, float(s["radius"]) + distance),
            smoothness=float(s["smoothness"]),
            parent=parent,
            name=name,
            style=style,
        )
    raw = element.get(_PILL_ATTR)
    if raw is not None:
        s = json.loads(raw)
        return add_pill(
            doc,
            x=float(s["x"]) - distance,
            y=float(s["y"]) - distance,
            width=float(s["width"]) + 2 * distance,
            height=float(s["height"]) + 2 * distance,
            smoothness=float(s["smoothness"]),
            parent=parent,
            name=name,
            style=style,
        )
    raw = element.get(_ROUNDED_POLYGON_ATTR)
    if raw is not None:
        s = json.loads(raw)
        sides = int(s["sides"])
        # Offsetting a regular polygon moves each edge out by `distance`: the apothem grows by
        # `distance`, so the circumradius grows by distance / cos(pi/sides); the fillet by distance.
        grow = distance / math.cos(math.pi / sides)
        return add_rounded_polygon(
            doc,
            cx=float(s["cx"]),
            cy=float(s["cy"]),
            radius=max(0.0, float(s["radius"]) + grow),
            sides=sides,
            corner_radius=max(0.0, float(s["corner_radius"]) + distance),
            smoothness=float(s["smoothness"]),
            start_angle=float(s["start_angle"]),
            parent=parent,
            name=name,
            style=style,
        )
    return None


def _subpath_segments(sub: list[list[list[float]]]) -> tuple[list[Cubic], bool]:
    def pt(p: list[float]) -> Point:
        return (float(p[0]), float(p[1]))

    segs: list[Cubic] = []
    for i in range(len(sub) - 1):
        segs.append((pt(sub[i][1]), pt(sub[i][2]), pt(sub[i + 1][0]), pt(sub[i + 1][1])))
    closed = len(sub) > 2 and math.dist(sub[0][1], sub[-1][1]) < 1e-6
    return segs, closed


def offset_path(
    doc: Document,
    target: str,
    distance: float,
    *,
    join: str = "round",
    miter_limit: float = 4.0,
    name: str | None = None,
) -> NodeRef:
    """Offset (parallel-curve / inset) a shape by ``distance``, returning a NEW node beside it.

    Positive ``distance`` grows a closed shape outward, negative insets it; for an open path it
    offsets to one side. A squircle/pill/rounded-polygon is offset EXACTLY by regenerating its
    parameters (and stays a re-editable parametric shape of the same kind). Anything else is offset
    by the analytic cubic-Bézier method (adaptive Tiller-Hanson + ``join`` corners) into a new plain
    path — APPROXIMATE, with no self-intersection trimming, so a large inward offset on a
    high-curvature/concave region can fold over itself. ``distance`` is in the target's local units.
    """
    element = doc.resolve(target)
    parent, style = _sibling_context(doc, element)
    parametric = _offset_parametric(doc, element, distance, parent, style, name)
    if parametric is not None:
        return parametric
    superpath = element.get_path().to_superpath()
    fragments = []
    for sub in superpath:
        segs, closed = _subpath_segments(sub)
        if segs:
            fragments.append(
                offset_cubic_subpath(
                    segs, distance, closed=closed, join=join, miter_limit=miter_limit
                )
            )
    if not fragments:
        raise InvalidArgument(f"{target!r} has no offsettable path geometry")
    new_element = inkex.PathElement.new(" ".join(fragments))
    if element.transform:
        new_element.transform = inkex.Transform(element.transform)
    return _place(
        doc, new_element, prefix="path", parent=parent, name=name, style=style, transform=None
    )


def add_text(
    doc: Document,
    *,
    x: float,
    y: float,
    content: str,
    parent: str | None = None,
    name: str | None = None,
    style: Style | None = None,
    transform: str | None = None,
) -> NodeRef:
    element = inkex.TextElement()
    element.set("x", x)
    element.set("y", y)
    element.text = content
    return _place(
        doc, element, prefix="text", parent=parent, name=name, style=style, transform=transform
    )


def create_group(
    doc: Document,
    *,
    name: str | None = None,
    parent: str | None = None,
    children: list[str] | None = None,
    transform: str | None = None,
) -> NodeRef:
    """Create a ``<g>``; optionally move existing nodes (by id/name) into it."""
    element = inkex.Group.new(name or "")
    ref = _place(
        doc, element, prefix="g", parent=parent, name=name, style=None, transform=transform
    )
    for child in children or []:
        element.add(doc.resolve(child))
    return ref


def create_layer(
    doc: Document,
    *,
    name: str,
    parent: str | None = None,
) -> NodeRef:
    """Create an Inkscape layer (a ``<g inkscape:groupmode="layer">``)."""
    element = inkex.Layer.new(name)
    return _place(
        doc, element, prefix="layer", parent=parent, name=name, style=None, transform=None
    )


def add_text_run(
    doc: Document,
    *,
    parent: str,
    text: str,
    x: float | None = None,
    y: float | None = None,
    dx: float | None = None,
    dy: float | None = None,
    name: str | None = None,
    style: Style | None = None,
) -> NodeRef:
    """Append a ``<tspan>`` run to an existing text (or tspan) node for multi-run/line text."""
    parent_element = doc.resolve(parent)
    tspan = inkex.Tspan()
    for key, value in (("x", x), ("y", y), ("dx", dx), ("dy", dy)):
        if value is not None:
            tspan.set(key, value)
    tspan.text = text
    parent_element.add(tspan)
    tspan.set_id(doc.new_id("tspan"))
    if name is not None:
        tspan.label = name
    _apply_style(tspan, _resolve_paint_refs(doc, style))
    return NodeRef(id=str(tspan.get_id()), tag=str(tspan.TAG), name=name)


def add_text_on_path(
    doc: Document,
    *,
    path: str,
    content: str,
    x: float | None = None,
    y: float | None = None,
    start_offset: str | None = None,
    side: str | None = None,
    parent: str | None = None,
    name: str | None = None,
    style: Style | None = None,
) -> NodeRef:
    """Add text flowing along a path: a ``<text>`` wrapping a ``<textPath>`` that references it."""
    text = inkex.TextElement()
    if x is not None:
        text.set("x", x)
    if y is not None:
        text.set("y", y)
    text_path = inkex.TextPath()
    text_path.href = doc.resolve(path)
    if start_offset is not None:
        text_path.set("startOffset", start_offset)
    if side is not None:
        text_path.set("side", side)
    text_path.text = content
    text.add(text_path)
    ref = _place(doc, text, prefix="text", parent=parent, name=name, style=style, transform=None)
    text_path.set_id(doc.new_id("textPath"))
    return ref


def add_image(
    doc: Document,
    *,
    x: float,
    y: float,
    width: float,
    height: float,
    href: str | None = None,
    data_base64: str | None = None,
    path: str | None = None,
    mime: str | None = None,
    preserve_aspect_ratio: str | None = None,
    parent: str | None = None,
    name: str | None = None,
    transform: str | None = None,
) -> NodeRef:
    """Add a raster ``<image>``. Provide exactly one source: an external ``href``, a
    ``data_base64`` string, or a local ``path`` (read and embedded as a base64 data URI)."""
    element = inkex.Image()
    for key, value in (("x", x), ("y", y), ("width", width), ("height", height)):
        element.set(key, value)
    if preserve_aspect_ratio is not None:
        element.set("preserveAspectRatio", preserve_aspect_ratio)

    if path is not None:
        raw = Path(path).read_bytes()
        resolved_mime = mime or mimetypes.guess_type(path)[0] or "image/png"
        encoded = base64.b64encode(raw).decode("ascii")
        element.set("xlink:href", f"data:{resolved_mime};base64,{encoded}")
    elif data_base64 is not None:
        element.set("xlink:href", f"data:{mime or 'image/png'};base64,{data_base64}")
    elif href is not None:
        element.set("xlink:href", href)

    return _place(
        doc, element, prefix="image", parent=parent, name=name, style=None, transform=transform
    )


def load_svg_document(*, svg: str | None = None, path: str | None = None) -> Document:
    """Build a :class:`Document` from SVG source given inline ``svg`` OR a file ``path``.

    Exactly one source must be provided. Reading from a path is preferred for large documents.
    """
    if (svg is None) == (path is None):
        raise InvalidArgument("provide exactly one of svg or path")
    text = Path(path).read_text(encoding="utf-8") if path is not None else svg
    assert text is not None  # narrowed by the xor check above
    return Document.from_svg(text)


def add_use(
    doc: Document,
    *,
    target: str,
    x: float = 0,
    y: float = 0,
    parent: str | None = None,
    name: str | None = None,
    transform: str | None = None,
) -> NodeRef:
    """Add a ``<use>`` instance referencing an existing node/symbol (by id/name)."""
    referenced = doc.resolve(target)
    element = inkex.Use.new(referenced, x, y)
    return _place(
        doc, element, prefix="use", parent=parent, name=name, style=None, transform=transform
    )


def unlink_use(doc: Document, target: str) -> NodeRef:
    """Expand a ``<use>`` into a real copy of its referenced content; returns the new node."""
    element = doc.resolve(target)
    expanded = element.unlink()
    return NodeRef(
        id=str(expanded.get_id()), tag=str(expanded.TAG), name=getattr(expanded, "label", None)
    )


def add_flowed_text(
    doc: Document,
    *,
    x: float,
    y: float,
    width: float,
    height: float,
    paragraphs: list[str],
    parent: str | None = None,
    name: str | None = None,
    style: Style | None = None,
) -> NodeRef:
    """Add Inkscape flowed text in a rectangular region (note: not universally rendered)."""
    root = inkex.FlowRoot()
    region = inkex.FlowRegion()
    region.add(inkex.Rectangle.new(x, y, width, height))
    root.add(region)
    for paragraph in paragraphs:
        para = inkex.FlowPara()
        para.text = paragraph
        root.add(para)
    return _place(
        doc, root, prefix="flowRoot", parent=parent, name=name, style=style, transform=None
    )


def wrap_in_link(
    doc: Document,
    *,
    href: str,
    children: list[str],
    parent: str | None = None,
    name: str | None = None,
) -> NodeRef:
    """Wrap existing nodes in an ``<a>`` hyperlink to ``href``."""
    anchor = inkex.Anchor.new(href)
    ref = _place(doc, anchor, prefix="a", parent=parent, name=name, style=None, transform=None)
    for child in children:
        anchor.add(doc.resolve(child))
    return ref


_PATH_PAINT_KEYS = (
    "fill",
    "stroke",
    "stroke-width",
    "fill-opacity",
    "stroke-opacity",
    "opacity",
    "stroke-linecap",
    "stroke-linejoin",
    "stroke-dasharray",
)


def _first_float(value: object, default: float = 0.0) -> float:
    """Parse the first number from an SVG attribute (which may be a space-separated list)."""
    if value in (None, ""):
        return default
    try:
        return float(str(value).split()[0])
    except (ValueError, IndexError):
        return default


def _run_font(style: BaseElement) -> tuple[str, float, bool, bool]:
    family = str(style.get("font-family") or "sans-serif").split(",")[0].strip().strip("'\"")
    size = parse_font_size(str(style.get("font-size") or ""))
    bold = is_bold(str(style.get("font-weight") or ""))
    italic = str(style.get("font-style") or "").strip().lower() in ("italic", "oblique")
    return family, size, bold, italic


def _paint_style(style: BaseElement) -> BaseElement:
    out = inkex.Style()
    for key in _PATH_PAINT_KEYS:
        value = style.get(key)
        if value is not None:
            out[key] = value
    return out


def _flatten_superpath(
    superpath: list[list[list[list[float]]]], steps: int = 24
) -> list[tuple[float, float]]:
    """Sample a CubicSuperPath into a dense (x, y) polyline (nodes are [in, point, out])."""
    points: list[tuple[float, float]] = []
    for sub in superpath:
        for i in range(len(sub) - 1):
            p0, c0 = sub[i][1], sub[i][2]  # on-curve point, its out-handle
            c1, p1 = sub[i + 1][0], sub[i + 1][1]  # next in-handle, next on-curve point
            for s in range(steps + 1):
                t = s / steps
                mt = 1.0 - t
                x = mt**3 * p0[0] + 3 * mt**2 * t * c0[0] + 3 * mt * t**2 * c1[0] + t**3 * p1[0]
                y = mt**3 * p0[1] + 3 * mt**2 * t * c0[1] + 3 * mt * t**2 * c1[1] + t**3 * p1[1]
                points.append((x, y))
    return points


def _make_sampler(
    points: list[tuple[float, float]],
) -> tuple[Callable[[float], tuple[float, float, float] | None], float]:
    """Build (sampler, total_length): sampler(distance) -> (x, y, tangent_radians) or None."""
    cumulative = [0.0]
    for i in range(1, len(points)):
        cumulative.append(
            cumulative[-1]
            + math.hypot(points[i][0] - points[i - 1][0], points[i][1] - points[i - 1][1])
        )
    total = cumulative[-1] if cumulative else 0.0

    def sample(distance: float) -> tuple[float, float, float] | None:
        if total == 0.0 or distance < 0.0 or distance > total:
            return None
        j = min(max(bisect.bisect_right(cumulative, distance) - 1, 0), len(points) - 2)
        span = cumulative[j + 1] - cumulative[j]
        f = 0.0 if span == 0.0 else (distance - cumulative[j]) / span
        (x0, y0), (x1, y1) = points[j], points[j + 1]
        return (x0 + (x1 - x0) * f, y0 + (y1 - y0) * f, math.atan2(y1 - y0, x1 - x0))

    return sample, total


def _parse_offset(value: object, total: float) -> float:
    text = "" if value is None else str(value).strip()
    if text.endswith("%"):
        return _first_float(text[:-1], 0.0) / 100.0 * total
    return _first_float(text, 0.0)


def _outline_text_on_path(doc: Document, element: BaseElement, text_path: BaseElement) -> NodeRef:
    content = text_path.text or ""
    if not content:
        raise InvalidArgument("textPath has no text content to outline")
    href = text_path.get("xlink:href") or text_path.get("href") or ""
    if not href.startswith("#"):
        raise InvalidArgument("textPath does not reference a path (xlink:href='#id')")
    referenced = doc.svg.getElementById(href[1:])
    if referenced is None:
        raise InvalidArgument(f"textPath references missing element {href!r}")
    try:
        superpath = referenced.get_path().to_superpath()
    except Exception as exc:
        raise InvalidArgument(f"cannot read referenced path geometry: {exc}") from exc

    points = _flatten_superpath(superpath)
    if referenced.transform:  # place text in the path's rendered coordinate space
        transform = referenced.transform
        points = [
            (float(mapped[0]), float(mapped[1]))
            for mapped in (transform.apply_to_point(p) for p in points)
        ]
    sampler, total = _make_sampler(points)

    style = text_path.specified_style()
    family, size, bold, italic = _run_font(style)
    start = _parse_offset(text_path.get("startOffset"), total)
    try:
        path_d = text_on_path_d(
            content,
            font_family=family,
            font_size=size,
            bold=bold,
            italic=italic,
            sampler=sampler,
            start_offset=start,
        )
    except FontNotFound as exc:
        raise InvalidArgument(str(exc)) from exc

    name = getattr(element, "label", None)
    parent = element.getparent()
    insert_at = parent.index(element)
    new_path = inkex.PathElement.new(path_d)
    new_path.style = _paint_style(style)
    if element.transform:
        new_path.transform = element.transform
    parent.insert(insert_at, new_path)
    new_path.set_id(doc.new_id("path"))
    if name is not None:
        new_path.label = name
    element.delete()
    return NodeRef(id=str(new_path.get_id()), tag=str(new_path.TAG), name=name)


def text_to_path(doc: Document, target: str) -> NodeRef:
    """Outline a text node into path geometry (glyphs baked, font-independent).

    Replaces the text in place, preserving its id, name, transform, and paint. ``<tspan>`` runs
    are flattened — each run keeps its own font (family/size/weight/italic) and fill, so a
    single path is produced for uniform text and a ``<g>`` of per-run paths for styled spans.
    Text-on-path (``<textPath>``) is outlined along the referenced curve (arc-length glyph
    walk). Pure-Python (fontTools); simple per-character advances (no kerning/ligatures).
    """
    element = doc.resolve(target)
    if not isinstance(element, inkex.TextElement | inkex.Tspan | inkex.TextPath):
        raise InvalidArgument(f"{target!r} is not a text element")

    # Text-on-path: outline along the referenced curve instead of a straight baseline.
    text_path = element if isinstance(element, inkex.TextPath) else None
    if text_path is None:
        text_path = next((c for c in element if isinstance(c, inkex.TextPath)), None)
    if text_path is not None:
        return _outline_text_on_path(doc, element, text_path)

    base_style = element.specified_style()
    # Collect runs in order: the element's direct text, then each tspan (and its tail text).
    runs: list[tuple[str, BaseElement, float | None, float | None, float | None, float | None]] = []
    if element.text:
        runs.append((element.text, base_style, None, None, None, None))
    for child in element:
        if isinstance(child, inkex.Tspan):
            child_style = child.specified_style()
            ax = _first_float(child.get("x"), 0.0) if child.get("x") else None
            ay = _first_float(child.get("y"), 0.0) if child.get("y") else None
            dx = _first_float(child.get("dx"), 0.0) if child.get("dx") else None
            dy = _first_float(child.get("dy"), 0.0) if child.get("dy") else None
            if child.text:
                runs.append((child.text, child_style, ax, ay, dx, dy))
            if child.tail:
                runs.append((child.tail, base_style, None, None, None, None))
    if not runs:
        raise InvalidArgument(f"{target!r} has no text content to outline")

    anchor = str(base_style.get("text-anchor") or "start").strip().lower()
    base_x = _first_float(element.get("x"), 0.0)
    base_y = _first_float(element.get("y"), 0.0)

    measured: list[
        tuple[
            str,
            str,
            float,
            bool,
            bool,
            float | None,
            float | None,
            float | None,
            float | None,
            float,
        ]
    ] = []
    try:
        for text, style, ax, ay, dx, dy in runs:
            family, size, bold, italic = _run_font(style)
            _d, width = glyph_run(
                text, font_family=family, font_size=size, bold=bold, italic=italic
            )
            measured.append((text, family, size, bold, italic, ax, ay, dx, dy, width))
    except FontNotFound as exc:
        raise InvalidArgument(str(exc)) from exc

    # paint per run, parallel to `runs`
    run_paint = [_paint_style(style) for _t, style, *_rest in runs]
    inline_total = sum(m[-1] for m in measured if m[5] is None)  # runs without an absolute x
    cursor_x = base_x + {"middle": -inline_total / 2.0, "end": -inline_total}.get(anchor, 0.0)
    cursor_y = base_y
    pieces: list[tuple[str, BaseElement]] = []
    for index, (text, family, size, bold, italic, ax, ay, dx, dy, width) in enumerate(measured):
        if ax is not None:
            cursor_x = ax + {"middle": -width / 2.0, "end": -width}.get(anchor, 0.0)
        if ay is not None:
            cursor_y = ay
        if dx is not None:
            cursor_x += dx
        if dy is not None:
            cursor_y += dy
        path_d, _w = glyph_run(
            text,
            font_family=family,
            font_size=size,
            bold=bold,
            italic=italic,
            x=cursor_x,
            y=cursor_y,
        )
        pieces.append((path_d, run_paint[index]))
        if ax is None:
            cursor_x += width

    name = getattr(element, "label", None)
    parent = element.getparent()
    insert_at = parent.index(element)
    transform = element.transform

    if len(pieces) == 1:
        node = inkex.PathElement.new(pieces[0][0])
        node.style = pieces[0][1]
        prefix = "path"
    else:
        node = inkex.Group()
        for piece_d, piece_style in pieces:
            run_path = inkex.PathElement.new(piece_d)
            run_path.style = piece_style
            node.add(run_path)
            run_path.set_id(doc.new_id("path"))
        prefix = "g"

    if transform:
        node.transform = transform
    parent.insert(insert_at, node)
    node.set_id(doc.new_id(prefix))
    if name is not None:
        node.label = name
    element.delete()
    return NodeRef(id=str(node.get_id()), tag=str(node.TAG), name=name)
