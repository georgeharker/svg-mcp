"""Layer ops: list, state (visible/locked/opacity), rename, and move nodes between layers."""

from __future__ import annotations

import inkex

from ..model.document import Document
from ..model.handles import NodeRef, names_node


def _ref(element: object) -> NodeRef:
    return NodeRef(
        id=str(element.get_id()),  # type: ignore[attr-defined]
        tag=str(element.TAG),  # type: ignore[attr-defined]
        name=getattr(element, "label", None),
    )


def list_layers(doc: Document) -> list[dict[str, str | bool | None]]:
    """List the document's layers with their visible/locked state."""
    layers: list[dict[str, str | bool | None]] = []
    for element in doc.svg.descendants():
        if isinstance(element, inkex.Layer):
            layers.append(
                {
                    "id": str(element.get_id()),
                    "name": getattr(element, "label", None),
                    "visible": bool(element.is_visible()),
                    "locked": not bool(element.is_sensitive()),
                }
            )
    return layers


def set_layer_state(
    doc: Document,
    target: str,
    *,
    visible: bool | None = None,
    locked: bool | None = None,
    opacity: float | None = None,
) -> NodeRef:
    """Set a layer's (or group's) visibility, lock, and/or opacity."""
    element = doc.resolve(target)
    style = element.style
    if visible is not None:
        style["display"] = "inline" if visible else "none"
    if opacity is not None:
        style["opacity"] = str(opacity)
    element.style = style
    if locked is not None:
        element.set_sensitive(not locked)
    return _ref(element)


@names_node
def rename_layer(doc: Document, target: str, name: str) -> NodeRef:
    """Rename a layer (sets ``inkscape:label``)."""
    element = doc.resolve(target)
    element.label = name
    return _ref(element)


def move_to_layer(doc: Document, target: str, layer: str) -> NodeRef:
    """Move a node into a layer (or any group)."""
    element = doc.resolve(target)
    doc.resolve(layer).add(element)
    return _ref(element)
