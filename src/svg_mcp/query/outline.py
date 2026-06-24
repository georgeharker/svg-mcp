"""Read side: the document outline and bounding-box queries.

``outline`` is the AI's structural map — a depth-limited tree of named nodes it uses to
orient and re-orient mid-edit. Non-visual furniture (defs, namedview, metadata) is omitted.
"""

from __future__ import annotations

import inkex

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
