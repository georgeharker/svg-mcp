"""Path-construction factories (arc, star) and path-data ops (transform/convert/bbox)."""

from __future__ import annotations

import inkex
from inkex import BaseElement

from ..model.document import Document
from ..model.errors import InvalidArgument
from ..model.handles import NodeRef
from .construct import Style, _place
from .geometry import _demote_parametric, _merge_style_and_transform


def _ref(element: BaseElement) -> NodeRef:
    return NodeRef(
        id=str(element.get_id()), tag=str(element.TAG), name=getattr(element, "label", None)
    )


def _required_params(
    element: BaseElement, target: str, attrs: tuple[str, ...], kind: str
) -> dict[str, float]:
    """Read the parametric attrs that MUST be present to rebuild a shape — strictly.

    A missing or non-numeric parameter means the node isn't a complete parametric ``kind`` (it may
    have been imported partial, or hand-edited). Rather than silently default — which would move or
    resize the shape — we raise and tell the caller to edit it as a plain path instead.
    """
    out: dict[str, float] = {}
    missing: list[str] = []
    for attr in attrs:
        value = element.get(attr)
        if value is None:
            missing.append(attr)
            continue
        try:
            out[attr] = float(value)
        except (TypeError, ValueError):
            raise InvalidArgument(
                f"{target!r} is a parametric {kind} but {attr}={value!r} is not numeric; "
                "edit it as a plain path with edit_path"
            ) from None
    if missing:
        raise InvalidArgument(
            f"{target!r} is a parametric {kind} but is missing parameter(s) {missing}; "
            "edit it as a plain path with edit_path"
        )
    return out


def _rederive(element: BaseElement, fresh: BaseElement) -> None:
    """Copy a freshly-built parametric shape's geometry+params onto ``element`` (keeping its id).

    ``fresh`` carries only ``d`` and the sodipodi/inkscape parameter attrs, so this updates the
    rendered path and the stored parameters together while leaving id/style/transform/clip intact.
    """
    for key, value in fresh.attrib.items():
        if key != "id":
            element.set(key, value)


def add_arc(
    doc: Document,
    *,
    cx: float,
    cy: float,
    rx: float,
    ry: float | None = None,
    arctype: str = "arc",
    parent: str | None = None,
    name: str | None = None,
    style: Style | None = None,
    transform: str | None = None,
) -> NodeRef:
    """Add an arc/pie/chord as a path. ``arctype``: ``arc`` | ``slice`` | ``chord``."""
    element = inkex.PathElement.arc((cx, cy), rx, ry, arctype=arctype)
    return _place(
        doc, element, prefix="path", parent=parent, name=name, style=style, transform=transform
    )


def add_star(
    doc: Document,
    *,
    cx: float,
    cy: float,
    outer_radius: float,
    inner_radius: float,
    sides: int = 5,
    rounded: float = 0.0,
    flatsided: bool = False,
    parent: str | None = None,
    name: str | None = None,
    style: Style | None = None,
    transform: str | None = None,
) -> NodeRef:
    """Add a star/regular-polygon as a path (``flatsided=True`` → polygon, ignores inner_radius)."""
    element = inkex.PathElement.star(
        (cx, cy), (outer_radius, inner_radius), sides=sides, rounded=rounded, flatsided=flatsided
    )
    return _place(
        doc, element, prefix="path", parent=parent, name=name, style=style, transform=transform
    )


