"""The Document facade: our stable wrapper around an inkex ``SvgDocumentElement``.

The facade owns the AI-facing semantics — id allocation and resolution of a target by id or
by friendly name — while delegating the actual DOM, transforms, styles, and bbox math to
inkex underneath. Keeping this boundary means the tool contract stays stable even as inkex
changes internally.
"""

from __future__ import annotations

import inkex
from inkex import BaseElement, SvgDocumentElement

from .errors import AmbiguousReference, NodeNotFound

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
        """Resolve a target to a single node by id or friendly name (``inkscape:label``).

        A bare name that matches several nodes is rejected with :class:`AmbiguousReference`
        (rather than silently picking one) — except when exactly one match is a *renderable* node
        (not under ``<defs>``); that one is preferred, so a shape sharing a label with a gradient
        or clip definition still resolves. Otherwise disambiguate with a hierarchy path
        ``ancestor/.../name`` — each segment is an id or name, matched down the ancestor chain —
        or by the node's unique id.
        """
        if "/" in target:
            return self._resolve_path(target)
        element = self.svg.getElementById(target)
        if element is not None:
            return element
        matches = [n for n in self.svg.descendants() if getattr(n, "label", None) == target]
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise NodeNotFound(f"no node with id or name {target!r}")
        renderable = [n for n in matches if not self._in_defs(n)]
        if len(renderable) == 1:
            return renderable[0]
        hints = "; ".join(self._qualify(m, target) for m in matches)
        raise AmbiguousReference(
            f"name {target!r} matches {len(matches)} nodes — qualify by hierarchy "
            f"(e.g. {self._qualify(matches[0], target)}) or use the node id. Candidates: {hints}"
        )

    @staticmethod
    def _in_defs(node: BaseElement) -> bool:
        """True if ``node`` is inside a ``<defs>`` (a non-rendered definition like a gradient)."""
        ancestor = node.getparent()
        while ancestor is not None:
            if str(getattr(ancestor, "TAG", "")) == "defs":
                return True
            ancestor = ancestor.getparent()
        return False

    @staticmethod
    def _matches(node: BaseElement, token: str) -> bool:
        return str(node.get_id()) == token or getattr(node, "label", None) == token

    def _qualify(self, node: BaseElement, name: str) -> str:
        """A disambiguating reference for ``node``: ``parent/name`` (parent by name or id)."""
        parent = node.getparent()
        if parent is None or parent is self.svg:
            return f"{name} ({node.get_id()})"
        tag = getattr(parent, "label", None) or str(parent.get_id())
        return f"{tag}/{name} ({node.get_id()})"

    def _resolve_path(self, target: str) -> BaseElement:
        """Resolve an ``a/b/c`` path: find ``c`` whose ancestors match ``b`` then ``a`` in order."""
        parts = [p for p in target.split("/") if p]
        if not parts:
            raise NodeNotFound(f"empty reference {target!r}")
        leaf = parts[-1]
        candidates = [n for n in self.svg.descendants() if self._matches(n, leaf)]

        def ancestry_matches(node: BaseElement) -> bool:
            needed = list(reversed(parts[:-1]))  # nearest ancestor first
            ancestor = node.getparent()
            while ancestor is not None and needed:
                if self._matches(ancestor, needed[0]):
                    needed.pop(0)
                ancestor = ancestor.getparent()
            return not needed

        filtered = [n for n in candidates if ancestry_matches(n)]
        if len(filtered) == 1:
            return filtered[0]
        if not filtered:
            raise NodeNotFound(f"no node matched the hierarchy path {target!r}")
        ids = ", ".join(str(n.get_id()) for n in filtered)
        raise AmbiguousReference(
            f"path {target!r} still matches {len(filtered)} nodes ({ids}); add more ancestors "
            "or use the node id"
        )

    def resolve_parent(self, parent: str | None) -> BaseElement:
        """Resolve a parent target, defaulting to the document root."""
        if parent is None:
            return self.svg
        return self.resolve(parent)

    def to_svg(self) -> str:
        """Serialize the document to an SVG string."""
        return str(self.svg.tostring().decode("utf-8"))
