"""Bulk graph ingestion: a producer's ``{nodes, edges}`` export becomes a laid-out diagram.

Everything here is mechanical translation, and that is the point. A service map, a dependency
tree, a state machine, an org chart, a call graph — anything that already knows what its nodes
and edges ARE — arrives as one JSON object, and turning it into a picture should not cost one
hand-written ``add_diagram_node`` per box. The wire shape below MIRRORS what such producers
actually emit (``from``/``to``, extra descriptive keys per object), so an export pastes in
verbatim rather than being transcribed.

Three decisions run through the whole module:

**The models are tolerant, the graph is strict.** :class:`GraphNode` keeps unknown keys and
:class:`GraphEdge` ignores them — alone among this codebase's schemas, which forbid them —
because the producer owns its own format: a richer export carrying ``file``, ``symbols`` or
``loc`` must not be rejected for being richer than we asked, and ``size_field`` can then name
one of those keys without this module having to know the word. The GRAPH itself is not tolerant
at all: an edge naming a node that was never declared is a hole in the data, and inventing the
missing box would draw a picture the export does not describe.

**Data never restyles.** An edge's ``weight`` and a node's ``size`` can be written into a label,
and a size can — only when asked — scale a box, which is geometry the facade already owns.
Neither ever touches paint: stroke and fill belong to the theme, and the theme wins that fight
everywhere else in this codebase.

**Nothing here decides what matters.** No ranking, no threshold, no importance score. Which
nodes deserve a box, and which several are really one thing, is a semantic judgement about what
the picture is FOR; the caller states it (``exclude``, ``collapse``) and this does the mechanical
part in full and reports exactly what it did. Every automatic alternative was tried against a
real codebase and every one of them nominated the exception module as the architecture.
"""

from __future__ import annotations

import math
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from fnmatch import fnmatch
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from ..model.document import Document
from ..model.errors import InvalidArgument
from ..query.outline import _bbox_xywh
from .diagram import _shape_for, add_diagram_edge, add_diagram_node, auto_size
from .diagram_layout import DiagramLayout, Direction, layout_diagram
from .themes import resolve_dressing, serving_theme

LabelMode = Literal["id", "basename", "trimmed"]
"""How a node's box is captioned when the export gave no explicit label."""

GraphLayout = Literal["layered", "tree", "grid", "none"]
"""Which layout to finish with — ``none`` leaves the nodes in the stack ingestion built."""


# --- the producer's wire shape -----------------------------------------------


def _numeric(value: JsonValue) -> float | None:
    """The magnitude ``value`` carries, or None if it carries none.

    A bool is an ``int`` in Python and "True symbols" is not a quantity. A numeric STRING is one:
    a producer that spells its numbers as text ("12") still means twelve, and a JSON export that
    quoted its counts is not a different graph.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            number = float(value)
        except ValueError:
            return None
        return number if math.isfinite(number) else None
    return None


_CARRIED_SIZE = "\x00size"
"""Where an unreadable ``size`` waits between :class:`GraphNode`'s two validators.

