"""Bulk graph ingestion: a producer's ``{nodes, edges}`` export becomes a laid-out diagram.

Everything here is mechanical translation, and that is the point. A call graph, an import graph,
a dependency tree — anything that already knows what its nodes and edges ARE — arrives as one
JSON object, and turning it into a picture should not cost one hand-written ``add_diagram_node``
per box. The wire shape below MIRRORS what such producers actually emit (``from``/``to``, extra
descriptive keys per object), so an export pastes in verbatim rather than being transcribed.

Two decisions run through the whole module:

**The models are tolerant, the graph is strict.** :class:`GraphNode` and :class:`GraphEdge` are
``extra="ignore"`` — alone among this codebase's schemas, which forbid unknown keys — because the
producer owns its own format: a richer export that also carries ``file``, ``symbols`` or ``loc``
must not be rejected for being richer than we asked. The GRAPH itself is not tolerant at all: an
edge naming a node that was never declared is a hole in the data, and inventing the missing box
would draw a picture the export does not describe.

**Weight is DATA, not style.** It filters (``min_weight``) and it can be written on the edge
(``weight_labels``), and that is all. Baking a weight into a stroke width would fight the theme
for control of the line, and the theme wins that fight everywhere else in this codebase.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from fnmatch import fnmatch
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from ..model.document import Document
from ..model.errors import InvalidArgument
from .diagram import _shape_for, add_diagram_edge, add_diagram_node
from .diagram_layout import DiagramLayout, Direction, layout_diagram
from .themes import resolve_dressing, serving_theme

LabelMode = Literal["id", "basename", "trimmed"]
"""How a node's box is captioned when the export gave no explicit label."""

GraphLayout = Literal["layered", "tree", "grid", "none"]
"""Which layout to finish with — ``none`` leaves the nodes in the stack ingestion built."""


# --- the producer's wire shape -----------------------------------------------


class GraphNode(BaseModel):
    """One node of an incoming graph: an identity, and optionally what to call it and what it is.

    ``extra="ignore"`` deliberately: a code-graph export carries whatever its producer found
    interesting per node (``file``, ``symbols``, ``loc``, …), and none of that is our business.
    """

    model_config = ConfigDict(extra="ignore")

    id: str
    """The producer's identity for this node — the key its edges name, and the diagram node's
    friendly name once ingested."""
    label: str | None = None
    """What the box says. Omit to derive it from the id via ``label_mode``."""
    kind: str | None = None
    """A DIAGRAM kind ("service", "datastore", …) — not a producer taxonomy. None takes
    ``default_node_kind``."""


class GraphEdge(BaseModel):
    """One edge of an incoming graph, accepted under the producer's spelling or ours.

    ``from``/``to`` are what exports write and what Python cannot name, so the fields are aliased:
    both ``{"from": …, "to": …}`` and ``{"source": …, "target": …}`` parse to the same edge.
    """

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    source: str = Field(alias="from")
    """The node id the edge leaves. Accepts ``from`` (the producer's spelling) or ``source``."""
    target: str = Field(alias="to")
    """The node id the edge arrives at. Accepts ``to`` or ``target``."""
    kind: str | None = None
    """A DIAGRAM edge kind ("data", "control", "dependency", …). None takes
    ``default_edge_kind``; a producer's own ``kind`` ("calls", "imports") is only meaningful here
    if a resident theme happens to serve a role by that name."""
    label: str | None = None
    """Text on the line. Wins over ``weight_labels``."""
    weight: float | None = None
    """How strong the relation is. Filters and labels only — never restyles the line."""


