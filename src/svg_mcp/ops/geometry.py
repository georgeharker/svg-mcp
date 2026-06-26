"""Geometry-editing ops: change a shape's coordinates/size/style/transform *in place*.

These mirror the ``add_*`` constructors (same geometry params, plus inline ``style`` and
``transform``) but mutate an existing node — so its id, clip, mask, filters, and z-order all
survive, unlike delete + re-add. The per-shape typed tools (``edit_rect``, ``edit_circle``, …)
are thin facades over the shared body here; the typing lives at the tool layer.
"""

from __future__ import annotations

import inkex

from ..model.document import Document
from ..model.errors import InvalidArgument
from ..model.handles import NodeRef
from .paint import resolve_paint_refs

Style = dict[str, str]


def _ref(element: object) -> NodeRef:
    return NodeRef(
        id=str(element.get_id()),  # type: ignore[attr-defined]
        tag=str(element.TAG),  # type: ignore[attr-defined]
        name=getattr(element, "label", None),
    )


def _merge_style_and_transform(
    doc: Document, element: inkex.BaseElement, style: Style | None, transform: str | None
) -> None:
    """Merge ``style`` (resolving ``@name`` paints, like restyle) and set the local transform."""
    if style:
        resolved = resolve_paint_refs(doc, style) or {}
        merged = element.style
        merged.update(resolved)
        element.style = merged
    if transform is not None:
        element.transform = inkex.Transform(transform)


def edit_shape(
    doc: Document,
    target: str,
    *,
    expect_tag: str,
    attrs: dict[str, float | str | None],
    style: Style | None = None,
    transform: str | None = None,
) -> NodeRef:
    """Edit a basic shape in place: set the given geometry attrs (+ optional style/transform).

    ``expect_tag`` guards the target's type, so e.g. ``edit_circle`` on a rect fails clearly. Only
    the attributes whose value is not ``None`` are changed; everything else is left as-is.
    """
    element = doc.resolve(target)
    tag = str(element.TAG)
    if tag != expect_tag:
        raise InvalidArgument(f"expected a <{expect_tag}> but {target!r} is a <{tag}>")
    for key, value in attrs.items():
        if value is not None:
            element.set(key, str(value))
    _merge_style_and_transform(doc, element, style, transform)
    return _ref(element)


def edit_path(
    doc: Document,
    target: str,
    d: str | None = None,
    *,
    style: Style | None = None,
    transform: str | None = None,
) -> NodeRef:
    """Edit a path in place: replace ``d`` (validated) and/or merge style / set transform.

    Works on a ``<path>`` or any path-derived node (star/arc). Editing the ``d`` of a parametric
    star/arc is a direct geometry change, so it demotes the node to a plain path (the parametric
    markers are dropped) — the path and its parameters can never then disagree.
    """
    element = doc.resolve(target)
    if element.get("d") is None and not isinstance(element, inkex.PathElement):
        raise InvalidArgument(
            f"node {target!r} (<{element.TAG}>) has no path data; "
            "use edit_rect/edit_circle/edit_ellipse/edit_line for basic shapes"
        )
    if d is not None:
        try:
            inkex.Path(d)  # validate syntax before committing
        except Exception as exc:
            raise InvalidArgument(f"invalid path data: {exc}") from exc
        element.set("d", d)
        _demote_parametric(element)
    _merge_style_and_transform(doc, element, style, transform)
    return _ref(element)


# Parametric-shape markers; a direct ``d`` edit invalidates them, so strip them — leaving an
# honest plain path rather than parameters that disagree with the geometry. Covers Inkscape's
# native star/arc attrs plus our own variable-width-path (``data-vwp``) and squircle
# (``data-squircle``) specs.
_PARAMETRIC_ATTRS = (
    "sodipodi:type",
    "sodipodi:sides",
    "sodipodi:cx",
    "sodipodi:cy",
    "sodipodi:r1",
    "sodipodi:r2",
    "sodipodi:arg1",
    "sodipodi:arg2",
    "sodipodi:rx",
    "sodipodi:ry",
    "sodipodi:start",
    "sodipodi:end",
    "sodipodi:open",
    "sodipodi:arc-type",
    "inkscape:rounded",
    "inkscape:randomized",
    "inkscape:flatsided",
    "data-vwp",
    "data-squircle",
)


def _demote_parametric(element: inkex.BaseElement) -> None:
    """Drop Inkscape parametric markers so a node edited via raw ``d`` becomes a plain path."""
    for attr in _PARAMETRIC_ATTRS:
        if element.get(attr) is not None:
            element.set(attr, None)
