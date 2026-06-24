"""Path-construction factories (arc, star) and path-data ops (transform/convert/bbox)."""

from __future__ import annotations

import inkex
from inkex import BaseElement

from ..model.document import Document
from ..model.errors import InvalidArgument
from ..model.handles import NodeRef
from .construct import Style, _place


def _ref(element: BaseElement) -> NodeRef:
    return NodeRef(
        id=str(element.get_id()), tag=str(element.TAG), name=getattr(element, "label", None)
    )


def add_arc(
    doc: Document,
    *,
    cx: float,
    cy: float,
    rx: float,
    ry: float | None = None,
    arctype: str = "arc",
    parent: str | None = None,
    name: str | None = None,
    style: Style | None = None,
    transform: str | None = None,
) -> NodeRef:
    """Add an arc/pie/chord as a path. ``arctype``: ``arc`` | ``slice`` | ``chord``."""
    element = inkex.PathElement.arc((cx, cy), rx, ry, arctype=arctype)
    return _place(
        doc, element, prefix="path", parent=parent, name=name, style=style, transform=transform
    )


def add_star(
    doc: Document,
    *,
    cx: float,
    cy: float,
    outer_radius: float,
    inner_radius: float,
    sides: int = 5,
    rounded: float = 0.0,
    flatsided: bool = False,
    parent: str | None = None,
    name: str | None = None,
    style: Style | None = None,
    transform: str | None = None,
) -> NodeRef:
    """Add a star/regular-polygon as a path (``flatsided=True`` → polygon, ignores inner_radius)."""
    element = inkex.PathElement.star(
        (cx, cy), (outer_radius, inner_radius), sides=sides, rounded=rounded, flatsided=flatsided
    )
    return _place(
        doc, element, prefix="path", parent=parent, name=name, style=style, transform=transform
    )


def _require_path(doc: Document, target: str) -> BaseElement:
    element = doc.resolve(target)
    if not isinstance(element, inkex.PathElement):
        raise InvalidArgument(f"{target!r} is not a path element")
    return element


def path_transform(doc: Document, target: str, transform: str) -> NodeRef:
    """Bake an SVG transform into a path's data (``d``), leaving the node transform untouched."""
    element = _require_path(doc, target)
    element.set_path(element.get_path().transform(inkex.Transform(transform)))
    return _ref(element)


def path_to_absolute(doc: Document, target: str) -> NodeRef:
    """Rewrite a path's data using absolute commands."""
    element = _require_path(doc, target)
    element.set_path(element.get_path().to_absolute())
    return _ref(element)


def path_to_relative(doc: Document, target: str) -> NodeRef:
    """Rewrite a path's data using relative commands."""
    element = _require_path(doc, target)
    element.set_path(element.get_path().to_relative())
    return _ref(element)


def path_bbox(doc: Document, target: str) -> dict[str, float] | None:
    """Return the geometric bounding box of a path's data (untransformed)."""
    element = _require_path(doc, target)
    box = element.get_path().bounding_box()
    if box is None:
        return None
    return {
        "x": float(box.left),
        "y": float(box.top),
        "width": float(box.width),
        "height": float(box.height),
    }