A NUL is not a key any producer can send, so parking the value here cannot collide with one of
the extras the export genuinely carries.
"""


class GraphNode(BaseModel):
    """One node of an incoming graph: an identity, and optionally what to call it and what it is.

    ``extra="allow"`` deliberately: an export carries whatever its producer found interesting per
    node (``file``, ``symbols``, ``loc``, ``owner``, …). None of it is our business to interpret —
    but it is KEPT rather than dropped, so ``size_field`` can name one of those keys and this
    module never has to learn any producer's vocabulary.
    """

    model_config = ConfigDict(extra="allow", use_attribute_docstrings=True)

    id: str
    """The producer's identity for this node — the key its edges name, and the diagram node's
    friendly name once ingested."""
    label: str | None = None
    """What the box says. Omit to derive it from the id via ``label_mode``."""
    kind: str | None = None
    """A DIAGRAM kind ("service", "datastore", …) — not a producer taxonomy. None takes
    ``default_node_kind``."""
    size: float | None = None
    """How big this node IS — its extent, not its box: symbols, lines, headcount, cost.

    Wins over whatever ``size_field`` names, on the same principle that an explicit ``label``
    beats ``label_mode``: a value stated per node beats a rule stated once.

    An explicit size must be NUMERIC (a number, or a string spelling one). ``size`` is a common
    enough word that some producers use it for something else — ``"12kB"``, ``"large"`` — and such
    a value is CARRIED, not interpreted and not rejected: the field stays unset, the original
    stays among the extra keys, and ``size_field="size"`` can still name it (and be told, in those
    words, that it holds no numbers).
    """

    @model_validator(mode="before")
    @classmethod
    def _park_an_unreadable_size(cls, data: dict[str, JsonValue]) -> dict[str, JsonValue]:
        """Take a non-numeric ``size`` off the field, parking it where the after-validator finds it.

        The whole promise of this module is that a producer's export PASTES IN, and one key of one
        node spelling its bytes rather than its extent must not fail the validation of the entire
        graph. It must not vanish either — hence the park rather than a drop.
        """
        raw = data.get("size")
        if raw is None or _numeric(raw) is not None:
            return data
        return {key: value for key, value in data.items() if key != "size"} | {_CARRIED_SIZE: raw}

    @model_validator(mode="after")
    def _carry_an_unreadable_size(self) -> GraphNode:
        """Put the parked value back under its own name, now the field can no longer claim it."""
        extra = self.__pydantic_extra__
        if extra is not None and _CARRIED_SIZE in extra:
            extra["size"] = extra.pop(_CARRIED_SIZE)
        return self


class GraphGroup(BaseModel):
    """Several nodes the CALLER has judged to be one thing, drawn as a single node.

    This is the whole of this module's answer to "the graph is too big to read", and it is
    deliberately not an algorithm. Which nodes are worth a box is a semantic question — a
    judgement about what the picture is *for* — and no centrality score answers it: rank a
    codebase by any of them and you get its exception module, not its architecture. So the
    caller, who understands the domain, names the groups; this just does the mechanical part.

    Collapsing REPLACES the members: edges to any of them re-point at the group, edges between
    them become self-edges (dropped and counted), and parallel edges merge with their weights
    summed. To keep members visible and draw a box around them instead, ingest them normally and
    call ``add_diagram_container`` afterwards with the ids from ``mapping``.
    """

    model_config = ConfigDict(extra="ignore", use_attribute_docstrings=True)

    id: str
    """The group's identity — its friendly name once drawn, and a handle edges may name."""
    members: list[str] = Field(min_length=1)
    """The node ids this group swallows. Each must be declared in ``nodes``, and no node may
    belong to two groups."""
    label: str | None = None
    """What the box says. Omit to derive it from the group's id via ``label_mode``."""
    kind: str | None = None
    """The group's diagram kind. None takes ``default_node_kind``."""
    size: float | None = None
    """The group's extent. Omit for the sum of its members' — the sensible auto — and state it
    only where the whole is not the sum of its parts."""


class GraphEdge(BaseModel):
    """One edge of an incoming graph, accepted under the producer's spelling or ours.

    ``from``/``to`` are what exports write and what Python cannot name, so the fields are aliased:
    both ``{"from": …, "to": …}`` and ``{"source": …, "target": …}`` parse to the same edge.
    """

    model_config = ConfigDict(
        extra="ignore", populate_by_name=True, use_attribute_docstrings=True
    )

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
    """How strong the relation is. It labels the line (``weight_labels``) and sums when parallel
    edges merge — never restyles it."""


