"""Metadata / accessibility ops: per-node title & description, document RDF metadata."""

from __future__ import annotations

import inkex
from inkex import BaseElement

from ..model.document import Document
from ..model.errors import InvalidArgument
from ..model.handles import NodeRef

# Top-level children that carry no rendered geometry — skipped when measuring content extent.
_NON_CONTENT = frozenset({"defs", "namedview", "metadata", "style"})


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


# Where each field lives under <metadata>/rdf:RDF/cc:Work, as Inkscape writes it. Agents
# (creator, rights) are wrapped in cc:Agent/dc:title.
_RDF_FIELDS: dict[str, tuple[str, ...]] = {
    "title": ("dc:title",),
    "date": ("dc:date",),
    "creator": ("dc:creator", "cc:Agent", "dc:title"),
    "rights": ("dc:rights", "cc:Agent", "dc:title"),
}


def _qname(prefixed: str) -> str:
    prefix, local = prefixed.split(":", 1)
    return str(inkex.addNS(local, prefix))


def _child(parent: BaseElement, prefixed: str) -> BaseElement:
    """The first child with this tag, created when missing."""
    tag = _qname(prefixed)
    found = parent.find(tag)
    if found is not None:
        return found
    created: BaseElement = inkex.etree.SubElement(parent, tag)
    return created


def _work(doc: Document) -> BaseElement:
    """<metadata>/rdf:RDF/cc:Work, created as needed (the root of Inkscape's RDF block)."""
    metadata = doc.svg.find(_qname("svg:metadata"))
    if metadata is None:
        metadata = inkex.etree.SubElement(doc.svg, _qname("svg:metadata"))
    rdf = metadata.find(_qname("rdf:RDF"))
    if rdf is None:
        # Declare the prefixes here so the block serializes as rdf:/cc:/dc: like Inkscape's,
        # not as auto-generated ns0: prefixes.
        rdf = inkex.etree.SubElement(
            metadata, _qname("rdf:RDF"), nsmap={p: str(inkex.NSS[p]) for p in ("rdf", "cc", "dc")}
        )
    work = _child(rdf, "cc:Work")
    if work.get(_qname("rdf:about")) is None:
        work.set(_qname("rdf:about"), "")
    return work


def set_document_metadata(
    doc: Document,
    *,
    title: str | None = None,
    creator: str | None = None,
    rights: str | None = None,
    date: str | None = None,
) -> dict[str, str]:
    """Set document-level RDF metadata fields (only the ones provided).

    Written directly as Inkscape lays it out (``<metadata>/rdf:RDF/cc:Work``): inkex 1.4.1's
    ``Metadata`` exposes these fields as read-only properties, so they can't be set through
    it. ``title`` is also written as the root ``<title>``, which is what browsers and screen
    readers read.
    """
    values = {"title": title, "creator": creator, "rights": rights, "date": date}
    applied = {name: value for name, value in values.items() if value is not None}
    if not applied:
        return applied
    work = _work(doc)
    for name, value in applied.items():
        node = work
        for step in _RDF_FIELDS[name]:
            node = _child(node, step)
        node.text = value
    if title is not None:
        _set_child_text(doc.svg, inkex.Title, title)
    return applied


def get_document_metadata(doc: Document) -> dict[str, str]:
    """The RDF metadata fields that are set, read straight from the XML (inkex's own getters
    test an lxml element's truthiness, which is False for a text-only element)."""
    metadata = doc.svg.find(_qname("svg:metadata"))
    if metadata is None:
        return {}
    work = metadata.find(f"{_qname('rdf:RDF')}/{_qname('cc:Work')}")
    if work is None:
        return {}
    found: dict[str, str] = {}
    for name, steps in _RDF_FIELDS.items():
        node = work.find("/".join(_qname(step) for step in steps))
        if node is not None and node.text is not None:
            found[name] = str(node.text)
    return found


def _content_bbox(doc: Document) -> inkex.transforms.BoundingBox | None:
    """Union world-bbox of the document's rendered content (skips defs/namedview/metadata)."""
    box: inkex.transforms.BoundingBox | None = None
    for child in doc.svg:
        if str(getattr(child, "TAG", "")).rsplit("}", 1)[-1] in _NON_CONTENT:
            continue
        try:
            current = child.bounding_box()
        except Exception:
            current = None
        if current is not None:
            box = current if box is None else box + current
    return box


def _num(value: float) -> str:
    return f"{value:.4f}".rstrip("0").rstrip(".") or "0"


def resize_document(
    doc: Document,
    *,
    width: float | None = None,
    height: float | None = None,
    mode: str = "plain",
    margin: float = 0.0,
) -> dict[str, str | None]:
    """Resize the document canvas. Three modes:

    - ``plain``: set ``width``/``height`` and a 1:1 ``viewBox`` (``0 0 width height``) — the canvas
      grows or crops around the content, which keeps its user-space coordinates. Needs width+height.
    - ``scale``: set ``width``/``height`` but keep the current ``viewBox`` — the content scales to
      fill the new canvas. Needs width+height.
    - ``fit``: set the ``viewBox`` (and size) to the content's bounding box plus ``margin`` —
      shrink-wraps/crops to the artwork. ``width``/``height`` optional (default = the fitted box;
      if given, the content scales to that size).

    Returns the new ``{width, height, viewBox}``.
    """
    svg = doc.svg
    if mode == "fit":
        box = _content_bbox(doc)
        if box is None:
            raise InvalidArgument("resize_document mode='fit' needs some rendered content")
        vx, vy = box.left - margin, box.top - margin
        vw, vh = box.width + 2 * margin, box.height + 2 * margin
        svg.set("viewBox", f"{_num(vx)} {_num(vy)} {_num(vw)} {_num(vh)}")
        svg.set("width", _num(width if width is not None else vw))
        svg.set("height", _num(height if height is not None else vh))
    elif mode == "scale":
        if width is None or height is None:
            raise InvalidArgument("resize_document mode='scale' needs both width and height")
        if not svg.get("viewBox"):  # establish the current frame so content scales predictably
            svg.set("viewBox", f"0 0 {svg.get('width') or width} {svg.get('height') or height}")
        svg.set("width", _num(width))
        svg.set("height", _num(height))
    elif mode == "plain":
        if width is None or height is None:
            raise InvalidArgument("resize_document mode='plain' needs both width and height")
        svg.set("width", _num(width))
        svg.set("height", _num(height))
        svg.set("viewBox", f"0 0 {_num(width)} {_num(height)}")
    else:
        raise InvalidArgument(f"unknown resize mode {mode!r}; choices: plain, scale, fit")
    return {"width": svg.get("width"), "height": svg.get("height"), "viewBox": svg.get("viewBox")}
