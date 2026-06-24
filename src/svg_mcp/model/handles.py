"""Stable, AI-facing handles to nodes in a document."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True, frozen=True)
class NodeRef:
    """A handle the AI keeps after creating or locating a node.

    ``id`` is the precise, unique SVG id (the machine handle). ``name`` is the optional
    friendly label (``inkscape:label``). ``tag`` is the SVG element name (e.g. ``rect``).
    """

    id: str
    tag: str
    name: str | None = None

    def as_dict(self) -> dict[str, str | None]:
        return {"id": self.id, "tag": self.tag, "name": self.name}