class GraphImport(BaseModel):
    """What one ingestion built, and what it declined to build.

    Every drop is counted SEPARATELY, because "37 edges went in and 24 came out" is only useful
    if the caller can tell a self-loop from a collapse from a node it excluded itself.
    """

    model_config = ConfigDict(use_attribute_docstrings=True)

    nodes_created: int = 0
    edges_created: int = 0
    groups_created: int = 0
    """``collapse`` groups drawn, each standing in for its members."""
    nodes_collapsed: int = 0
    """Nodes swallowed by a group, and so not drawn in their own right."""
    self_edges_dropped: int = 0
    """Edges whose two ends were the same node: a box-and-arrow diagram has nowhere to draw one.

    Collapsing feeds this: every edge BETWEEN two members of one group becomes a loop on that
    group, which is the honest reading — the relation is now internal to the thing you drew.
    """
    edges_dropped_filtered: int = 0
    """Edges left dangling by ``include``/``exclude`` taking one of their endpoints out."""
    nodes_filtered: int = 0
    """Nodes ``include``/``exclude`` kept out of the drawing."""
    mapping: dict[str, str] = Field(default_factory=dict)
    """Every id the caller named → the SVG id of the node that REPRESENTS it.

    A collapsed member maps to its group's node, so a handle taken from the input still finds
    whatever ended up standing for it.
    """
    kinds_defaulted: list[str] = Field(default_factory=list)
    """Kinds the export named that no resident theme dresses, drawn as the default kind instead.

    A producer's taxonomy ("calls", "imports") is not a design language, and the whole promise of
    this op is that an export pastes in — so an unservable kind is REPORTED, not raised on. Every
    substitution is listed here, so a real typo ("servcie") is visible rather than silent.
    """
    bounds: tuple[float, float, float, float] | None = None
    """``(x, y, width, height)`` in world units enclosing everything this call drew; None if it
    drew nothing.

    Reported because a graph decides its own size: eight nodes fit a default canvas and forty do
    not, and until now the only way to find that out was to render it and see the boxes fall off
    the edge. Nothing is resized here — a canvas is the caller's decision — but
    ``resize_document(mode="fit", margin=20)`` shrink-wraps to it in one call.
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
    """Whether this id names a FILE: it has a path separator, or a recognised extension.

    The second limb is what a FLAT export needs. ``diagram.py`` carries no directories at all, so
    on the first limb alone the dot would be read as a hierarchy step and the box captioned
    ``py`` — every Python file in the graph captioned ``py``, every Go file ``go``. A dot-tail the
    closed list recognises settles it: this is a filename, and its extension is not its name.
    """
    if any(separator in text for separator in _PATH_SEPARATORS):
        return True
    head, dot, tail = text.rpartition(".")
    return bool(dot and head) and tail.lower() in _EXTENSIONS


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


# --- magnitude ---------------------------------------------------------------


def _magnitude(node: GraphNode, size_field: str | None) -> float | None:
    """How big this node is: its own ``size`` if it states one, else ``size_field``'s value.

    Reading an arbitrary key off the export is why :class:`GraphNode` keeps its extras. The
    alternative — a fixed ``symbols`` field — would bind this op to one producer's vocabulary,
    and the next one calls it ``loc``, ``commits`` or ``cost``.
    """
    if node.size is not None:
        return node.size
    if size_field is None:
        return None
    return _numeric((node.model_extra or {}).get(size_field))


def _scale(value: float, low: float, high: float, span: tuple[float, float], power: float) -> float:
    """Place ``value`` in ``[low, high]``, compressed by ``power`` across the data's own range."""
    smallest, largest = span
    if largest <= smallest:
        return low  # no variation in the data is no reason to draw a difference

    def lift(number: float) -> float:
        return math.pow(max(number, 0.0), power)

    floor, ceiling = lift(smallest), lift(largest)
    if ceiling <= floor:
        # The lift collapsed the span: every drawn magnitude is <= 0, so the data varies but
        # not in any quantity a box can carry. That is the all-equal case in disguise, and it
        # takes the all-equal answer rather than dividing by the zero it just produced.
        return low
    reach = (lift(value) - floor) / (ceiling - floor)
    return low + (high - low) * min(max(reach, 0.0), 1.0)


# --- filtering and merging ---------------------------------------------------


def _hit(node_id: str, patterns: Sequence[str]) -> bool:
    """Whether ``node_id`` is named by ``patterns`` — as a literal id first, then as a glob.

    Literal first because these lists are usually WRITTEN OUT rather than pattern-matched: the
    caller has decided which nodes to leave out, one id at a time. An id may contain glob
    metacharacters of its own (``operator[]``, ``Foo<T>::bar``, a file named ``[id].tsx``), and
    such an id must name itself.
    """
    return any(node_id == pattern or fnmatch(node_id, pattern) for pattern in patterns)


def _selected(node_id: str, include: Sequence[str] | None, exclude: Sequence[str] | None) -> bool:
    """Whether a node survives the filters. ``exclude`` beats ``include``, always."""
    if include is not None and not _hit(node_id, include):
        return False
    return not (exclude is not None and _hit(node_id, exclude))


