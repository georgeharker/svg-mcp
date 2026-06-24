"""Serialization: model -> SVG string (used for both export and render input)."""

from __future__ import annotations

from ..model.document import Document


def export_svg(doc: Document) -> str:
    """Serialize the document to an SVG string."""
    return doc.to_svg()