class GraphImport(BaseModel):
    """What one ingestion built, and what it declined to build.

    Every drop is counted SEPARATELY, because "37 edges went in and 24 came out" is only useful
    if the caller can tell a weight filter from a self-loop from a node it excluded itself.
    """

    nodes_created: int = 0
    edges_created: int = 0
    self_edges_dropped: int = 0
    """Edges whose two ends were the same node: a box-and-arrow diagram has nowhere to draw one."""
    edges_dropped_filtered: int = 0
    """Edges left dangling by ``include``/``exclude`` taking one of their endpoints out."""
    edges_dropped_weight: int = 0
    """Edges below ``min_weight``."""
    nodes_filtered: int = 0
    """Nodes ``include``/``exclude`` kept out of the drawing."""
    mapping: dict[str, str] = Field(default_factory=dict)
    """Graph id → the created node's SVG id. The handle to use for everything afterwards."""
    kinds_defaulted: list[str] = Field(default_factory=list)
    """Kinds the export named that no resident theme dresses, drawn as the default kind instead.

    A producer's taxonomy ("calls", "imports") is not a design language, and the whole promise of
    this op is that an export pastes in — so an unservable kind is REPORTED, not raised on. Every
    substitution is listed here, so a real typo ("servcie") is visible rather than silent.
    """
    ranks: int | None = None
    """Layout results, absent when ``layout="none"``."""
    cycles_broken: int | None = None
    edges_rerouted: int | None = None


# --- label derivation --------------------------------------------------------


# How a graph producer spells a hierarchy, and it is NOT one convention: a file path uses "/"
# (or "\\"), and a fully-qualified name uses whatever its language uses — "." for Python/Java/C#,
# "::" for Rust/C++/Ruby, "\\" for PHP namespaces. Ids also arrive MIXED, e.g. a C++ symbol as
# ``src/render/canvas.cpp::Canvas::draw``, so the separator is found per id rather than declared.
#
# Getting this wrong is not cosmetic. Read ``svg_mcp.ops.graph.add_diagram_graph`` as a path and
# its "extension" is ``.add_diagram_graph`` — every symbol in a module then ends up captioned
# with the module's name, silently, on every box.
# "/" is a path separator everywhere. "\\" is BOTH a Windows path separator and PHP's namespace
# separator — we cannot tell which from the character, and happily do not need to: it is a
# hierarchy step under either reading.
_PATH_SEPARATORS = ("/", "\\")
_NAME_SEPARATORS = ("::", ".")

# A CLOSED list, for the same reason the chart formatters are closed: the alternative is a
# heuristic, and every heuristic here is wrong about some language. "Short and alphabetic" would
# read Go's ``net/http.Client`` as the file ``net/http`` with extension ``.Client`` and caption
# that box ``http``. An extension nobody listed just stays in the label — longer, never wrong.
_EXTENSIONS = frozenset({
    "py", "pyi", "pyx", "js", "mjs", "cjs", "jsx", "ts", "mts", "cts", "tsx", "vue", "svelte",
    "go", "rs", "rb", "java", "kt", "kts", "scala", "clj", "cljs", "swift", "m", "mm", "cs",
    "fs", "php", "c", "h", "cc", "cpp", "cxx", "hh", "hpp", "hxx", "sh", "bash", "zsh", "fish",
    "lua", "pl", "pm", "r", "jl", "ex", "exs", "erl", "dart", "groovy", "sql", "md", "rst",
    "json", "yaml", "yml", "toml", "ini", "cfg", "xml", "html", "css", "scss", "sass", "less",
})  # fmt: skip


def _names_a_file(text: str) -> bool:
    return any(separator in text for separator in _PATH_SEPARATORS)


def _separators(file_shaped: bool) -> tuple[str, ...]:
    """Which strings count as a hierarchy step.

    A dot is the one ambiguous character: a step in ``pkg.module.Class``, an EXTENSION in
    ``diagram.py``. So it counts as a separator only where no filename is in play — otherwise
    ``basename`` of ``ops/diagram.py`` is ``py``, which is not a name for anything.
    """
    return (*_PATH_SEPARATORS, "::") if file_shaped else (*_PATH_SEPARATORS, *_NAME_SEPARATORS)


def _last_boundary(text: str, separators: Sequence[str]) -> int:
    """The index just past ``text``'s last separator (0 if it has none).

    Whichever separator ends LATEST wins, so a mixed ``src/render/canvas.cpp::Canvas::draw`` cuts
    at the ``::`` — the symbol — rather than at the ``/`` that only gets you to the file.
    """
    return max((text.rfind(sep) + len(sep) for sep in separators if sep in text), default=0)


