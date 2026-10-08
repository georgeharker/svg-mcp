"""Shared paint-reference resolution for style dicts.

Tools accept an ``@name`` shorthand on ``fill``/``stroke`` to reference a defined resource
(gradient, pattern, …) by its friendly name. This rewrites those to the concrete ``url(#id)``
form. Used by every op that applies a style, so the shorthand works uniformly.
"""

from __future__ import annotations

import contextlib

from ..model.document import Document

Style = dict[str, str]

#: Marker properties resolve references exactly like paint, but by ELEMENT (the marker def),
#: not just paint servers — so a named style can carry a marker rule that survives reflow,
#: which the diagram router's inline style rewrites cannot take away.
_MARKER_KEYS = ("marker-start", "marker-mid", "marker-end")


def resolve_paint_refs(doc: Document, style: Style | None) -> Style | None:
    """Rewrite paint references on fill/stroke — and element references on the marker props —
    to ``url(#id)`` of a defined resource.

    Accepts the ``@name`` shorthand, and also ``url(#ref)`` where ``ref`` is a friendly *name*
    rather than the literal id — a common trap, since ``define_*`` returns a generated id but it's
    natural to reference the name you gave it. A ``url(#id)`` that already points at a real id is
    left untouched; an unresolvable reference is left as-is (it surfaces normally downstream).
    On a marker prop, a plain id or name (no ``@``, no ``url()``) is accepted too, and ``none``
    is the explicit "no glyph here" and passes through untouched.
    """
    if not style:
        return style
    resolved = dict(style)
    for key in ("fill", "stroke", *_MARKER_KEYS):
        value = resolved.get(key)
        if not value or value == "none":
            continue
        if value.startswith("@"):
            resolved[key] = doc.resolve(value[1:]).get_id(as_url=2)
        elif value.startswith("url(#") and value.endswith(")"):
            ref = value[5:-1]
            if doc.svg.getElementById(ref) is None:  # not a real id — maybe a friendly name
                with contextlib.suppress(Exception):  # unresolvable → leave as-is
                    resolved[key] = doc.resolve(ref).get_id(as_url=2)
        elif key in _MARKER_KEYS:
            # A bare marker id or friendly name — resolve it now, since a class rule that
            # says ``marker-start: triangle`` is not valid CSS.
            with contextlib.suppress(Exception):  # unresolvable → leave as-is
                resolved[key] = doc.resolve(value).get_id(as_url=2)
    return resolved