def _collapse(
    nodes: Sequence[GraphNode], groups: Sequence[GraphGroup]
) -> tuple[list[GraphNode], dict[str, str]]:
    """Substitute each group for its members, returning the nodes to draw and member → group.

    Every way this can be incoherent is refused up front and by name, because a collapse is an
    assertion about what things ARE: a member nobody declared, a node claimed by two groups, or a
    group id already spoken for are all statements that cannot be true at once.

    A group takes the position of its FIRST member in document order, which is the tie-break
    every layout algorithm falls back on — so collapsing part of a graph does not reshuffle the
    rest of it.
    """
    declared = {node.id for node in nodes}
    representative: dict[str, str] = {}
    seen_groups: set[str] = set()
    for group in groups:
        if group.id in seen_groups:
            raise InvalidArgument(f"collapse group {group.id!r} is declared twice")
        seen_groups.add(group.id)
        if group.id in declared and group.id not in group.members:
            raise InvalidArgument(
                f"collapse group {group.id!r} has the same id as a node it does not contain; "
                "give the group its own id, or include that node in its members"
            )
        for member in group.members:
            if member not in declared:
                raise InvalidArgument(
                    f"collapse group {group.id!r} names member {member!r}, which no node "
                    "declares; a group gathers nodes, it cannot invent them"
                )
            held = representative.get(member)
            if held is not None:
                raise InvalidArgument(
                    f"node {member!r} is claimed by both {held!r} and {group.id!r}; "
                    "a node belongs to one group"
                )
            representative[member] = group.id

    by_id = {group.id: group for group in groups}
    drawn: list[GraphNode] = []
    emitted: set[str] = set()
    for node in nodes:
        owner = representative.get(node.id)
        if owner is None:
            drawn.append(node)
            continue
        if owner in emitted:
            continue
        emitted.add(owner)
        group = by_id[owner]
        drawn.append(GraphNode(id=group.id, label=group.label, kind=group.kind))
    return drawn, representative


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


Box = tuple[float, float, float, float]