def edit_star(
    doc: Document,
    target: str,
    *,
    cx: float | None = None,
    cy: float | None = None,
    outer_radius: float | None = None,
    inner_radius: float | None = None,
    sides: int | None = None,
    rounded: float | None = None,
    flatsided: bool | None = None,
    style: Style | None = None,
    transform: str | None = None,
) -> NodeRef:
    """Edit a parametric star/polygon by its PARAMETERS, re-deriving the path (keeps id/style/clip).

    Errors if ``target`` isn't a parametric star (e.g. it was demoted to a plain path by a raw
    ``d`` edit) — use ``edit_path`` for those.
    """
    element = _require_path(doc, target)
    if element.get("sodipodi:type") != "star":
        raise InvalidArgument(
            f"{target!r} is not a parametric star (it may have been demoted to a plain path); "
            "use edit_path for plain paths"
        )
    rounded_attr = inkex.addNS("inkscape:rounded", "inkscape")
    flat_attr = inkex.addNS("inkscape:flatsided", "inkscape")
    cur = _required_params(
        element,
        target,
        (
            "sodipodi:cx",
            "sodipodi:cy",
            "sodipodi:r1",
            "sodipodi:r2",
            "sodipodi:sides",
            rounded_attr,
        ),
        "star",
    )
    flat_raw = element.get(flat_attr)
    if flat_raw is None:
        raise InvalidArgument(
            f"{target!r} is a parametric star but is missing inkscape:flatsided; "
            "edit it as a plain path with edit_path"
        )
    fresh = inkex.PathElement.star(
        (
            cx if cx is not None else cur["sodipodi:cx"],
            cy if cy is not None else cur["sodipodi:cy"],
        ),
        (
            outer_radius if outer_radius is not None else cur["sodipodi:r1"],
            inner_radius if inner_radius is not None else cur["sodipodi:r2"],
        ),
        sides=sides if sides is not None else int(cur["sodipodi:sides"]),
        rounded=rounded if rounded is not None else cur[rounded_attr],
        flatsided=flatsided if flatsided is not None else (flat_raw == "true"),
    )
    _rederive(element, fresh)
    _merge_style_and_transform(doc, element, style, transform)
    return _ref(element)


def edit_arc(
    doc: Document,
    target: str,
    *,
    cx: float | None = None,
    cy: float | None = None,
    rx: float | None = None,
    ry: float | None = None,
    arctype: str | None = None,
    style: Style | None = None,
    transform: str | None = None,
) -> NodeRef:
    """Edit a parametric arc/slice/chord by its PARAMETERS, re-deriving the path (keeps id/style).

    Errors if ``target`` isn't a parametric arc (e.g. demoted to a plain path) — use ``edit_path``.
    """
    element = _require_path(doc, target)
    if element.get("sodipodi:type") != "arc":
        raise InvalidArgument(
            f"{target!r} is not a parametric arc (it may have been demoted to a plain path); "
            "use edit_path for plain paths"
        )
    cur = _required_params(
        element, target, ("sodipodi:cx", "sodipodi:cy", "sodipodi:rx", "sodipodi:ry"), "arc"
    )
    fresh = inkex.PathElement.arc(
        (
            cx if cx is not None else cur["sodipodi:cx"],
            cy if cy is not None else cur["sodipodi:cy"],
        ),
        rx if rx is not None else cur["sodipodi:rx"],
        ry if ry is not None else cur["sodipodi:ry"],
        arctype=arctype if arctype is not None else (element.get("sodipodi:arc-type") or "arc"),
    )
    _rederive(element, fresh)
    _merge_style_and_transform(doc, element, style, transform)
    return _ref(element)


def _require_path(doc: Document, target: str) -> BaseElement:
    element = doc.resolve(target)
    if not isinstance(element, inkex.PathElement):
        raise InvalidArgument(f"{target!r} is not a path element")
    return element


def path_transform(doc: Document, target: str, transform: str) -> NodeRef:
    """Bake an SVG transform into a path's data (``d``), leaving the node transform untouched.

    A direct ``d`` rewrite, so a parametric star/arc is demoted to a plain path.
    """
    element = _require_path(doc, target)
    element.set_path(element.get_path().transform(inkex.Transform(transform)))
    _demote_parametric(element)
    return _ref(element)


def path_to_absolute(doc: Document, target: str) -> NodeRef:
    """Rewrite a path's data using absolute commands (demotes a parametric star/arc to a path)."""
    element = _require_path(doc, target)
    element.set_path(element.get_path().to_absolute())
    _demote_parametric(element)
    return _ref(element)


def path_to_relative(doc: Document, target: str) -> NodeRef:
    """Rewrite a path's data using relative commands (demotes a parametric star/arc to a path)."""
    element = _require_path(doc, target)
    element.set_path(element.get_path().to_relative())
    _demote_parametric(element)
    return _ref(element)


def path_bbox(doc: Document, target: str) -> dict[str, float] | None:
    """Return the geometric bounding box of a path's data (untransformed)."""
    element = _require_path(doc, target)
    box = element.get_path().bounding_box()
    if box is None:
        return None
    return {
        "x": float(box.left),
        "y": float(box.top),
        "width": float(box.width),
        "height": float(box.height),
    }