def _shared_prefix(ids: Sequence[str]) -> str:
    """The longest prefix every id shares, cut back to a separator boundary (``""`` if none).

    Cut back, because a character-wise prefix is not a hierarchy: ``ops/diagram.py`` and
    ``ops/diagrams.py`` share ``ops/diagram``, and trimming that would leave one node captioned
    ``s``. Only whole segments are ever removed, so what is left still reads as a path — or as a
    dotted, ``::``-ed or namespaced name, whichever went in.

    One file path anywhere in the set settles the dot for the WHOLE set: a mixed export is read
    the conservative way, since cutting a shared prefix mid-filename is the worse mistake.
    """
    if not ids:
        return ""
    separators = _separators(any(_names_a_file(node_id) for node_id in ids))
    prefix = ids[0]
    for other in ids[1:]:
        limit = min(len(prefix), len(other))
        cut = limit
        for index in range(limit):
            if prefix[index] != other[index]:
                cut = index
                break
        prefix = prefix[:cut]
        if not prefix:
            return ""
    return prefix[: _last_boundary(prefix, separators)]


def _drop_extension(text: str) -> str:
    """``diagram.py`` → ``diagram``, for a dot-tail this module actually recognises as one.

    Everything else that can follow a dot — a Go symbol on a package path, a Python attribute, a
    ``::`` chain hanging off a C++ filename — is the thing the node IS, and must survive intact.
    """
    head, dot, tail = text.rpartition(".")
    return head if dot and head and tail.lower() in _EXTENSIONS else text


def _derive_label(node: GraphNode, mode: LabelMode, prefix: str) -> str:
    """What the box says: the export's own label if it gave one, else the id made readable."""
    if node.label is not None:
        return node.label
    if mode == "id":
        return node.id
    # An extension belongs to a filename, so it is only shaved off an id that names a FILE — and
    # that is judged on the ORIGINAL id, not on what trimming left of it, since `diagram.py` is
    # still a filename once its directories have been cut away.
    file_shaped = _names_a_file(node.id)
    if mode == "basename":
        tail = node.id[_last_boundary(node.id, _separators(file_shaped)) :] or node.id
        return _drop_extension(tail) if file_shaped else tail
    trimmed = node.id.removeprefix(prefix) or node.id
    return _drop_extension(trimmed) if file_shaped else trimmed


def _format_weight(weight: float) -> str:
    """A weight as a label: an integral one reads as an integer, since most of them are counts."""
    return str(int(weight)) if float(weight).is_integer() else f"{weight:g}"


# --- filtering and merging ---------------------------------------------------


def _selected(node_id: str, include: Sequence[str] | None, exclude: Sequence[str] | None) -> bool:
    """Whether a node survives the glob filters. ``exclude`` beats ``include``, always."""
    if include is not None and not any(fnmatch(node_id, pattern) for pattern in include):
        return False
    return not (exclude is not None and any(fnmatch(node_id, pattern) for pattern in exclude))


@dataclass(slots=True)
class _Merged:
    """One edge as it will be drawn, after every parallel copy of it has been folded in."""

    source: str
    target: str
    kind: str
    label: str | None
    weight: float | None


def _merge_weights(first: float | None, second: float | None) -> float | None:
    """Sum two weights, treating "no weight" as absent rather than as zero."""
    if first is None:
        return second
    return first if second is None else first + second


# --- atomicity ---------------------------------------------------------------


