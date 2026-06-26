"""Inspection queries: document description, computed style, transform/CTM, unit conversion."""

from __future__ import annotations

import json

import inkex

from ..model.document import Document
from .outline import _bbox_xywh, _is_visual, _kind

# One ancestor's contribution to a node's transform stack.
type TransformEntry = dict[str, str | None | list[float]]
# A node's geometry in a chosen coordinate frame.
type Geometry = dict[str, str | float | list[float] | dict[str, str]]
# A shape's editable settings under the add_*/edit_* parameter names.
type ParamValue = str | float | int | bool | list[float] | list[list[float]] | None
type ShapeParams = dict[str, ParamValue]


def describe_document(doc: Document) -> dict[str, str | int | None]:
    """Summarize the document: size, viewBox, unit, and layer/shape counts."""
    svg = doc.svg
    layers = sum(1 for e in svg.descendants() if isinstance(e, inkex.Layer))
    shapes = sum(
        1
        for e in svg.descendants()
        if isinstance(e, inkex.ShapeElement) and not isinstance(e, inkex.Group)
    )
    return {
        "width": svg.get("width"),
        "height": svg.get("height"),
        "viewBox": svg.get("viewBox"),
        "unit": str(svg.unit),
        "layers": layers,
        "shapes": shapes,
    }


def get_computed_style(doc: Document, target: str) -> dict[str, str]:
    """Return a node's fully cascaded/inherited presentation style as a flat dict."""
    style = doc.resolve(target).specified_style()
    return {str(key): str(value) for key, value in style.items()}


def _transform_stack(element: inkex.BaseElement) -> list[TransformEntry]:
    """Each ancestor's local transform, node-first up to the document root.

    The matrices multiply (left = nearest the node) to the composed CTM, so this shows *where*
    each part of a node's placement comes from — not just the flattened result.
    """
    stack: list[TransformEntry] = []
    node: inkex.BaseElement | None = element
    while node is not None:
        local = getattr(node, "transform", None)
        if local is None:
            break
        node_id = node.get_id() if hasattr(node, "get_id") else None
        stack.append(
            {
                "id": str(node_id) if node_id else None,
                "name": getattr(node, "label", None),
                "tag": str(getattr(node, "TAG", "")),
                "transform": str(local),
                "matrix": [float(v) for v in local.to_hexad()],
            }
        )
        node = node.getparent()
    return stack


def get_transform(
    doc: Document, target: str
) -> dict[str, str | list[float] | list[TransformEntry]]:
    """Return a node's local transform, its composed CTM, and the full per-ancestor stack."""
    element = doc.resolve(target)
    local = element.transform
    composed = element.composed_transform()
    return {
        "local": str(local),
        "composed": str(composed),
        "local_matrix": [float(v) for v in local.to_hexad()],
        "composed_matrix": [float(v) for v in composed.to_hexad()],
        "stack": _transform_stack(element),
    }


def _local_geometric_bbox(element: inkex.BaseElement) -> inkex.transforms.BoundingBox | None:
    """The element's geometric bbox in its OWN coordinate system (its own transform cancelled)."""
    try:
        return element.bounding_box(-element.transform)
    except Exception:
        return None


def _transform_bbox(
    matrix: inkex.Transform, bbox: inkex.transforms.BoundingBox
) -> tuple[float, float, float, float]:
    """Map a bbox through ``matrix`` (via its corners) and return ``(x, y, width, height)``."""
    left, top, width, height = bbox.left, bbox.top, bbox.width, bbox.height
    corners = [(left, top), (left + width, top), (left + width, top + height), (left, top + height)]
    pts = [matrix.apply_to_point(c) for c in corners]
    xs = [float(p[0]) for p in pts]
    ys = [float(p[1]) for p in pts]
    return min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys)


_GEOMETRY_KEYS = (
    "x",
    "y",
    "width",
    "height",
    "rx",
    "ry",
    "cx",
    "cy",
    "r",
    "x1",
    "y1",
    "x2",
    "y2",
    "points",
    "d",
)


def _shape_attrs(element: inkex.BaseElement) -> dict[str, str]:
    """The node's raw local geometry attributes (whichever it actually carries)."""
    return {k: str(v) for k in _GEOMETRY_KEYS if (v := element.get(k)) is not None}


