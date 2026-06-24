"""The Document facade: our stable wrapper around an inkex ``SvgDocumentElement``.

The facade owns the AI-facing semantics — id allocation and resolution of a target by id or
by friendly name — while delegating the actual DOM, transforms, styles, and bbox math to
inkex underneath. Keeping this boundary means the tool contract stays stable even as inkex
changes internally.
"""

from __future__ import annotations

import inkex
from inkex import BaseElement, SvgDocumentElement

from .errors import NodeNotFound

# Namespaces declared up front so inkscape:/sodipodi:/xlink: attributes serialize cleanly.
_SVG_TEMPLATE = (
    '<svg xmlns="http://www.w3.org/2000/svg" '
    'xmlns:inkscape="http://www.inkscape.org/namespaces/inkscape" '
    'xmlns:sodipodi="http://sodipodi.sourceforge.net/DTD/sodipodi-0.0.dtd" '
    'xmlns:xlink="http://www.w3.org/1999/xlink" '
    'width="{w}" height="{h}" viewBox="{vb}"></svg>'
)


class Document:
    """One SVG document, mutated in place across tool calls."""

    _STYLESHEET_ID = "svgmcp-styles"

    def __init__(self, svg: SvgDocumentElement) -> None:
        self.svg = svg
        # Named styles the AI defines, mirrored into a single <style> sheet in <defs>.
        self.styles: dict[str, dict[str, str]] = {}

    def add_def(self, element: BaseElement, prefix: str, name: str | None = None) -> str:
        """Add a reusable resource to ``<defs>``, assign it an id, and return that id."""
        self.svg.defs.add(element)
        element.set_id(self.new_id(prefix))
        if name is not None:
            element.label = name
        return str(element.get_id())

    def stylesheet(self) -> BaseElement:
        """Get (or create) the single ``<style>`` element backing named styles."""
        existing = self.svg.getElementById(self._STYLESHEET_ID)
        if existing is not None:
            return existing
        sheet = inkex.StyleElement()
        self.svg.defs.add(sheet)
        sheet.set_id(self._STYLESHEET_ID)
        return sheet

    @classmethod
    def create(cls, width: float, height: float, viewbox: str | None = None) -> Document:
        """Create a blank document of the given size (user units)."""
        vb = viewbox if viewbox is not None else f"0 0 {width} {height}"
        tree = inkex.load_svg(_SVG_TEMPLATE.format(w=width, h=height, vb=vb))
        return cls(tree.getroot())

    @classmethod
    def from_svg(cls, svg_text: str) -> Document:
        """Load an existing SVG document from a string."""
        tree = inkex.load_svg(svg_text)
        return cls(tree.getroot())

    def new_id(self, prefix: str) -> str:
        """Allocate a fresh, document-unique id with the given prefix."""
        return str(self.svg.get_unique_id(prefix))

    def resolve(self, target: str) -> BaseElement:
        """Resolve a target by SVG id, falling back to friendly name (``inkscape:label``)."""
        element = self.svg.getElementById(target)
        if element is not None:
            return element
        for node in self.svg.descendants():
            if getattr(node, "label", None) == target:
                return node
        raise NodeNotFound(f"no node with id or name {target!r}")

    def resolve_parent(self, parent: str | None) -> BaseElement:
        """Resolve a parent target, defaulting to the document root."""
        if parent is None:
            return self.svg
        return self.resolve(parent)

    def to_svg(self) -> str:
        """Serialize the document to an SVG string."""
        return str(self.svg.tostring().decode("utf-8"))