def _content_bounds(doc: Document, created: Sequence[str]) -> Box | None:
    """The world box enclosing every element ``created`` still names, or None if none has one."""
    boxes: list[list[float]] = []
    for node_id in created:
        element = doc.svg.getElementById(node_id)
        box = None if element is None else _bbox_xywh(element)
        if box is not None:
            boxes.append(box)
    if not boxes:
        return None
    left = min(box[0] for box in boxes)
    top = min(box[1] for box in boxes)
    right = max(box[0] + box[2] for box in boxes)
    bottom = max(box[1] + box[3] for box in boxes)
    return (left, top, right - left, bottom - top)


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

    The question is asked with ``themed=True`` no matter how the ingest was called, because what
    it decides is BOOKKEEPING, not dressing: which kind each node's spec records, and therefore
    which parallel edges are the same edge. An unthemed ingest skips the class attachment — that
    is all ``themed=False`` ever meant — but it must not skip the substitution, or an unthemed
    document would silently store "calls" where a themed one stores "data": two arrows where there
    should be one, an unreported kind, and a spec that the first themed edit of it chokes on.
    """

    def dress(kind: str) -> None:
        prim = "edge" if category == "connector" else _shape_for(serving_theme(doc, kind), kind)
        resolve_dressing(doc, category=category, prim=prim, role=kind, themed=True)

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
    collapse: Sequence[GraphGroup] | None = None,
    include: Sequence[str] | None = None,
    exclude: Sequence[str] | None = None,
    size_field: str | None = None,
    size_labels: bool = False,
    scale_width: tuple[float, float] | None = None,
    scale_height: tuple[float, float] | None = None,
    weight_labels: bool = False,
    layout: GraphLayout = "layered",
    direction: Direction = "LR",
    spacing_main: float | None = None,
    spacing_cross: float | None = None,
    parent: str | None = None,
    themed: bool = True,
) -> GraphImport:
    """Ingest a whole ``{nodes, edges}`` graph as one diagram: boxes, arrows, and a layout.

    ANY graph that already knows what its nodes and edges are — a service map, a dependency
    tree, a state machine, an org chart, a call graph — in one call instead of N. The same
    picture ``add_diagram_node`` × N + ``add_diagram_edge`` × M + ``layout_diagram`` would draw,
    with the bookkeeping done: self edges dropped and COUNTED, parallel edges merged with their
    weights summed, and every node named after its graph id so the caller's own vocabulary keeps
    working afterwards.

    Nothing here decides what MATTERS. There is no ranking, no threshold, no importance score:
    which nodes deserve a box, and which several are really one thing, is a semantic judgement
    about what the picture is for, and it belongs to whoever understands the domain. Say it
    outright — ``exclude`` to leave nodes out, ``collapse`` to fold several into one — and this
    does the mechanical part, in full, and reports exactly what it did.

    The order of operations is fixed, because each step decides what the next one sees:
    collapse groups → filter nodes → reject unknown endpoints → drop edges the filter orphaned →
    drop self edges (which is where an edge INSIDE a collapsed group goes) → resolve kinds →
    merge parallels → draw → lay out.

    Unknown endpoints are rejected BEFORE either drop, against every id the export declared: an
    edge that is both a hole in the data and a casualty of the filter is a hole first.

    Collapsing runs FIRST so that ``include``/``exclude`` see the graph as drawn: a group is
    filtered by its own id, and a member no longer exists as a separate thing to name.

    Kinds are resolved BEFORE the merge so that two parallel edges whose different producer kinds
    both fall back to the default collapse into ONE arrow rather than two identical ones.

    A node's SIZE is its extent — symbols, lines, headcount — and, like weight, it is data: it
    can be written into the label (``size_labels``) and, only if asked, scale the box
    (``scale_width``/``scale_height``, each independently optional). The compression exponent is
    DERIVED rather than offered as a knob: scale one dimension and it maps linearly, scale both
    and each maps by the square root, so that in either case the box's AREA carries the quantity.
    A scaled box is floored at the size its label needs, because a diagram whose text overflows
    its boxes has encoded the data at the cost of the words.

    Unknown endpoints are the one thing here that refuses rather than counts. A node the caller
    left out is a decision; a node NOBODY declared is a hole in the data, and auto-creating a box
    for it would draw a graph the producer never described.
    """
    # Every id the export DECLARED. The filter narrows what gets DRAWN, not what counts as
    # declared — that difference is the whole of how an orphaned edge is told from a dangling one.
    seen: set[str] = set()
    for node in nodes:
        if node.id in seen:
            raise InvalidArgument(f"node id {node.id!r} appears twice in `nodes`; ids are identity")
        seen.add(node.id)

    magnitude: dict[str, float] = {}
    for node in nodes:
        value = _magnitude(node, size_field)
        if value is not None:
            magnitude[node.id] = value
    if size_field is not None:
        # Asked of the KEY, not of the result: a graph whose nodes also state explicit sizes would
        # otherwise have a mistyped size_field pass unmentioned, quietly sizing the boxes by
        # something the caller never asked for. And a key that IS there but holds no numbers is a
        # different mistake from one nobody carries — told apart, since the fix differs.
        carried: list[JsonValue] = []
        for node in nodes:
            extra = node.model_extra or {}
            if size_field in extra:
                carried.append(extra[size_field])
        if not carried:
            offered = sorted({key for node in nodes for key in (node.model_extra or {})})
            raise InvalidArgument(
                f"size_field={size_field!r} names a key no node carries; the nodes offer: "
                f"{', '.join(offered) or '(no extra keys at all)'}"
            )
        if not any(_numeric(value) is not None for value in carried):
            raise InvalidArgument(
                f"size_field={size_field!r} names a key that carries no numeric values "
                f"(e.g. {carried[0]!r}); a size is a magnitude, so it must be a number"
            )

    drawable, representative = _collapse(nodes, collapse or ())
    for group in collapse or ():
        parts = [magnitude[member] for member in group.members if member in magnitude]
        if group.size is not None:
            magnitude[group.id] = group.size
        elif parts:
            magnitude[group.id] = sum(parts)
    # A group id is nameable by an edge too, and a member stays "declared" — it is still a thing
    # the caller told us about, now standing for its group.
    seen |= {node.id for node in drawable}

    kept = [node for node in drawable if _selected(node.id, include, exclude)]
    kept_ids = {node.id for node in kept}

    dropped_filtered = dropped_self = 0
    surviving: list[tuple[GraphEdge, str, str]] = []
    for edge in edges:
        source = representative.get(edge.source, edge.source)
        target = representative.get(edge.target, edge.target)
        endpoints = ((source, "source"), (target, "target"))
        # FIRST, and against what the export DECLARED rather than what survived the filters: a
        # hole in the data is a hole whatever else would have become of the edge. Checked later,
        # a ghost that happened to name itself at both ends read as a self-edge, and a ghost
        # opposite an excluded node read as collateral of the filter — a typo silently absorbed
        # into a count, which is exactly the reading these counters exist to make impossible.
        for end, role in endpoints:
            if end not in seen:
                raise InvalidArgument(
                    f"edge {edge.source!r} -> {edge.target!r} names {end!r} as its {role}, but no "
                    "node declares that id; add it to `nodes` (nodes are never auto-created)"
                )
        if any(end not in kept_ids for end, _role in endpoints):
            dropped_filtered += 1  # declared, then filtered out: the caller's own decision
            continue
        if source == target:
            dropped_self += 1  # includes every edge that was internal to a collapsed group
            continue
        surviving.append((edge, source, target))

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
    )
    edge_kinds, edge_defaulted = _resolve_kinds(
        doc,
        category="connector",
        kinds=[edge.kind for edge, _s, _t in surviving if edge.kind is not None],
        default=default_edge_kind,
    )

    merged: dict[tuple[str, str, str], _Merged] = {}
    for edge, source, target in surviving:
        kind = edge_kinds[edge.kind] if edge.kind is not None else default_edge_kind
        key = (source, target, kind)
        existing = merged.get(key)
        if existing is None:
            merged[key] = _Merged(source, target, kind, edge.label, edge.weight)
        else:
            existing.weight = _merge_weights(existing.weight, edge.weight)
            if existing.label is None:
                existing.label = edge.label

    # A collapse group's id is the CALLER's word ("storage", "ab"), not the producer's, and it
    # shares no hierarchy with the ids around it. Left in, it drives the common prefix to nothing
    # and every remaining box is captioned with the full path that `trimmed` exists to remove —
    # collapsing part of a graph would silently re-caption the part it did not touch.
    group_ids = {group.id for group in collapse or ()}
    prefix = _shared_prefix([node.id for node in kept if node.id not in group_ids])
    dims = [span for span in (scale_width, scale_height) if span is not None]
    # One scaled dimension carries the quantity on its own; two share it, so each takes the root.
    power = 1.0 / len(dims) if dims else 1.0
    drawn_sizes = [magnitude[node.id] for node in kept if node.id in magnitude]
    span = (min(drawn_sizes), max(drawn_sizes)) if drawn_sizes else (0.0, 0.0)

    created: list[str] = []
    mapping: dict[str, str] = {}
    laid: DiagramLayout | None = None
    with _rollback(doc, created):
        for node in kept:
            kind = node_kinds[node.kind] if node.kind is not None else default_node_kind
            # A group says its own name: it was never one of the producer's ids, so there is no
            # prefix to trim off it and no extension to shave.
            mode: LabelMode = "id" if node.id in group_ids else label_mode
            caption = _derive_label(node, mode, prefix)
            value = magnitude.get(node.id)
            if size_labels and value is not None:
                caption = f"{caption} ({_format_weight(value)})"
            width: float | None = None
            height: float | None = None
            if value is not None and dims:
                # Floored at what the label needs: the words are the point of the box.
                measured_w, measured_h = auto_size(doc, kind, caption)
                if scale_width is not None:
                    width = max(measured_w, _scale(value, *scale_width, span, power))
                if scale_height is not None:
                    height = max(measured_h, _scale(value, *scale_height, span, power))
            placed = add_diagram_node(
                doc,
                kind=kind,
                label=caption,
                width=width,
                height=height,
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
    # A member resolves to whatever now stands for it, so a handle taken from the INPUT still
    # finds something — that is the difference between collapsing a node and losing it.
    for member, owner in representative.items():
        if owner in mapping:
            mapping[member] = mapping[owner]
    return GraphImport(
        nodes_created=len(kept),
        edges_created=len(merged),
        groups_created=sum(1 for node in kept if node.id in group_ids),
        nodes_collapsed=len(representative),
        self_edges_dropped=dropped_self,
        edges_dropped_filtered=dropped_filtered,
        nodes_filtered=len(drawable) - len(kept),
        mapping=mapping,
        kinds_defaulted=sorted({*node_defaulted, *edge_defaulted}),
        bounds=_content_bounds(doc, created),
        ranks=None if laid is None else laid.ranks,
        cycles_broken=None if laid is None else laid.cycles_broken,
        edges_rerouted=None if laid is None else laid.edges_rerouted,
    )