@contextmanager
def _rollback(doc: Document, created: list[str]) -> Iterator[None]:
    """Build a whole graph, removing everything it created again if any part of it fails.

    The batch equivalent of ``_facade_body``: a half-ingested graph — some boxes drawn, the edges
    that would have explained them missing — is worse than none at all, and nothing outside this
    call can be holding a handle on any of it yet. Removal is in reverse order so an edge goes
    before the nodes it connects.

    What it does NOT undo is document furniture a facade may have installed on the way (the
    theme's stylesheet block, the one shared arrowhead marker). Those are idempotent, shared by
    every facade in the document, and re-created identically by the next successful call — which
    is why the failures this module can actually anticipate are all raised BEFORE the first node
    is drawn, where "unchanged" needs no argument at all.
    """
    try:
        yield
    except Exception:
        for node_id in reversed(created):
            element = doc.svg.getElementById(node_id)
            if element is not None:
                element.delete()
        raise


def _resolve_kinds(
    doc: Document,
    *,
    category: Literal["shape", "connector"],
    kinds: Sequence[str],
    default: str,
    themed: bool,
) -> tuple[dict[str, str], list[str]]:
    """Map every named kind to one the theme can dress, falling back to ``default`` where it can't.

    Two jobs at once, both about failing EARLY. ``resolve_dressing`` is a pure question — it
    installs nothing and touches no element — so asking it here, before a single box is drawn,
    turns the commonest failure of a bulk ingest (an unservable kind named on the fortieth edge)
    into an answer rather than a rollback.

    The answer is a substitution, not a refusal, because a producer's taxonomy is not a design
    language: an import graph legitimately calls its edges "calls" and "imports", and refusing the
    whole export over vocabulary would defeat the point of pasting one in. The CALLER's own
    ``default_node_kind``/``default_edge_kind`` is different — that one is a choice, not data, so
    an unservable default still raises.
    """

    def dress(kind: str) -> None:
        prim = "edge" if category == "connector" else _shape_for(serving_theme(doc, kind), kind)
        resolve_dressing(doc, category=category, prim=prim, role=kind, themed=themed)

    dress(default)
    mapped: dict[str, str] = {}
    defaulted: list[str] = []
    for kind in dict.fromkeys(kinds):
        try:
            dress(kind)
        except InvalidArgument:
            mapped[kind] = default
            defaulted.append(kind)
        else:
            mapped[kind] = kind
    return mapped, defaulted


# --- the ingest --------------------------------------------------------------


