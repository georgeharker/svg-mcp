"""Reusable-resource ops: named styles, gradients, clip/mask, and filters.

All follow the inkex pattern "build an element in ``<defs>``, reference it by ``url(#id)``".
Each ``define_*`` returns the new resource's id; ``apply_*`` wires a target node to it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import inkex
from inkex import BaseElement
from lxml import etree

from ..model.document import Document
from ..model.errors import InvalidArgument
from ..model.handles import NodeRef, names_node
from .paint import resolve_paint_refs

Style = dict[str, str]
Stop = tuple[float, str, float]  # (offset, color, opacity)


@dataclass(slots=True)
class FePrimitive:
    """A filter-primitive node for the raw ``define_filter`` graph (recursive via children)."""

    tag: str
    attrs: dict[str, str] = field(default_factory=dict)
    children: list[FePrimitive] = field(default_factory=list)


def _ref(element: BaseElement) -> NodeRef:
    return NodeRef(
        id=str(element.get_id()), tag=str(element.TAG), name=getattr(element, "label", None)
    )


# Elements that live in <defs> / are not rendered on their own. Applying a clip, mask, filter,
# or marker to one is meaningless (a common footgun when a paint and a shape share a name).
_NON_RENDERABLE = frozenset(
    {
        "linearGradient",
        "radialGradient",
        "meshgradient",
        "pattern",
        "clipPath",
        "mask",
        "filter",
        "marker",
        "symbol",
        "stop",
        "style",
        "defs",
    }
)


def _ensure_renderable(element: BaseElement, op: str) -> None:
    """Reject applying ``op`` to a non-rendered definition element (gradient, clipPath, …)."""
    tag = str(element.TAG)
    local = tag.rsplit("}", 1)[-1]  # strip any namespace
    if local in _NON_RENDERABLE:
        raise InvalidArgument(
            f"{op} cannot target <{local}> {element.get_id()!r}: it is a definition, not a "
            "rendered shape (did a paint/resource and a shape share a name?)"
        )


def _set_prop(element: BaseElement, key: str, value: str) -> None:
    style = element.style
    style[key] = value
    element.style = style


# --- named styles (CSS classes) -------------------------------------------


def _sync_stylesheet(doc: Document) -> None:
    rules = [
        "." + name + " { " + "; ".join(f"{k}:{v}" for k, v in props.items()) + " }"
        for name, props in doc.styles.items()
    ]
    doc.stylesheet().set_text("\n".join(rules))


def define_style(doc: Document, name: str, props: Style) -> str:
    """Define (or redefine) a named style, emitted as a CSS class ``.name``.

    ``@name`` paint shorthands on fill/stroke are resolved to ``url(#id)`` so a class may
    reference a defined gradient/pattern.
    """
    doc.styles[name] = resolve_paint_refs(doc, props) or {}
    _sync_stylesheet(doc)
    return name


def edit_style(doc: Document, name: str, props: Style, *, replace: bool = False) -> str:
    """Edit a named style — MERGE ``props`` into it by default, or REPLACE it wholesale.

    Mirrors ``restyle``'s ergonomics for a CSS class: merging changes only the props you pass and
    keeps the rest. Every node carrying the class updates, since it's a shared ``<style>`` rule.
    Errors if the style isn't defined yet — use ``define_style`` to create it.
    """
    if name not in doc.styles:
        raise InvalidArgument(f"no named style {name!r}; create it with define_style")
    resolved = resolve_paint_refs(doc, props) or {}
    doc.styles[name] = resolved if replace else {**doc.styles[name], **resolved}
    _sync_stylesheet(doc)
    return name


def delete_style(doc: Document, name: str) -> str:
    """Delete a named style. Nodes still referencing the class keep their ``class`` attr but lose
    its rules. Errors if the style isn't defined."""
    if name not in doc.styles:
        raise InvalidArgument(f"no named style {name!r}")
    del doc.styles[name]
    _sync_stylesheet(doc)
    return name


def apply_styles(doc: Document, target: str, names: list[str]) -> NodeRef:
    """Apply named styles to a node by setting its ``class`` attribute."""
    element = doc.resolve(target)
    element.set("class", " ".join(names))
    return _ref(element)


# --- gradients -------------------------------------------------------------


def _add_stops(gradient: BaseElement, stops: list[Stop]) -> None:
    for offset, color, opacity in stops:
        stop = inkex.Stop()
        stop.set("offset", offset)
        stop.style["stop-color"] = color
        stop.style["stop-opacity"] = str(opacity)
        gradient.add(stop)


def define_linear_gradient(
    doc: Document,
    *,
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    stops: list[Stop],
    name: str | None = None,
    spread: str | None = None,
    units: str | None = None,
    gradient_transform: str | None = None,
) -> str:
    """Define a linear gradient in ``<defs>``; returns its id (reference it as ``url(#id)``)."""
    gradient = inkex.LinearGradient()
    for key, value in (("x1", x1), ("y1", y1), ("x2", x2), ("y2", y2)):
        gradient.set(key, value)
    if spread is not None:
        gradient.set("spreadMethod", spread)
    if units is not None:
        gradient.set("gradientUnits", units)
    if gradient_transform is not None:
        gradient.set("gradientTransform", gradient_transform)
    _add_stops(gradient, stops)
    return doc.add_def(gradient, "linearGradient", name)


def define_radial_gradient(
    doc: Document,
    *,
    cx: float,
    cy: float,
    r: float,
    stops: list[Stop],
    fx: float | None = None,
    fy: float | None = None,
    name: str | None = None,
    spread: str | None = None,
    units: str | None = None,
    gradient_transform: str | None = None,
) -> str:
    """Define a radial gradient in ``<defs>``; returns its id."""
    gradient = inkex.RadialGradient()
    for key, value in (("cx", cx), ("cy", cy), ("r", r)):
        gradient.set(key, value)
    if fx is not None:
        gradient.set("fx", fx)
    if fy is not None:
        gradient.set("fy", fy)
    if spread is not None:
        gradient.set("spreadMethod", spread)
    if units is not None:
        gradient.set("gradientUnits", units)
    if gradient_transform is not None:
        gradient.set("gradientTransform", gradient_transform)
    _add_stops(gradient, stops)
    return doc.add_def(gradient, "radialGradient", name)


# --- clip & mask -----------------------------------------------------------


def define_clip(
    doc: Document, *, content: list[str], name: str | None = None, units: str | None = None
) -> str:
    """Create a clipPath from existing nodes (moved into it); returns its id."""
    clip = inkex.ClipPath()
    if units is not None:
        clip.set("clipPathUnits", units)
    clip_id = doc.add_def(clip, "clipPath", name)
    for node in content:
        clip.add(doc.resolve(node))
    return clip_id


def define_mask(
    doc: Document, *, content: list[str], name: str | None = None, units: str | None = None
) -> str:
    """Create a mask from existing nodes (moved into it); returns its id."""
    mask = inkex.Mask()
    if units is not None:
        mask.set("maskUnits", units)
    mask_id = doc.add_def(mask, "mask", name)
    for node in content:
        mask.add(doc.resolve(node))
    return mask_id


def apply_clip(doc: Document, target: str, clip: str) -> NodeRef:
    element = doc.resolve(target)
    _ensure_renderable(element, "apply_clip")
    element.set("clip-path", doc.resolve(clip).get_id(as_url=2))
    return _ref(element)


def apply_mask(doc: Document, target: str, mask: str) -> NodeRef:
    element = doc.resolve(target)
    _ensure_renderable(element, "apply_mask")
    element.set("mask", doc.resolve(mask).get_id(as_url=2))
    return _ref(element)


def _prune_def(doc: Document, url_value: str | None) -> None:
    """Delete the referenced ``<defs>`` resource if nothing else still references it."""
    if not url_value:
        return
    match = re.search(r"#([^)\s]+)", str(url_value))
    if not match:
        return
    rid = match.group(1)
    for element in doc.svg.iter():
        for value in element.attrib.values():
            text = str(value)
            if f"#{rid})" in text or text == f"#{rid}":  # url(#rid) or href="#rid"
                return  # still referenced
    resource = doc.svg.getElementById(rid)
    if resource is not None:
        resource.delete()


def clear_clip(doc: Document, target: str) -> NodeRef:
    element = doc.resolve(target)
    previous = element.get("clip-path")
    element.pop("clip-path", None)
    _prune_def(doc, previous)
    return _ref(element)


def clear_mask(doc: Document, target: str) -> NodeRef:
    element = doc.resolve(target)
    previous = element.get("mask")
    element.pop("mask", None)
    _prune_def(doc, previous)
    return _ref(element)


# --- boolean operations (render-time, via clip/mask/compound path) ----------
#
# SVG has no native path booleans (Inkscape uses lib2geom in C++). Until a geometry engine is
# wired in, we realize the *visual* result with constructs that need no new dependency:
#   union        → group the inputs
#   intersection → clip the subject by each operand (clipPath)
#   difference   → mask the subject with the operands painted black (luminance mask)
#   exclusion    → merge all inputs into one compound path with fill-rule:evenodd (exact XOR)
# These produce a render construct, NOT a single re-editable merged `d`; a true geometry-level
# merge (offset-able, measurable as one path) awaits the engine. Assumes the targets share a
# coordinate space (siblings without conflicting ancestor transforms) — the common authoring case.

_BOOLEAN_OPS = ("union", "difference", "intersection", "exclusion")


def _new_in_tree(doc: Document, element: BaseElement, anchor: BaseElement, prefix: str) -> None:
    """Insert ``element`` into the tree just before ``anchor`` and give it a fresh id."""
    anchor.addprevious(element)
    element.set_id(doc.new_id(prefix))


def _union_bbox(elements: list[BaseElement]) -> inkex.BoundingBox | None:
    box: inkex.BoundingBox | None = None
    for el in elements:
        try:
            current = el.bounding_box()
        except Exception:
            current = None
        if current is not None:
            box = current if box is None else box + current
    return box


def _leaf_shapes(element: BaseElement) -> list[BaseElement]:
    """Flatten a (possibly composite) node to its renderable shape leaves.

    A group/layer/anchor/switch is descended into; anything else (a path/basic shape/text) is a
    leaf. Lets boolean operands be composite groups, not just single shapes.
    """
    if isinstance(element, inkex.Group | inkex.Layer) or str(element.TAG).rsplit("}", 1)[-1] in (
        "a",
        "switch",
    ):
        leaves: list[BaseElement] = []
        for child in element:
            if isinstance(child, BaseElement):
                leaves.extend(_leaf_shapes(child))
        return leaves
    return [element]


def _recolor_subtree(element: BaseElement, fill: str) -> None:
    """Force ``fill`` (and drop stroke) on every shape leaf so a whole subtree reads as one tone.

    Needed for mask-based ops: a group operand's children keep their own fills otherwise, so the
    luminance mask wouldn't treat the group as a single solid region.
    """
    for leaf in _leaf_shapes(element):
        leaf.style = inkex.Style({**dict(leaf.style), "fill": fill, "stroke": "none"})


def _reframe(element: BaseElement, dest_ct: inkex.Transform) -> None:
    """Rewrite ``element.transform`` so it keeps its WORLD position after moving into a new frame.

    ``dest_ct`` is the composed transform that maps the destination content space to world (e.g. for
    a clipPath/mask, the composed transform of the element that references it; for a sibling, its
    parent's composed transform). Same math as ``reparent``'s ``keep_world_position`` — without it,
    a flattened operand under any ancestor/subject transform would be doubly transformed.
    """
    element.transform = (-dest_ct) @ element.composed_transform()


@names_node
def boolean(doc: Document, *, op: str, targets: list[str], name: str | None = None) -> NodeRef:
    """Combine 2+ shapes with a boolean op, realized via clip/mask/compound path (no new deps).

    The FIRST target is the subject; the rest are operands. ``union`` groups them; ``intersection``
    clips the subject by the operands; ``difference`` subtracts the operands from the subject via a
    luminance mask; ``exclusion`` (XOR) merges everything into one fill-rule:evenodd compound path.

    WARNINGS — this is not a true geometry boolean, and it mutates/consumes the inputs:
      * The result is a RENDER-TIME construct (a clip/mask, or a merged compound path), NOT a single
        re-editable merged outline. You cannot then ``offset``/``get_bbox`` it as one path, and deep
        boolean chains get unwieldy. A real geometry-level merge awaits an engine (lib2geom).
      * Operands are CONSUMED: intersection moves them into a ``<clipPath>``, difference recolors
        them solid black and moves them into a ``<mask>``, exclusion bakes them into the compound
        path and deletes them. They no longer exist as independent nodes afterward.
      * A composite GROUP operand is FLATTENED to its shape leaves (the empty shell is removed) for
        intersection/exclusion, and RECOLORED solid black throughout for difference — so any
        per-child fills/strokes in that group are discarded. A group works fine as the *subject*.
      * Assumes targets share a coordinate space (siblings without conflicting ancestor transforms);
        operand leaf transforms are baked to world coordinates, but a transformed subject can still
        misalign the clip/mask.
    """
    if op not in _BOOLEAN_OPS:
        raise InvalidArgument(f"unknown boolean op {op!r}; choices: {list(_BOOLEAN_OPS)}")
    if len(targets) < 2:
        raise InvalidArgument("boolean needs at least 2 targets")
    elements = [doc.resolve(t) for t in targets]
    for el in elements:
        _ensure_renderable(el, f"boolean {op}")
    subject, operands = elements[0], elements[1:]

    if op == "union":
        group = inkex.Group.new(name or "")
        _new_in_tree(doc, group, subject, "g")
        for el in elements:
            group.add(el)
        if name is not None:
            group.label = name
        return _ref(group)

    if op == "intersection":
        result = subject
        for operand in operands:
            if result.get("clip-path") is not None:  # one clip-path per node — nest in a group
                group = inkex.Group.new("")
                _new_in_tree(doc, group, result, "g")
                group.add(result)
                result = group
            # The clip is applied to `result`; its content space maps to world by result's composed
            # transform. A clipPath can't legally hold a <g>, so flatten a group operand to its
            # shape leaves (their union clips the same region), reframed to keep their world place.
            dest_ct = result.composed_transform()
            leaves = _leaf_shapes(operand)
            for leaf in leaves:
                _reframe(leaf, dest_ct)
            clip = define_clip(doc, content=[str(leaf.get_id()) for leaf in leaves])
            # If the operand was a GROUP, its leaves were moved into the clip — drop the empty
            # shell. A single-shape operand IS its own leaf (now in the clip), so must NOT be
            # deleted, or the clipPath ends up empty and the subject clips to nothing.
            operand_is_shell = not (len(leaves) == 1 and leaves[0] is operand)
            if operand_is_shell and operand.getparent() is not None:
                operand.delete()
            apply_clip(doc, str(result.get_id()), clip)
        if name is not None:
            result.label = name
        return _ref(result)

    if op == "difference":
        box = _union_bbox(elements)  # world coords
        if box is None:
            raise InvalidArgument("difference needs targets with a measurable bounding box")
        # The mask content space maps to world by the subject's composed transform, so express the
        # world-coord backdrop and reframe each operand into that space.
        subj_ct = subject.composed_transform()
        pad = 2.0
        backdrop = inkex.Rectangle()
        backdrop.set("x", str(box.left - pad))
        backdrop.set("y", str(box.top - pad))
        backdrop.set("width", str(box.width + 2 * pad))
        backdrop.set("height", str(box.height + 2 * pad))
        backdrop.style = inkex.Style({"fill": "#ffffff"})
        backdrop.transform = -subj_ct  # world bbox → subject's mask frame
        _new_in_tree(doc, backdrop, subject, "rect")
        content = [str(backdrop.get_id())]
        for operand in operands:  # paint operands black (recursively) so they subtract
            _reframe(operand, subj_ct)
            _recolor_subtree(operand, "#000000")
            content.append(str(operand.get_id()))
        mask = define_mask(doc, content=content)
        apply_mask(doc, str(subject.get_id()), mask)
        if name is not None:
            subject.label = name
        return _ref(subject)

    # exclusion (XOR): merge every input's outline into one evenodd compound path. Groups are
    # flattened to their shape leaves; each leaf is expressed in the result path's frame (it lands
    # as a sibling of the subject) so nested-group transforms are concatenated correctly.
    parent = subject.getparent()
    dest_ct = parent.composed_transform() if parent is not None else inkex.Transform()
    combined: list[str] = []
    for el in elements:
        for leaf in _leaf_shapes(el):
            rel = (-dest_ct) @ leaf.composed_transform()
            combined.append(str(leaf.get_path().transform(rel)))
    result_path = inkex.PathElement.new(" ".join(combined))
    result_path.style = inkex.Style({**dict(subject.style), "fill-rule": "evenodd"})
    _new_in_tree(doc, result_path, subject, "path")
    if name is not None:
        result_path.label = name
    for el in elements:
        el.delete()
    return _ref(result_path)


# --- filters ---------------------------------------------------------------


def _fe(parent: BaseElement, tag: str, attrs: dict[str, object]) -> BaseElement:
    """Append a filter-primitive element (or child) with the given attributes."""
    element = etree.SubElement(parent, inkex.addNS(tag, "svg"))
    for key, value in attrs.items():
        if value is not None:
            element.set(key, str(value))
    return element


def _new_filter(doc: Document, name: str | None, region_pad: float = 0.5) -> BaseElement:
    """Create a <filter> in defs with a generous region so blurs/shadows aren't clipped."""
    flt = inkex.Filter()
    pad = region_pad
    for key, value in (
        ("x", f"-{pad * 100:g}%"),
        ("y", f"-{pad * 100:g}%"),
        ("width", f"{(1 + 2 * pad) * 100:g}%"),
        ("height", f"{(1 + 2 * pad) * 100:g}%"),
    ):
        flt.set(key, value)
    doc.add_def(flt, "filter", name)
    return flt


def _attach_filter(doc: Document, target: str, flt: BaseElement) -> NodeRef:
    element = doc.resolve(target)
    _set_prop(element, "filter", flt.get_id(as_url=2))
    return _ref(element)


def apply_blur(
    doc: Document, target: str, *, std_deviation: float, name: str | None = None
) -> NodeRef:
    """Gaussian-blur a node."""
    flt = _new_filter(doc, name)
    _fe(flt, "feGaussianBlur", {"in": "SourceGraphic", "stdDeviation": std_deviation})
    return _attach_filter(doc, target, flt)


def apply_drop_shadow(
    doc: Document,
    target: str,
    *,
    dx: float = 2,
    dy: float = 2,
    blur: float = 2,
    color: str = "#000000",
    opacity: float = 0.5,
    name: str | None = None,
) -> NodeRef:
    """Drop shadow, synthesized (inkex/SVG1.1 has no feDropShadow)."""
    flt = _new_filter(doc, name)
    _fe(flt, "feGaussianBlur", {"in": "SourceAlpha", "stdDeviation": blur, "result": "blur"})
    _fe(flt, "feOffset", {"in": "blur", "dx": dx, "dy": dy, "result": "off"})
    _fe(flt, "feFlood", {"flood-color": color, "flood-opacity": opacity, "result": "color"})
    _fe(flt, "feComposite", {"in": "color", "in2": "off", "operator": "in", "result": "shadow"})
    merge = _fe(flt, "feMerge", {})
    _fe(merge, "feMergeNode", {"in": "shadow"})
    _fe(merge, "feMergeNode", {"in": "SourceGraphic"})
    return _attach_filter(doc, target, flt)


def apply_color_matrix(
    doc: Document,
    target: str,
    *,
    type: str = "matrix",
    values: str | None = None,
    name: str | None = None,
) -> NodeRef:
    """feColorMatrix: type ``matrix``/``saturate``/``hueRotate``/``luminanceToAlpha``."""
    flt = _new_filter(doc, name, region_pad=0.0)
    _fe(flt, "feColorMatrix", {"in": "SourceGraphic", "type": type, "values": values})
    return _attach_filter(doc, target, flt)


def apply_color_overlay(
    doc: Document, target: str, *, color: str, opacity: float = 1.0, name: str | None = None
) -> NodeRef:
    """Tint a node by flooding a color and compositing it inside the source alpha."""
    flt = _new_filter(doc, name, region_pad=0.0)
    _fe(flt, "feFlood", {"flood-color": color, "flood-opacity": opacity, "result": "flood"})
    _fe(flt, "feComposite", {"in": "flood", "in2": "SourceGraphic", "operator": "in"})
    return _attach_filter(doc, target, flt)


def apply_blend(doc: Document, target: str, *, mode: str, name: str | None = None) -> NodeRef:
    """feBlend the node against itself (sets a blend mode via filter)."""
    flt = _new_filter(doc, name, region_pad=0.0)
    _fe(flt, "feBlend", {"in": "SourceGraphic", "in2": "SourceGraphic", "mode": mode})
    return _attach_filter(doc, target, flt)


def apply_morphology(
    doc: Document,
    target: str,
    *,
    operator: str = "dilate",
    radius: float = 1,
    name: str | None = None,
) -> NodeRef:
    """feMorphology: ``dilate`` (thicken) or ``erode`` (thin) by ``radius``."""
    flt = _new_filter(doc, name)
    _fe(flt, "feMorphology", {"in": "SourceGraphic", "operator": operator, "radius": radius})
    return _attach_filter(doc, target, flt)


def apply_component_transfer(
    doc: Document,
    target: str,
    *,
    func_type: str = "table",
    table_values: str | None = None,
    slope: float | None = None,
    intercept: float | None = None,
    name: str | None = None,
) -> NodeRef:
    """feComponentTransfer applied identically to R/G/B (levels, posterize, gamma)."""
    flt = _new_filter(doc, name, region_pad=0.0)
    transfer = _fe(flt, "feComponentTransfer", {"in": "SourceGraphic"})
    for channel in ("feFuncR", "feFuncG", "feFuncB"):
        _fe(
            transfer,
            channel,
            {
                "type": func_type,
                "tableValues": table_values,
                "slope": slope,
                "intercept": intercept,
            },
        )
    return _attach_filter(doc, target, flt)


def apply_turbulence(
    doc: Document,
    target: str,
    *,
    base_frequency: float,
    num_octaves: int = 1,
    type: str = "fractalNoise",
    seed: int = 0,
    name: str | None = None,
) -> NodeRef:
    """feTurbulence noise, clipped to the node's shape (texture overlay)."""
    flt = _new_filter(doc, name, region_pad=0.0)
    _fe(
        flt,
        "feTurbulence",
        {
            "type": type,
            "baseFrequency": base_frequency,
            "numOctaves": num_octaves,
            "seed": seed,
            "result": "noise",
        },
    )
    _fe(flt, "feComposite", {"in": "noise", "in2": "SourceGraphic", "operator": "in"})
    return _attach_filter(doc, target, flt)


def _build_fe(parent: BaseElement, primitive: FePrimitive) -> None:
    element = _fe(parent, primitive.tag, dict(primitive.attrs))
    for child in primitive.children:
        _build_fe(element, child)


def define_filter(
    doc: Document,
    *,
    primitives: list[FePrimitive],
    name: str | None = None,
    region_pad: float = 0.5,
) -> str:
    """Define a filter from a raw primitive graph in ``<defs>``; returns its id."""
    flt = _new_filter(doc, name, region_pad)
    for primitive in primitives:
        _build_fe(flt, primitive)
    return str(flt.get_id())


def apply_filter(doc: Document, target: str, filter: str) -> NodeRef:
    """Attach an existing filter (by id/name) to a node."""
    flt = doc.resolve(filter)
    return _attach_filter_element(doc, target, flt)


def _attach_filter_element(doc: Document, target: str, flt: BaseElement) -> NodeRef:
    element = doc.resolve(target)
    _ensure_renderable(element, "apply_filter")
    _set_prop(element, "filter", flt.get_id(as_url=2))
    return _ref(element)


# --- symbols, patterns, markers --------------------------------------------


def define_symbol(doc: Document, *, content: list[str], name: str | None = None) -> str:
    """Create a reusable ``<symbol>`` from existing nodes (moved into it); returns its id."""
    symbol = inkex.Symbol()
    symbol_id = doc.add_def(symbol, "symbol", name)
    for node in content:
        symbol.add(doc.resolve(node))
    return symbol_id


def define_pattern(
    doc: Document,
    *,
    content: list[str],
    width: float,
    height: float,
    x: float = 0,
    y: float = 0,
    units: str | None = None,
    pattern_transform: str | None = None,
    name: str | None = None,
) -> str:
    """Create a tiling ``<pattern>`` from existing nodes; returns its id (use as ``url(#id)``)."""
    pattern = inkex.Pattern()
    for key, value in (("x", x), ("y", y), ("width", width), ("height", height)):
        pattern.set(key, value)
    if units is not None:
        pattern.set("patternUnits", units)
    if pattern_transform is not None:
        pattern.set("patternTransform", pattern_transform)
    pattern_id = doc.add_def(pattern, "pattern", name)
    for node in content:
        pattern.add(doc.resolve(node))
    return pattern_id


def define_marker(
    doc: Document,
    *,
    content: list[str],
    ref_x: float = 0,
    ref_y: float = 0,
    marker_width: float = 10,
    marker_height: float = 10,
    orient: str = "auto",
    units: str = "strokeWidth",
    name: str | None = None,
) -> str:
    """Create a ``<marker>`` (arrowhead/dot) from existing nodes; returns its id."""
    marker = inkex.Marker()
    for key, value in (
        ("refX", ref_x),
        ("refY", ref_y),
        ("markerWidth", marker_width),
        ("markerHeight", marker_height),
        ("orient", orient),
        ("markerUnits", units),
    ):
        marker.set(key, value)
    marker_id = doc.add_def(marker, "marker", name)
    for node in content:
        marker.add(doc.resolve(node))
    return marker_id


# Arrowhead presets, drawn in a 0..10 marker viewBox pointing +x (forward along the path).
# Value: (path d, filled, refX). refX puts the tip ~at the path end with a slight overlap so a
# stroke tucks under the head. "dot" is a special-cased circle.
_ARROW_PRESETS: dict[str, tuple[str, bool, float]] = {
    "triangle": ("M0,0 L10,5 L0,10 Z", True, 9.0),
    "barbed": ("M0,0 L10,5 L0,10 L3.5,5 Z", True, 9.0),
    "stealth": ("M0,0 L10,5 L0,10 L4.5,5 Z", True, 9.5),
    "diamond": ("M0,5 L5,0 L10,5 L5,10 Z", True, 9.0),
    "open": ("M1,1 L9.5,5 L1,9", False, 8.5),  # stroked chevron (no fill)
    "dot": ("", True, 5.0),  # special-cased to a circle
}


def define_arrow_marker(
    doc: Document,
    *,
    preset: str = "triangle",
    size: float = 8.0,
    color: str = "#000000",
    stroke_width: float = 1.6,
    name: str | None = None,
) -> str:
    """Create an arrowhead/endpoint ``<marker>`` from a named preset; returns its id.

    A one-call convenience over ``define_marker``: it builds the head geometry for you. Apply it
    with ``apply_marker(target, id, position="end")`` (or start/mid). It is ``orient="auto"``
    so it follows the path direction, and ``markerUnits="strokeWidth"`` so it scales with the line.

    Args:
        preset: One of "triangle", "barbed", "stealth", "diamond", "open" (stroked chevron), "dot".
        size: Marker size in stroke-width multiples (markerWidth/Height).
        color: Head color — the fill for solid presets, the stroke for "open".
        stroke_width: Stroke width (in the 0..10 marker space) for the "open" preset.
        name: Friendly name, usable as the "@name" shorthand.

    Returns:
        The marker's id (use it as ``apply_marker``'s ``marker`` argument).
    """
    spec = _ARROW_PRESETS.get(preset)
    if spec is None:
        raise InvalidArgument(f"unknown arrow preset {preset!r}; choices: {sorted(_ARROW_PRESETS)}")
    path_d, filled, ref_x = spec
    marker = inkex.Marker()
    for key, value in (
        ("refX", ref_x),
        ("refY", 5.0),
        ("markerWidth", size),
        ("markerHeight", size),
        ("orient", "auto"),
        ("markerUnits", "strokeWidth"),
        ("viewBox", "0 0 10 10"),  # map the 0..10 geometry cleanly into the marker box
    ):
        marker.set(key, value)
    marker_id = doc.add_def(marker, "marker", name)
    shape: BaseElement
    if preset == "dot":
        shape = inkex.Circle.new((5.0, 5.0), 4.0)
        shape.style = inkex.Style({"fill": color})
    elif filled:
        shape = inkex.PathElement.new(path_d)
        shape.style = inkex.Style({"fill": color, "stroke": "none"})
    else:
        shape = inkex.PathElement.new(path_d)
        shape.style = inkex.Style(
            {"fill": "none", "stroke": color, "stroke-width": str(stroke_width)}
        )
    marker.add(shape)
    shape.set_id(doc.new_id("arrowhead"))
    return marker_id


def apply_marker(doc: Document, target: str, marker: str, position: str = "end") -> NodeRef:
    """Attach a marker to a path/line at ``start`` | ``mid`` | ``end``."""
    prop = {"start": "marker-start", "mid": "marker-mid", "end": "marker-end"}[position]
    element = doc.resolve(target)
    _ensure_renderable(element, "apply_marker")
    _set_prop(element, prop, doc.resolve(marker).get_id(as_url=2))
    return _ref(element)


def apply_displacement_map(
    doc: Document,
    target: str,
    *,
    scale: float = 10,
    base_frequency: float = 0.05,
    num_octaves: int = 2,
    name: str | None = None,
) -> NodeRef:
    """Warp a node using turbulence-driven feDisplacementMap (organic distortion)."""
    flt = _new_filter(doc, name)
    _fe(
        flt,
        "feTurbulence",
        {
            "type": "turbulence",
            "baseFrequency": base_frequency,
            "numOctaves": num_octaves,
            "result": "noise",
        },
    )
    _fe(
        flt,
        "feDisplacementMap",
        {
            "in": "SourceGraphic",
            "in2": "noise",
            "scale": scale,
            "xChannelSelector": "R",
            "yChannelSelector": "G",
        },
    )
    return _attach_filter(doc, target, flt)


# --- mesh gradient (advanced) ----------------------------------------------


def define_mesh_gradient(
    doc: Document, *, x: float, y: float, rows: int = 1, cols: int = 1, name: str | None = None
) -> str:
    """Define a (skeleton) mesh gradient at (x, y); returns its id. Limited renderer support."""
    mesh = inkex.MeshGradient.new_mesh((x, y), rows, cols)
    return doc.add_def(mesh, "meshgradient", name)
