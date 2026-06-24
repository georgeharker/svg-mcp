"""Inspection queries: document description, computed style, transform/CTM, unit conversion."""

from __future__ import annotations

import inkex

from ..model.document import Document
from .outline import _bbox_xywh, _is_visual, _kind


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


def get_transform(doc: Document, target: str) -> dict[str, str | list[float]]:
    """Return a node's local transform and its composed transform-to-root (CTM)."""
    element = doc.resolve(target)
    local = element.transform
    composed = element.composed_transform()
    return {
        "local": str(local),
        "composed": str(composed),
        "local_matrix": [float(v) for v in local.to_hexad()],
        "composed_matrix": [float(v) for v in composed.to_hexad()],
    }


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
