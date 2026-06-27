"""Metadata / accessibility ops: per-node title & description, document RDF metadata."""

from __future__ import annotations

import inkex
from inkex import BaseElement

from ..model.document import Document
from ..model.errors import InvalidArgument
from ..model.handles import NodeRef

# Top-level children that carry no rendered geometry — skipped when measuring content extent.
_NON_CONTENT = frozenset({"defs", "namedview", "metadata", "style"})


def _ref(element: BaseElement) -> NodeRef:
    return NodeRef(
        id=str(element.get_id()), tag=str(element.TAG), name=getattr(element, "label", None)
    )


def _set_child_text(element: BaseElement, child_class: type, text: str) -> None:
    for child in list(element):
        if isinstance(child, child_class):
            element.remove(child)
    node = child_class()
    node.text = text
    element.insert(0, node)


def set_title(doc: Document, target: str, text: str) -> NodeRef:
    """Set a node's ``<title>`` (accessibility / tooltip)."""
    element = doc.resolve(target)
    _set_child_text(element, inkex.Title, text)
    return _ref(element)


def set_description(doc: Document, target: str, text: str) -> NodeRef:
    """Set a node's ``<desc>`` (accessibility / long description)."""
    element = doc.resolve(target)
    _set_child_text(element, inkex.Desc, text)
    return _ref(element)


def set_document_metadata(
    doc: Document,
    *,
    title: str | None = None,
    creator: str | None = None,
    rights: str | None = None,
    date: str | None = None,
) -> dict[str, str | None]:
    """Set document-level RDF metadata fields (only the ones provided)."""
    metadata = doc.svg.metadata
    applied: dict[str, str | None] = {}
    for field_name, value in (
        ("doc_title", title),
        ("creator", creator),
        ("rights", rights),
        ("date", date),
    ):
        if value is not None and hasattr(metadata, field_name):
            setattr(metadata, field_name, value)
            applied[field_name] = value
    return applied


def _content_bbox(doc: Document) -> inkex.transforms.BoundingBox | None:
    """Union world-bbox of the document's rendered content (skips defs/namedview/metadata)."""
    box: inkex.transforms.BoundingBox | None = None
    for child in doc.svg:
        if str(getattr(child, "TAG", "")).rsplit("}", 1)[-1] in _NON_CONTENT:
            continue
        try:
            current = child.bounding_box()
        except Exception:
            current = None
        if current is not None:
            box = current if box is None else box + current
    return box


def _num(value: float) -> str:
    return f"{value:.4f}".rstrip("0").rstrip(".") or "0"


def resize_document(
    doc: Document,
    *,
    width: float | None = None,
    height: float | None = None,
    mode: str = "plain",
    margin: float = 0.0,
) -> dict[str, str | None]:
    """Resize the document canvas. Three modes:

    - ``plain``: set ``width``/``height`` and a 1:1 ``viewBox`` (``0 0 width height``) — the canvas
      grows or crops around the content, which keeps its user-space coordinates. Needs width+height.
    - ``scale``: set ``width``/``height`` but keep the current ``viewBox`` — the content scales to
      fill the new canvas. Needs width+height.
    - ``fit``: set the ``viewBox`` (and size) to the content's bounding box plus ``margin`` —
      shrink-wraps/crops to the artwork. ``width``/``height`` optional (default = the fitted box;
      if given, the content scales to that size).

    Returns the new ``{width, height, viewBox}``.
    """
    svg = doc.svg
    if mode == "fit":
        box = _content_bbox(doc)
        if box is None:
            raise InvalidArgument("resize_document mode='fit' needs some rendered content")
        vx, vy = box.left - margin, box.top - margin
        vw, vh = box.width + 2 * margin, box.height + 2 * margin
        svg.set("viewBox", f"{_num(vx)} {_num(vy)} {_num(vw)} {_num(vh)}")
        svg.set("width", _num(width if width is not None else vw))
        svg.set("height", _num(height if height is not None else vh))
    elif mode == "scale":
        if width is None or height is None:
            raise InvalidArgument("resize_document mode='scale' needs both width and height")
        if not svg.get("viewBox"):  # establish the current frame so content scales predictably
            svg.set("viewBox", f"0 0 {svg.get('width') or width} {svg.get('height') or height}")
        svg.set("width", _num(width))
        svg.set("height", _num(height))
    elif mode == "plain":
        if width is None or height is None:
            raise InvalidArgument("resize_document mode='plain' needs both width and height")
        svg.set("width", _num(width))
        svg.set("height", _num(height))
        svg.set("viewBox", f"0 0 {_num(width)} {_num(height)}")
    else:
        raise InvalidArgument(f"unknown resize mode {mode!r}; choices: plain, scale, fit")
    return {"width": svg.get("width"), "height": svg.get("height"), "viewBox": svg.get("viewBox")}
