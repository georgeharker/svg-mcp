"""Construction ops: add primitives, groups, and layers to a document.

Each function mutates the document in place and returns a :class:`NodeRef` handle. Styling is
a plain ``dict[str, str]`` of SVG presentation properties (the schema layer validates and
produces it); transforms are SVG transform strings.
"""

from __future__ import annotations

import base64
import mimetypes
from pathlib import Path

import inkex
from inkex import BaseElement

from ..model.document import Document
from ..model.handles import NodeRef

Style = dict[str, str]
Point = tuple[float, float]


def _resolve_paint_refs(doc: Document, style: Style | None) -> Style | None:
    """Rewrite ``@name`` paint shorthands on fill/stroke to ``url(#id)`` of a named def."""
    if not style:
        return style
    resolved = dict(style)
    for key in ("fill", "stroke"):
        value = resolved.get(key)
        if value and value.startswith("@"):
            resolved[key] = doc.resolve(value[1:]).get_id(as_url=2)
    return resolved


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