def get_geometry(doc: Document, target: str, relative_to: str = "world") -> Geometry | None:
    """A node's position and size in a chosen coordinate frame, plus its raw local attributes.

    ``relative_to`` selects the frame:
      - ``"world"`` (default): document/global coordinates (full composed CTM applied).
      - ``"local"``: the node's own coordinate system (before any of its transforms).
      - ``"parent"``: the node's coordinates within its immediate parent (its own transform only).
      - any node id/name: the node's box expressed in THAT node's coordinate frame.

    Returns ``{frame, x, y, width, height, center, local}`` (``None`` if the node has no bbox).
    """
    element = doc.resolve(target)
    local_bb = _local_geometric_bbox(element)
    if local_bb is None:
        return None
    composed = element.composed_transform()
    if relative_to in ("world", ""):
        frame, matrix = "world", composed
    elif relative_to == "local":
        frame, matrix = "local", inkex.Transform()
    elif relative_to == "parent":
        frame, matrix = "parent", element.transform
    else:
        ref = doc.resolve(relative_to)
        frame, matrix = relative_to, (-ref.composed_transform()) @ composed
    x, y, width, height = _transform_bbox(matrix, local_bb)
    return {
        "frame": frame,
        "x": x,
        "y": y,
        "width": width,
        "height": height,
        "center": [x + width / 2, y + height / 2],
        "local": _shape_attrs(element),
    }


def _gf(element: inkex.BaseElement, attr: str) -> float | None:
    """Read a numeric attribute as float; ``None`` if absent or unparseable (never raises)."""
    value = element.get(attr)
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _gi(element: inkex.BaseElement, attr: str, default: int) -> int:
    """Read an integer attribute; ``default`` if absent or unparseable."""
    value = _gf(element, attr)
    return default if value is None else int(value)


_BASIC_GEOMETRY: dict[str, tuple[str, ...]] = {
    "rect": ("x", "y", "width", "height", "rx", "ry"),
    "circle": ("cx", "cy", "r"),
    "ellipse": ("cx", "cy", "rx", "ry"),
    "line": ("x1", "y1", "x2", "y2"),
}

# Custom generated primitives (ops.construct): (data-* attr, reported kind, param keys to read).
_PARAM_SHAPES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("data-squircle", "squircle", ("x", "y", "width", "height", "radius", "smoothness")),
    (
        "data-rounded-polygon",
        "rounded_polygon",
        ("cx", "cy", "radius", "sides", "corner_radius", "smoothness", "start_angle"),
    ),
    ("data-superellipse", "superellipse", ("cx", "cy", "rx", "ry", "exponent", "samples")),
    ("data-pill", "pill", ("x", "y", "width", "height", "smoothness")),
)


def _read_params(element: inkex.BaseElement) -> tuple[str, bool, ShapeParams]:
    """Dispatch a node to its (kind, parametric, params) — robust to missing/malformed markers."""
    sodipodi = element.get("sodipodi:type")
    if sodipodi == "star":
        flat = element.get(inkex.addNS("inkscape:flatsided", "inkscape")) == "true"
        return (
            "star",
            True,
            {
                "cx": _gf(element, "sodipodi:cx"),
                "cy": _gf(element, "sodipodi:cy"),
                "outer_radius": _gf(element, "sodipodi:r1"),
                "inner_radius": _gf(element, "sodipodi:r2"),
                "sides": _gi(element, "sodipodi:sides", 0),
                "rounded": _gf(element, inkex.addNS("inkscape:rounded", "inkscape")),
                "flatsided": flat,
            },
        )
    if sodipodi == "arc":
        return (
            "arc",
            True,
            {
                "cx": _gf(element, "sodipodi:cx"),
                "cy": _gf(element, "sodipodi:cy"),
                "rx": _gf(element, "sodipodi:rx"),
                "ry": _gf(element, "sodipodi:ry"),
                "arctype": element.get("sodipodi:arc-type") or "arc",
            },
        )
    vwp = element.get("data-vwp")
    if vwp is not None:
        try:
            spec = json.loads(vwp)
            return (
                "variable_width_path",
                True,
                {
                    "points": [[float(x), float(y)] for x, y in spec["points"]],
                    "widths": [float(w) for w in spec["widths"]],
                    "closed": bool(spec["closed"]),
                    "cap": str(spec["cap"]),
                    "interpolation": str(spec["interpolation"]),
                    "samples": int(spec["samples"]),
                },
            )
        except (ValueError, TypeError, KeyError):
            pass  # corrupt spec → fall through and report it as a plain path
    # Custom generated primitives stash their params in a data-* JSON attr (see ops.construct).
    for attr, kind, param_keys in _PARAM_SHAPES:
        raw = element.get(attr)
        if raw is None:
            continue
        try:
            spec = json.loads(raw)
            return kind, True, {k: float(spec[k]) for k in param_keys}
        except (ValueError, TypeError, KeyError):
            pass  # corrupt spec → fall through and report it as a plain path

    tag = str(element.TAG)
    keys = _BASIC_GEOMETRY.get(tag)
    if keys is not None:
        return tag, False, {k: _gf(element, k) for k in keys if element.get(k) is not None}
    if tag in ("polyline", "polygon"):
        return tag, False, {"points": element.get("points")}
    if tag == "path":
        return "path", False, {"d": element.get("d")}
    return tag, False, {}


