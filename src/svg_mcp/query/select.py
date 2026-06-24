"""Selection queries: find nodes by predicate, extract a subtree, extract embedded image data."""

from __future__ import annotations

import inkex

from ..model.document import Document
from .outline import OutlineNode, _summarize


def find(
    doc: Document,
    *,
    types: list[str] | None = None,
    name: str | None = None,
    name_contains: str | None = None,
    has_class: str | None = None,
    within: str | None = None,
) -> list[dict[str, str | None]]:
    """Find visual nodes matching all given predicates; returns a list of {id, tag, name}."""
    root = doc.svg if within is None else doc.resolve(within)
    results: list[dict[str, str | None]] = []
    for element in root.descendants():
        if element is root or not isinstance(element, (inkex.ShapeElement, inkex.Group)):
            continue
        label = getattr(element, "label", None)
        if types is not None and str(element.TAG) not in types:
            continue
        if name is not None and label != name:
            continue
        if name_contains is not None and (label is None or name_contains not in label):
            continue
        if has_class is not None and has_class not in (element.get("class") or "").split():
            continue
        results.append({"id": str(element.get_id()), "tag": str(element.TAG), "name": label})
    return results


def get_subtree(doc: Document, target: str) -> dict[str, str | OutlineNode]:
    """Return a node's subtree as both an SVG fragment and a structured outline."""
    element = doc.resolve(target)
    return {
        "svg": str(element.tostring().decode("utf-8")),
        "outline": _summarize(element, None, False),
    }


def extract_image(doc: Document, target: str) -> dict[str, str] | None:
    """Extract an ``<image>``'s embedded data: returns {mime, data_base64}, or None if external."""
    element = doc.resolve(target)
    href = element.get("xlink:href") or element.get("href")
    if not href or not href.startswith("data:"):
        return None
    header, _, data = href.partition(",")
    mime = header[len("data:") :].split(";")[0]
    return {"mime": mime, "data_base64": data}
