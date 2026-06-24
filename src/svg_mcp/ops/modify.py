"""Modification ops: transform, restyle, rename, reparent, reorder, delete."""

from __future__ import annotations

from typing import Literal

import inkex

from ..model.document import Document
from ..model.handles import NodeRef
from .paint import resolve_paint_refs

Style = dict[str, str]
Point = tuple[float, float]


def _ref(element: object) -> NodeRef:
    return NodeRef(
        id=str(element.get_id()),  # type: ignore[attr-defined]
        tag=str(element.TAG),  # type: ignore[attr-defined]
        name=getattr(element, "label", None),
    )


def set_name(doc: Document, target: str, name: str) -> NodeRef:
    element = doc.resolve(target)
    element.label = name
    return _ref(element)


def delete_node(doc: Document, target: str) -> str:
    element = doc.resolve(target)
    node_id = str(element.get_id())
    element.delete()
    return node_id


def restyle(doc: Document, target: str, style: Style, *, replace: bool = False) -> NodeRef:
    """Merge ``style`` into the node's presentation (or replace it wholesale).

    ``@name`` paint shorthands on fill/stroke are resolved to ``url(#id)``, just like at creation.
    """
    element = doc.resolve(target)
    resolved = resolve_paint_refs(doc, style) or {}
    if replace:
        element.style = inkex.Style(resolved)
    else:
        merged = element.style
        merged.update(resolved)
        element.style = merged
    return _ref(element)


def reparent(
    doc: Document,
    target: str,
    new_parent: str | None,
    index: int | None = None,
    keep_world_position: bool = False,
) -> NodeRef:
    """Move a node under a new parent (root if None), optionally at a child index.

    With ``keep_world_position``, the node's local transform is recomputed so it does not
    visually jump despite the change of ancestor transforms.
    """
    element = doc.resolve(target)
    parent_element = doc.resolve_parent(new_parent)
    if keep_world_position:
        world = element.composed_transform()
        parent_ctm = parent_element.composed_transform()
        element.transform = (-parent_ctm) @ world
    if index is None:
        parent_element.add(element)
    else:
        parent_element.insert(index, element)
    return _ref(element)


def _compose(doc: Document, target: str, applied: object) -> NodeRef:
    """Compose ``applied`` onto a node's transform, in parent space (before its existing one)."""
    element = doc.resolve(target)
    element.transform = applied @ element.transform
    return _ref(element)


def translate_node(doc: Document, target: str, dx: float, dy: float) -> NodeRef:
    """Move a node by (dx, dy)."""
    transform = inkex.Transform()
    transform.add_translate(dx, dy)
    return _compose(doc, target, transform)


def rotate_node(doc: Document, target: str, degrees: float, center: Point | None = None) -> NodeRef:
    """Rotate a node by ``degrees``, optionally about a center point (else its local origin)."""
    transform = inkex.Transform()
    if center is not None:
        transform.add_rotate(degrees, center)
    else:
        transform.add_rotate(degrees)
    return _compose(doc, target, transform)


def scale_node(
    doc: Document,
    target: str,
    sx: float,
    sy: float | None = None,
    center: Point | None = None,
) -> NodeRef:
    """Scale a node by (sx, sy) — ``sy`` defaults to ``sx`` — optionally about an anchor point."""
    sy = sx if sy is None else sy
    transform = inkex.Transform()
    if center is not None:
        cx, cy = center
        transform.add_translate(cx, cy)
        transform.add_scale(sx, sy)
        transform.add_translate(-cx, -cy)
    else:
        transform.add_scale(sx, sy)
    return _compose(doc, target, transform)


def skew_node(doc: Document, target: str, axis: Literal["x", "y"], degrees: float) -> NodeRef:
    """Skew a node along the x or y axis by ``degrees``."""
    transform = inkex.Transform()
    if axis == "x":
        transform.add_skewx(degrees)
    else:
        transform.add_skewy(degrees)
    return _compose(doc, target, transform)


def apply_transform(doc: Document, target: str, transform: str) -> NodeRef:
    """Compose any raw SVG transform string (e.g. ``"rotate(45 100 100)"``) onto a node."""
    return _compose(doc, target, inkex.Transform(transform))


def ungroup(doc: Document, target: str) -> list[str]:
    """Dissolve a group/layer, baking its transform into its children (preserving their world
    position) and moving them into the group's parent. Returns the freed children's ids."""
    group = doc.resolve(target)
    parent = group.getparent()
    index = parent.index(group)
    group_transform = group.transform
    moved: list[str] = []
    for child in list(group):
        child.transform = group_transform @ child.transform
        parent.insert(index, child)
        index += 1
        moved.append(str(child.get_id()))
    group.delete()
    return moved


def to_front(doc: Document, target: str) -> NodeRef:
    """Raise a node to the top of its parent's stacking order (drawn last)."""
    element = doc.resolve(target)
    element.getparent().append(element)
    return _ref(element)


def to_back(doc: Document, target: str) -> NodeRef:
    """Lower a node to the bottom of its parent's stacking order (drawn first)."""
    element = doc.resolve(target)
    element.getparent().insert(0, element)
    return _ref(element)


def raise_node(doc: Document, target: str) -> NodeRef:
    """Raise a node one step up its parent's stacking order."""
    element = doc.resolve(target)
    parent = element.getparent()
    index = parent.index(element)
    if index < len(parent) - 1:
        parent.remove(element)
        parent.insert(index + 1, element)
    return _ref(element)


def lower_node(doc: Document, target: str) -> NodeRef:
    """Lower a node one step down its parent's stacking order."""
    element = doc.resolve(target)
    parent = element.getparent()
    index = parent.index(element)
    if index > 0:
        parent.remove(element)
        parent.insert(index - 1, element)
    return _ref(element)


def duplicate(doc: Document, target: str, into: str | None = None) -> NodeRef:
    """Duplicate a node (fresh id, name suffixed ``-copy``); optionally move it into a parent.

    The copy's friendly name gets a ``-copy`` suffix so it doesn't collide with the original
    under name-based resolution.
    """
    element = doc.resolve(target)
    copy = element.duplicate()
    label = getattr(copy, "label", None)
    if label:
        copy.label = f"{label}-copy"
    if into is not None:
        doc.resolve_parent(into).add(copy)
    return _ref(copy)
