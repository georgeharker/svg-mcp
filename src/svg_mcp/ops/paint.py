"""Shared paint-reference resolution for style dicts.

Tools accept an ``@name`` shorthand on ``fill``/``stroke`` to reference a defined resource
(gradient, pattern, …) by its friendly name. This rewrites those to the concrete ``url(#id)``
form. Used by every op that applies a style, so the shorthand works uniformly.
"""

from __future__ import annotations

from ..model.document import Document

Style = dict[str, str]


def resolve_paint_refs(doc: Document, style: Style | None) -> Style | None:
    """Rewrite ``@name`` paint shorthands on fill/stroke to ``url(#id)`` of a named def."""
    if not style:
        return style
    resolved = dict(style)
    for key in ("fill", "stroke"):
        value = resolved.get(key)
        if value and value.startswith("@"):
            resolved[key] = doc.resolve(value[1:]).get_id(as_url=2)
    return resolved