def add_diagram_graph(
    doc: Document,
    *,
    nodes: Sequence[GraphNode],
    edges: Sequence[GraphEdge],
    default_node_kind: str = "service",
    default_edge_kind: str = "data",
    label_mode: LabelMode = "trimmed",
    min_weight: float | None = None,
    include: Sequence[str] | None = None,
    exclude: Sequence[str] | None = None,
    weight_labels: bool = False,
    layout: GraphLayout = "layered",
    direction: Direction = "LR",
    spacing_main: float | None = None,
    spacing_cross: float | None = None,
    parent: str | None = None,
    themed: bool = True,
) -> GraphImport:
    """Ingest a whole ``{nodes, edges}`` graph as one diagram: boxes, arrows, and a layout.

    The same picture ``add_diagram_node`` × N + ``add_diagram_edge`` × M + ``layout_diagram``
    would draw, in one call and with the bookkeeping done: nodes filtered by glob, dangling and
    self edges dropped and COUNTED, parallel edges merged with their weights summed, and every
    created node named after its graph id so the export's own vocabulary keeps working afterwards.

    The order of operations is fixed, because each step decides what the next one sees:
    filter nodes → drop edges the filter orphaned → drop self edges → drop light edges → reject
    unknown endpoints → resolve kinds → merge parallels → draw → lay out.

    Kinds are resolved BEFORE the merge so that two parallel edges whose different producer kinds
    both fall back to the default collapse into ONE arrow rather than two identical ones.

    Unknown endpoints are the one thing here that refuses rather than counts. A node the filter
    removed is a decision the caller made, so its edges are dropped quietly; a node NOBODY
    declared is a hole in the export, and auto-creating a box for it would draw a graph the
    producer never described.
    """
    # Every id the export DECLARED. The filter narrows what gets DRAWN, not what counts as
    # declared — that difference is the whole of how an orphaned edge is told from a dangling one.
    seen: set[str] = set()
    for node in nodes:
        if node.id in seen:
            raise InvalidArgument(f"node id {node.id!r} appears twice in `nodes`; ids are identity")
        seen.add(node.id)

    kept = [node for node in nodes if _selected(node.id, include, exclude)]
    kept_ids = {node.id for node in kept}

    dropped_filtered = dropped_self = dropped_weight = 0
    surviving: list[GraphEdge] = []
    for edge in edges:
        endpoints = ((edge.source, "source"), (edge.target, "target"))
        if any(end in seen and end not in kept_ids for end, _role in endpoints):
            dropped_filtered += 1
            continue
        if edge.source == edge.target:
            dropped_self += 1
            continue
        if min_weight is not None and edge.weight is not None and edge.weight < min_weight:
            dropped_weight += 1
            continue
        for end, role in endpoints:
            if end not in seen:
                raise InvalidArgument(
                    f"edge {edge.source!r} -> {edge.target!r} names {end!r} as its {role}, but no "
                    "node declares that id; add it to `nodes` (nodes are never auto-created)"
                )
        surviving.append(edge)

    for node in kept:
        warning = doc.name_warning(node.id)
        if warning is not None:
            raise InvalidArgument(
                f"node id {node.id!r} collides with something already in the document: {warning}"
            )
    node_kinds, node_defaulted = _resolve_kinds(
        doc,
        category="shape",
        kinds=[node.kind for node in kept if node.kind is not None],
        default=default_node_kind,
        themed=themed,
    )
    edge_kinds, edge_defaulted = _resolve_kinds(
        doc,
        category="connector",
        kinds=[edge.kind for edge in surviving if edge.kind is not None],
        default=default_edge_kind,
        themed=themed,
    )

    merged: dict[tuple[str, str, str], _Merged] = {}
    for edge in surviving:
        kind = edge_kinds[edge.kind] if edge.kind is not None else default_edge_kind
        key = (edge.source, edge.target, kind)
        existing = merged.get(key)
        if existing is None:
            merged[key] = _Merged(edge.source, edge.target, kind, edge.label, edge.weight)
        else:
            existing.weight = _merge_weights(existing.weight, edge.weight)
            if existing.label is None:
                existing.label = edge.label

    prefix = _shared_prefix([node.id for node in kept])
    created: list[str] = []
    mapping: dict[str, str] = {}
    laid: DiagramLayout | None = None
    with _rollback(doc, created):
        for node in kept:
            placed = add_diagram_node(
                doc,
                kind=node_kinds[node.kind] if node.kind is not None else default_node_kind,
                label=_derive_label(node, label_mode, prefix),
                parent=parent,
                name=node.id,
                themed=themed,
            )
            created.append(placed.ref.id)
            mapping[node.id] = placed.ref.id
        for entry in merged.values():
            label = entry.label
            if label is None and weight_labels and entry.weight is not None:
                label = _format_weight(entry.weight)
            placed_edge = add_diagram_edge(
                doc,
                source=mapping[entry.source],
                target=mapping[entry.target],
                kind=entry.kind,
                label=label,
                parent=parent,
                themed=themed,
            )
            created.append(placed_edge.ref.id)
        if layout != "none" and kept:
            laid = layout_diagram(
                doc,
                algorithm=layout,
                direction=direction,
                scope=parent,
                spacing_main=spacing_main,
                spacing_cross=spacing_cross,
            )
    return GraphImport(
        nodes_created=len(kept),
        edges_created=len(merged),
        self_edges_dropped=dropped_self,
        edges_dropped_filtered=dropped_filtered,
        edges_dropped_weight=dropped_weight,
        nodes_filtered=len(nodes) - len(kept),
        mapping=mapping,
        kinds_defaulted=sorted({*node_defaulted, *edge_defaulted}),
        ranks=None if laid is None else laid.ranks,
        cycles_broken=None if laid is None else laid.cycles_broken,
        edges_rerouted=None if laid is None else laid.edges_rerouted,
    )
