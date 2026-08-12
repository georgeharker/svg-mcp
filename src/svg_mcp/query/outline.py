"""Read side: the document outline and bounding-box queries.

``outline`` is the AI's structural map — a depth-limited tree of named nodes it uses to
orient and re-orient mid-edit. Non-visual furniture (defs, namedview, metadata) is omitted.
"""

from __future__ import annotations

import inkex
from inkex import BaseElement

from ..model.document import Document

# A node in the outline tree: heterogeneous but precisely typed (no Any/object). The 3.12
# `type` statement makes the self-recursion resolvable by both mypy and pydantic.
type OutlineNode = dict[str, str | int | None | list[float] | list[OutlineNode]]


def _kind(element: object) -> str:
    if isinstance(element, inkex.Layer):
        return "layer"
    if isinstance(element, inkex.Group):
        return "group"
    if isinstance(element, inkex.SvgDocumentElement):
        return "document"
    return "shape"


def _is_visual(child: object) -> bool:
    """True for shapes, groups, and layers; False for defs/namedview/metadata/etc."""
    return isinstance(child, (inkex.ShapeElement, inkex.Group))


def _bbox_xywh(element: object) -> list[float] | None:
    """World-absolute bounding box [x, y, w, h] (ancestor transforms applied), or None."""
    try:
        parent = element.getparent()  # type: ignore[attr-defined]
        ctm = parent.composed_transform() if parent is not None else None
        box = element.bounding_box(ctm)  # type: ignore[attr-defined]
    except Exception:
        return None
    if box is None:
        return None
    return [float(box.left), float(box.top), float(box.width), float(box.height)]


def _inverse_frame(element: BaseElement) -> inkex.Transform:
    """The world → local transform for ``element``'s OWN coordinate space.

    ``_bbox_xywh`` answers in WORLD coordinates, but the numbers written onto a node (x/y/width,
    x1/y1/x2/y2, …) are read in the frame its ancestors' transforms establish. Anything that
    measures a box and then writes a child's geometry from it has to cross that boundary, or the
    child lands offset by every ancestor transform between them — and moves again on every edit.

    Pass the element being written, or (for a child that does not exist yet) the group it is
    about to join: a child with no transform of its own shares its parent's composed frame.
    """
    try:
        return -element.composed_transform()
    except Exception:  # pragma: no cover - a detached or transform-less node
        return inkex.Transform()


def _to_local_point(element: BaseElement, point: tuple[float, float]) -> tuple[float, float]:
    """A WORLD point expressed in ``element``'s own frame (see :func:`_inverse_frame`)."""
    moved = _inverse_frame(element).apply_to_point(point)
    return (float(moved[0]), float(moved[1]))


def _to_world_point(element: BaseElement, point: tuple[float, float]) -> tuple[float, float]:
    """A point in ``element``'s own frame expressed in WORLD coordinates — ``_to_local_point``
    the other way round. What anything that computes a position INSIDE a facade and then has to
    hand it to something outside (a callout's leader, say) crosses back over with."""
    try:
        frame = element.composed_transform()
    except Exception:  # pragma: no cover - a detached or transform-less node
        return (float(point[0]), float(point[1]))
    moved = frame.apply_to_point(point)
    return (float(moved[0]), float(moved[1]))


def _to_local_box(
    element: BaseElement, box: tuple[float, float, float, float]
) -> tuple[float, float, float, float]:
    """A WORLD ``(x, y, w, h)`` expressed in ``element``'s own frame, re-axis-aligned.

    All four corners are mapped and re-bounded, so a rotating or skewing ancestor still yields
    the smallest axis-aligned box holding the region rather than a nonsensical width.
    """
    inverse = _inverse_frame(element)
    corners = [
        inverse.apply_to_point((box[0] + dx * box[2], box[1] + dy * box[3]))
        for dx in (0.0, 1.0)
        for dy in (0.0, 1.0)
    ]
    xs = [float(corner[0]) for corner in corners]
    ys = [float(corner[1]) for corner in corners]
    return (min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys))


def _summarize(element: object, depth: int | None, include_bbox: bool) -> OutlineNode:
    node: OutlineNode = {
        "id": (element.get_id() or None) or None,  # type: ignore[attr-defined]
        "tag": str(element.TAG),  # type: ignore[attr-defined]
        "kind": _kind(element),
    }
    label = getattr(element, "label", None)
    if label:
        node["name"] = str(label)
    if include_bbox:
        node["bbox"] = _bbox_xywh(element)

    children = [child for child in element if _is_visual(child)]  # type: ignore[attr-defined]
    if children:
        if depth is not None and depth <= 0:
            node["children_count"] = len(children)
        else:
            next_depth = None if depth is None else depth - 1
            node["children"] = [_summarize(c, next_depth, include_bbox) for c in children]
    return node


def outline(
    doc: Document,
    *,
    root: str | None = None,
    depth: int | None = None,
    include_bbox: bool = False,
) -> OutlineNode:
    """Return a structured tree of the document (or a subtree rooted at ``root``)."""
    element = doc.svg if root is None else doc.resolve(root)
    return _summarize(element, depth, include_bbox)


def get_bbox(doc: Document, target: str) -> dict[str, float] | None:
    """Return the geometric bounding box of a node as ``{x,y,width,height}`` (None if empty)."""
    box = _bbox_xywh(doc.resolve(target))
    if box is None:
        return None
    return {"x": box[0], "y": box[1], "width": box[2], "height": box[3]}
