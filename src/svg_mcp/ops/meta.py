"""Metadata / accessibility ops: per-node title & description, document RDF metadata."""

from __future__ import annotations

import inkex
from inkex import BaseElement

from ..model.document import Document
from ..model.handles import NodeRef


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
