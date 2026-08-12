"""The Document facade: our stable wrapper around an inkex ``SvgDocumentElement``.

The facade owns the AI-facing semantics — id allocation and resolution of a target by id or
by friendly name — while delegating the actual DOM, transforms, styles, and bbox math to
inkex underneath. Keeping this boundary means the tool contract stays stable even as inkex
changes internally.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

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


@dataclass(slots=True)
class ThemeMeta:
    """What one materialized theme contributed to a document (its CSS lives in ``theme_css``).

    ``source`` and ``search_paths`` are exactly what the load used, so re-reading the same theme
    from disk needs no extra argument. ``routes`` is the set of route keys (categories and roles)
    this theme currently serves; ``class_names`` is every class its materialized CSS defines, which
    is what decides whether an auto-applied hook has a rule behind it. ``kinds`` is the manifest's
    diagram kind → shape primitive mapping, kept here so a facade can ask the document which shape
    a kind wants without re-reading the theme from disk.
    """

    variant: str | None = None
    tokens: dict[str, str] = field(default_factory=dict)
    descriptions: dict[str, str] = field(default_factory=dict)
    source: Path = Path()
    search_paths: list[Path] = field(default_factory=list)
    routes: list[str] = field(default_factory=list)
    class_names: frozenset[str] = frozenset()
    kinds: dict[str, str] = field(default_factory=dict)


class Document:
    """One SVG document, mutated in place across tool calls."""

    _STYLESHEET_ID = "svgmcp-styles"

    def __init__(self, svg: SvgDocumentElement) -> None:
        self.svg = svg
        # Named styles the AI defines, mirrored into a single <style> sheet in <defs>.
        self.styles: dict[str, dict[str, str]] = {}
        # CSS captured from an imported document's own svg-mcp <style> sheet. The registries
        # (theme_css, styles) start empty on import, so without this the first _sync_stylesheet
        # would rewrite the sheet from nothing and silently unstyle every classed node.
        # It is emitted FIRST, before the theme blocks and named styles, so re-loading the same
        # theme (or redefining a style) materializes AFTER it and wins the equal-specificity tie.
        #
        # It is a PRESERVATION SHIM, not a second stylesheet: it holds rules only until a
        # registry can speak for them again, and is INVALIDATED as that happens. Loading a theme
        # drops the rules its own block now covers (`supersede_imported_theme`) and defining a
        # named style drops the imported `.name` rule it replaces — so unloading a theme really
        # unstyles, a variant switch really switches, and repeated export/import cycles cannot
        # stack copy after copy of the same block. Rules naming classes nothing here manages are
        # never touched: genuinely foreign CSS is preserved for the life of the document.
        self.imported_css: str = ""
        # Materialized theme stylesheets, theme name -> CSS text, in the order they were applied.
        # They are emitted BEFORE doc.styles so a named style beats a theme hook on a tie.
        self.theme_css: dict[str, str] = {}
        self.theme_meta: dict[str, ThemeMeta] = {}
        # Route key (a category or a role name) -> the resident theme serving it. Themes are a
        # role-routed SET, so a key is served by exactly one theme at a time.
        self.theme_routing: dict[str, str] = {}
        # friendly-name -> set of node ids, built lazily from the tree then kept current by the
        # naming ops, so a duplicate-name check is an O(1) dict hit instead of a full-tree scan.
        self._names: dict[str, set[str]] | None = None

    def _name_index(self) -> dict[str, set[str]]:
        if self._names is None:
            index: dict[str, set[str]] = {}
            for node in self.svg.iter():
                label = getattr(node, "label", None)
                if label:
                    index.setdefault(str(label), set()).add(str(node.get_id()))
            self._names = index
        return self._names

    def register_name(self, name: str, node_id: str) -> None:
        """Record that ``node_id`` now carries friendly name ``name`` (keeps the index current)."""
        self._name_index().setdefault(name, set()).add(str(node_id))

    def name_warning(self, name: str, exclude_id: str | None = None) -> str | None:
        """A non-fatal advisory if ``name`` collides with another node's name (or id); else None.

        Consults the maintained name index (no full scan) and verifies each candidate against the
        live tree — stale entries (deleted/renamed elsewhere) are pruned, so they can't cause a
        false positive.
        """
        conflicts: list[str] = []
        candidates = self._name_index().get(name)
        if candidates:
            stale = {
                nid
                for nid in candidates
                if nid != exclude_id
                and str(getattr(self.svg.getElementById(nid), "label", "") or "") != name
            }
            candidates -= stale
            conflicts.extend(nid for nid in candidates if nid != exclude_id)
        # A name equal to an existing node's *id* is also ambiguous (resolve matches id first).
        id_match = self.svg.getElementById(name)
        if id_match is not None and str(id_match.get_id()) != exclude_id:
            conflicts.append(name)
        if not conflicts:
            return None
        others = ", ".join(sorted(set(conflicts))[:4])
        return (
            f"name {name!r} is already used by {others}; @name and name-based lookups are now "
            "ambiguous (a renderable node is preferred). Reference this node by id, or rename it."
        )

    def add_def(self, element: BaseElement, prefix: str, name: str | None = None) -> str:
        """Add a reusable resource to ``<defs>``, assign it an id, and return that id."""
        self.svg.defs.add(element)
        element.set_id(self.new_id(prefix))
        if name is not None:
            element.label = name
            self.register_name(name, str(element.get_id()))
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
        """Load an existing SVG document from a string, keeping any svg-mcp sheet it carries."""
        tree = inkex.load_svg(svg_text)
        document = cls(tree.getroot())
        existing = document.svg.getElementById(cls._STYLESHEET_ID)
        if existing is not None:
            document.imported_css = str(existing.text or "").strip()
        return document

    def new_id(self, prefix: str) -> str:
        """Allocate a fresh, document-unique id with the given prefix."""
        return str(self.svg.get_unique_id(prefix))

    def resolve(self, target: str) -> BaseElement:
        """Resolve a target to a single node — an IDENTITY by default, a query only if asked.

        A handle carries three namespaces, and which one is meant is declared, never guessed:

        - ``id:x`` — that exact id, and nothing else.
        - ``name:x`` — that exact friendly name (``inkscape:label``), verbatim: slashes,
          colons and all.
        - ``path:a/b/c`` — the hierarchy QUERY: the node matching ``c`` whose ancestors match
          ``b`` then ``a`` in order (gaps allowed). A disambiguator of last resort.
        - anything else — a bare handle: the id if one matches, else the exact name. **Never a
          path.** A name may contain ``/`` (a graph import names its nodes after ids like
          ``src/ops/diagram.py``), and a name that looks like a query is still a name.

        The prefixes are stripped ONCE, which is what makes this a grammar rather than a nicer
        heuristic: every possible label has an unambiguous spelling. A node genuinely called
        ``name:foo`` is ``name:name:foo``; one whose name collides with another node's id is
        ``name:…`` while the other is ``id:…``.

        A name matching several nodes is rejected with :class:`AmbiguousReference` rather than
        silently picking one — except when exactly one match is *renderable* (not under
        ``<defs>``); that one is preferred, so a shape sharing a label with a gradient or clip
        definition still resolves.
        """
        for prefix, resolver in (
            ("id:", self._resolve_id),
            ("name:", self._resolve_name),
            ("path:", self._resolve_path),
        ):
            if target.startswith(prefix):
                return resolver(target[len(prefix) :])
        element = self.svg.getElementById(target)
        if element is not None:
            return element
        return self._resolve_name(target, bare=True)

    def _resolve_id(self, node_id: str) -> BaseElement:
        element = self.svg.getElementById(node_id)
        if element is None:
            raise NodeNotFound(f"no node with id {node_id!r}")
        return element

    def _resolve_name(self, name: str, *, bare: bool = False) -> BaseElement:
        """The node whose friendly name is exactly ``name`` — no path interpretation, ever."""
        matches = [n for n in self.svg.descendants() if getattr(n, "label", None) == name]
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise NodeNotFound(self._nothing_named(name, bare=bare))
        renderable = [n for n in matches if not self._in_defs(n)]
        if len(renderable) == 1:
            return renderable[0]
        hints = "; ".join(self._qualify(m, name) for m in matches)
        raise AmbiguousReference(
            f"name {name!r} matches {len(matches)} nodes — qualify by hierarchy "
            f"(e.g. {self._qualify(matches[0], name)}) or use the node id. Candidates: {hints}"
        )

    @staticmethod
    def _nothing_named(name: str, *, bare: bool) -> str:
        """Say what was looked for — and teach the path form to whoever was relying on it."""
        message = f"no node with {'id or name' if bare else 'name'} {name!r}"
        if bare and "/" in name:
            message += (
                " — a bare handle is an id or an EXACT name, never a hierarchy path; write "
                f"path:{name} to search the ancestor chain instead"
            )
        return message

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
        """A disambiguating reference for ``node``: ``path:parent/name`` (parent by name or id).

        Spelled with its prefix because it is offered as something to PASS BACK, and a bare
        ``parent/name`` no longer resolves as a query.
        """
        parent = node.getparent()
        if parent is None or parent is self.svg:
            return f"{name} ({node.get_id()})"
        tag = getattr(parent, "label", None) or str(parent.get_id())
        return f"path:{tag}/{name} ({node.get_id()})"

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