def get_params(doc: Document, target: str) -> dict[str, str | bool | ShapeParams | dict[str, str]]:
    """A node's current settings under the SAME names the ``add_*``/``edit_*`` tools use, + style.

    Lets you read a shape, then edit it with matching params. Recognizes parametric stars/arcs,
    variable-width paths, and squircles (returns their generator parameters and
    ``parametric: true``); for basic
    shapes returns their geometry attributes; for a plain path, its ``d``. ``style`` is the node's
    current presentation properties (fill, stroke, …).

    Returns ``{kind, parametric, params, style}``.
    """
    element = doc.resolve(target)
    kind, parametric, params = _read_params(element)
    style = {str(key): str(value) for key, value in element.style.items()}
    return {"kind": kind, "parametric": parametric, "params": params, "style": style}


def convert_units(doc: Document, value: str, to_unit: str) -> float:
    """Convert a length like ``"10mm"`` into a number in ``to_unit`` (e.g. ``"px"``)."""
    user_units = doc.svg.unittouu(value)
    return float(doc.svg.uutounit(user_units, to_unit))


def describe_node(
    doc: Document, target: str
) -> dict[str, str | int | None | list[float] | dict[str, str]]:
    """Everything about one node in a single call: kind, world bbox, style, transform, parent."""
    element = doc.resolve(target)
    parent = element.getparent()
    parent_id = str(parent.get_id()) if parent is not None and hasattr(parent, "get_id") else None
    style = {str(k): str(v) for k, v in element.specified_style().items()}
    return {
        "id": str(element.get_id()),
        "name": getattr(element, "label", None),
        "tag": str(element.TAG),
        "kind": _kind(element),
        "parent": parent_id,
        "children": sum(1 for child in element if _is_visual(child)),
        "world_bbox": _bbox_xywh(element),
        "computed_style": style,
        "transform": {
            "local": str(element.transform),
            "composed": str(element.composed_transform()),
        },
    }


_DEFS_TAG_CATEGORY = {
    "linearGradient": "gradients",
    "radialGradient": "gradients",
    "meshgradient": "gradients",
    "pattern": "patterns",
    "filter": "filters",
    "clipPath": "clips",
    "mask": "masks",
    "marker": "markers",
    "symbol": "symbols",
}


def list_resources(doc: Document) -> dict[str, list[dict[str, str | None]]]:
    """List reusable resources defined in the document so they can be referenced/reused.

    Returns one bucket per kind (gradients, patterns, filters, clips, masks, markers, symbols)
    of ``{id, name}``, plus named ``styles`` (CSS classes) as ``{name}``.
    """
    buckets: dict[str, list[dict[str, str | None]]] = {
        category: []
        for category in ("gradients", "patterns", "filters", "clips", "masks", "markers", "symbols")
    }
    for child in doc.svg.defs:
        category = _DEFS_TAG_CATEGORY.get(str(getattr(child, "TAG", "")))
        if category is not None:
            buckets[category].append(
                {"id": str(child.get_id()), "name": getattr(child, "label", None)}
            )
    buckets["styles"] = [{"name": name} for name in doc.styles]
    return buckets
