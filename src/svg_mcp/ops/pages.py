"""Document-furniture ops: guides and pages (Inkscape namedview)."""

from __future__ import annotations

import inkex

from ..model.document import Document

Point = tuple[float, float]


def add_guide(
    doc: Document, *, position: Point, angle: float = 90.0, name: str | None = None
) -> dict[str, str | None]:
    """Add a guide through ``position`` at ``angle`` degrees (90 = vertical); returns its id."""
    guide = inkex.Guide()
    doc.svg.namedview.add(guide)  # must be rooted before set_position (reads the viewBox)
    guide.set_id(doc.new_id("guide"))
    guide.set_position(position[0], position[1], angle)
    if name is not None:
        guide.label = name
    return {"id": str(guide.get_id()), "name": name}


def list_guides(doc: Document) -> list[dict[str, str | None]]:
    """List the document's guides."""
    return [
        {"id": str(guide.get_id()), "name": getattr(guide, "label", None)}
        for guide in doc.svg.namedview.get_guides()
    ]


def add_page(
    doc: Document,
    *,
    x: float,
    y: float,
    width: float,
    height: float,
    label: str | None = None,
) -> dict[str, str | None]:
    """Add a page (multi-page document); returns its id and label."""
    page = doc.svg.namedview.new_page(str(x), str(y), str(width), str(height), label)
    return {"id": str(page.get_id()), "label": label}


def list_pages(doc: Document) -> list[dict[str, str | None]]:
    """List the document's pages."""
    return [
        {"id": str(page.get_id()), "label": page.get("inkscape:label")}
        for page in doc.svg.namedview.get_pages()
    ]
