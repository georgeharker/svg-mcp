"""Whole-scope layout for diagram facades: three algorithms, then the pass that applies one.

The algorithms are pure functions over plain data — a list of ``LayoutNode`` (id and size), a
list of edge pairs, and a container→members map in, top-left positions out. They know nothing
about documents, elements or themes, so every ordering rule below is testable on its own.

Layout is deliberately a whole-scope OPT-IN: :func:`layout_diagram` repositions every node in
the scope it is given, explicit prior positions included. Hand placement survives by not calling
it — there is no per-node "pinned" flag, because a layout that has to route around fixed nodes
is a different algorithm, not a flag on this one.

Both axes are named rather than numbered: the MAIN axis is the one ranks advance along and the
CROSS axis is the one nodes spread across within a rank. ``LR`` maps main→x and cross→y, ``TB``
maps main→y and cross→x, and that pair of maps is the whole of the direction handling.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel

from ..model.document import Document
from ..model.errors import InvalidArgument
from ..query.outline import _bbox_xywh
from .diagram import (
    _DEFAULT_GAP,
    _box_of,
    _container_groups,
    _edge_groups,
    _token,
    read_node_spec,
    reflow,
)
from .modify import translate_node
from .themes import serving_theme

Point = tuple[float, float]
Direction = Literal["LR", "TB"]
Algorithm = Literal["layered", "tree", "grid"]

_DEFAULT_GAP_LAYER = 90.0
_SWEEPS = 4  # down, up, down, up — enough to settle the small graphs a diagram actually is


@dataclass(frozen=True, slots=True)
class LayoutNode:
    """One node as an algorithm sees it: an identity and a size.

    Its position in the input list IS document order, which every algorithm uses as its
    tie-break — so a layout is stable against everything except the drawing changing.
    """

    id: str
    w: float
    h: float


@dataclass(frozen=True, slots=True)
class Placement:
    """What an algorithm decided: a top-left per node, plus what it had to notice on the way."""

    positions: dict[str, Point] = field(default_factory=dict)
    ranks: int = 0
    cycles_broken: int = 0


class DiagramLayout(BaseModel):
    """What a layout pass did: nodes moved, ranks made, cycles cut, and the reflow that followed."""

    nodes_placed: int = 0
    ranks: int = 0
    cycles_broken: int = 0
    edges_rerouted: int = 0
    containers_refit: int = 0


# --- axis helpers ------------------------------------------------------------


def _extents(node: LayoutNode, direction: Direction) -> tuple[float, float]:
    """``(main, cross)`` extents of a node under ``direction``."""
    return (node.w, node.h) if direction == "LR" else (node.h, node.w)


def _origins(direction: Direction, origin_x: float, origin_y: float) -> tuple[float, float]:
    """``(main, cross)`` origins under ``direction``."""
    return (origin_x, origin_y) if direction == "LR" else (origin_y, origin_x)


def _point(main: float, cross: float, direction: Direction) -> Point:
    """An ``(x, y)`` from a main/cross pair — the only place the two frames meet."""
    return (main, cross) if direction == "LR" else (cross, main)


def _rank_offsets(
    ranked: Sequence[Sequence[LayoutNode]], direction: Direction, main_origin: float, gap: float
) -> list[float]:
    """Where each rank starts on the main axis: the previous rank's widest node, then the gap."""
    offsets: list[float] = []
    at = main_origin
    for members in ranked:
        offsets.append(at)
        widest = max((_extents(node, direction)[0] for node in members), default=0.0)
        at += widest + gap
    return offsets


# --- grid --------------------------------------------------------------------


def layout_grid(
    nodes: Sequence[LayoutNode],
    *,
    direction: Direction = "LR",
    spacing_main: float,
    spacing_cross: float,
    origin_x: float = 20.0,
    origin_y: float = 20.0,
    columns: int | None = None,
) -> Placement:
    """Lay nodes out on a uniform grid in document order — the layout that ignores the edges.

    Every cell is as big as the largest node, so the rows and columns line up whatever is in
    them, and each node is centered in its own cell. ``LR`` fills along rows, ``TB`` down
    columns; the ORDER is the document's either way, since a grid has no other opinion.
    """
    if not nodes:
        return Placement()
    total = len(nodes)
    ncols = max(1, columns if columns is not None else math.ceil(math.sqrt(total)))
    nrows = math.ceil(total / ncols)
    cell_w = max(node.w for node in nodes)
    cell_h = max(node.h for node in nodes)

    positions: dict[str, Point] = {}
    for index, node in enumerate(nodes):
        if direction == "LR":
            row, column = divmod(index, ncols)
        else:
            column, row = divmod(index, nrows)
        x = origin_x + column * (cell_w + spacing_main) + (cell_w - node.w) / 2.0
        y = origin_y + row * (cell_h + spacing_cross) + (cell_h - node.h) / 2.0
        positions[node.id] = (x, y)
    return Placement(positions=positions, ranks=nrows if direction == "LR" else ncols)


# --- shared graph plumbing ---------------------------------------------------


def _clean_edges(
    nodes: Sequence[LayoutNode], edges: Iterable[tuple[str, str]]
) -> tuple[list[tuple[str, str]], int]:
    """Edges between two nodes of the set, deduped; self-loops are dropped and counted.

    A self-loop cannot be laid out — reversing it leaves it a self-loop — so it is treated as
    the degenerate cycle it is and reported alongside the real ones.
    """
    known = {node.id for node in nodes}
    kept: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    loops = 0
    for source, target in edges:
        if source not in known or target not in known:
            continue
        if source == target:
            loops += 1
            continue
        if (source, target) in seen:
            continue
        seen.add((source, target))
        kept.append((source, target))
    return kept, loops


def _adjacency(
    nodes: Sequence[LayoutNode], edges: Sequence[tuple[str, str]]
) -> dict[str, list[str]]:
    """Successors per node, in edge order, keyed for every node in the set."""
    out: dict[str, list[str]] = {node.id: [] for node in nodes}
    for source, target in edges:
        out[source].append(target)
    return out


# --- tree --------------------------------------------------------------------


def layout_tree(
    nodes: Sequence[LayoutNode],
    edges: Sequence[tuple[str, str]],
    *,
    direction: Direction = "LR",
    spacing_main: float,
    spacing_cross: float,
    origin_x: float = 20.0,
    origin_y: float = 20.0,
) -> Placement:
    """Lay a forest out by depth: rank = distance from a root, leaves in slots, parents centered.

    Roots are the nodes nothing points at (or the first node in document order, if the graph is
    all cycle). The DFS visits children in document order and SKIPS any edge back to a node it
    has already placed — a tree layout cannot honour a second parent, so those edges are counted
    as broken and the drawing stays a tree.
    """
    if not nodes:
        return Placement()
    clean, cycles = _clean_edges(nodes, edges)
    adjacency = _adjacency(nodes, clean)
    targets = {target for _source, target in clean}
    order = [node.id for node in nodes]
    roots = [node_id for node_id in order if node_id not in targets] or [order[0]]

    depth: dict[str, int] = {}
    children: dict[str, list[str]] = {}
    placed: set[str] = set()
    preorder: list[str] = []
    # Every node must land somewhere, so a component the roots cannot reach (a pure cycle
    # hanging off nothing) adopts its own first node as a root when its turn comes.
    for start in [*roots, *order]:
        if start in placed:
            continue
        placed.add(start)
        depth[start] = 0
        children[start] = []
        preorder.append(start)
        stack: list[tuple[str, Iterator[str]]] = [(start, iter(adjacency[start]))]
        while stack:
            node, remaining = stack[-1]
            child = next(remaining, None)
            if child is None:
                stack.pop()
                continue
            if child in placed:
                cycles += 1  # a second parent, or an edge back up: not something a tree can draw
                continue
            placed.add(child)
            depth[child] = depth[node] + 1
            children[node].append(child)
            children[child] = []
            preorder.append(child)
            stack.append((child, iter(adjacency[child])))

    sizes = {node.id: _extents(node, direction) for node in nodes}
    pitch = max(cross for _main, cross in sizes.values()) + spacing_cross
    widest_cross = max(cross for _main, cross in sizes.values())
    main_origin, cross_origin = _origins(direction, origin_x, origin_y)

    centers: dict[str, float] = {}
    slot = 0
    for node_id in preorder:  # pre-order visits the leaves left to right
        if not children[node_id]:
            centers[node_id] = cross_origin + widest_cross / 2.0 + slot * pitch
            slot += 1
    for node_id in reversed(preorder):  # children are always later in a pre-order than their parent
        kids = children[node_id]
        if kids:
            centers[node_id] = sum(centers[kid] for kid in kids) / len(kids)

    rank_count = max(depth.values()) + 1
    ranked = [[node for node in nodes if depth[node.id] == rank] for rank in range(rank_count)]
    offsets = _rank_offsets(ranked, direction, main_origin, spacing_main)
    positions = {
        node.id: _point(
            offsets[depth[node.id]], centers[node.id] - sizes[node.id][1] / 2.0, direction
        )
        for node in nodes
    }
    return Placement(positions=positions, ranks=rank_count, cycles_broken=cycles)


# --- layered (Sugiyama-lite) -------------------------------------------------


def _break_cycles(
    order: Sequence[str], adjacency: Mapping[str, Sequence[str]]
) -> set[tuple[str, str]]:
    """The back edges of a DFS in document order — the ones to reverse to get an acyclic graph."""
    state = dict.fromkeys(order, 0)  # 0 unseen, 1 on the current path, 2 finished
    back: set[tuple[str, str]] = set()
    for start in order:
        if state[start] != 0:
            continue
        state[start] = 1
        stack: list[tuple[str, Iterator[str]]] = [(start, iter(adjacency[start]))]
        while stack:
            node, children = stack[-1]
            child = next(children, None)
            if child is None:
                state[node] = 2
                stack.pop()
                continue
            if state[child] == 0:
                state[child] = 1
                stack.append((child, iter(adjacency[child])))
            elif state[child] == 1:
                back.add((node, child))
    return back


def _longest_path_ranks(
    order: Sequence[str], edges: Sequence[tuple[str, str]]
) -> dict[str, int]:
    """Longest-path layering: sources at rank 0, everyone else one past their deepest source."""
    successors: dict[str, list[str]] = {node_id: [] for node_id in order}
    indegree = dict.fromkeys(order, 0)
    for source, target in edges:
        successors[source].append(target)
        indegree[target] += 1
    rank = dict.fromkeys(order, 0)
    queue = [node_id for node_id in order if indegree[node_id] == 0]
    while queue:
        node = queue.pop(0)
        for target in successors[node]:
            rank[target] = max(rank[target], rank[node] + 1)
            indegree[target] -= 1
            if indegree[target] == 0:
                queue.append(target)
    return rank


def _barycenter_sweep(
    layers: list[list[str]],
    neighbours: Mapping[str, set[str]],
    rank: Mapping[str, int],
    *,
    down: bool,
) -> None:
    """One barycenter pass: sort each rank by the mean position of its neighbours in the last one.

    A node with no neighbour over there keeps its current index as its key, so it drifts with the
    crowd instead of being swept to one end. The sort is stable, so equal keys keep sweep order.
    """
    sequence = range(1, len(layers)) if down else range(len(layers) - 2, -1, -1)
    for index in sequence:
        adjacent = index - 1 if down else index + 1
        reference = {node_id: position for position, node_id in enumerate(layers[adjacent])}
        keys: dict[str, float] = {}
        for position, node_id in enumerate(layers[index]):
            over_there = [
                reference[other]
                for other in neighbours[node_id]
                if rank[other] == adjacent and other in reference
            ]
            keys[node_id] = (
                sum(over_there) / len(over_there) if over_there else float(position)
            )
        layers[index].sort(key=lambda node_id: keys[node_id])


def _regroup_by_container(layer: Sequence[str], group_of: Mapping[str, str]) -> list[str]:
    """Pull each container's members in a rank together, groups ordered by their mean position.

    A barycenter sweep is free to interleave two containers' nodes, which draws boxes that
    overlap. This is the constraint that stops it: members keep the order the sweep gave them,
    only the GROUPS move.
    """
    members: dict[str, list[str]] = {}
    positions: dict[str, list[int]] = {}
    for index, node_id in enumerate(layer):
        key = group_of.get(node_id) or f"\0{node_id}"  # an ungrouped node is its own group
        members.setdefault(key, []).append(node_id)
        positions.setdefault(key, []).append(index)
    order = sorted(members, key=lambda key: sum(positions[key]) / len(positions[key]))
    return [node_id for key in order for node_id in members[key]]


def layout_layered(
    nodes: Sequence[LayoutNode],
    edges: Sequence[tuple[str, str]],
    containers: Mapping[str, Sequence[str]] | None = None,
    *,
    direction: Direction = "LR",
    spacing_main: float,
    spacing_cross: float,
    origin_x: float = 20.0,
    origin_y: float = 20.0,
) -> Placement:
    """Sugiyama-lite: break cycles, layer by longest path, order by barycenter, then place.

    The one addition to the textbook version is the container constraint — after every ordering
    sweep the members of a container are pulled back together within their rank, so a container
    drawn around them afterwards is a box and not a comb.
    """
    if not nodes:
        return Placement()
    order = [node.id for node in nodes]
    clean, cycles = _clean_edges(nodes, edges)
    back = _break_cycles(order, _adjacency(nodes, clean))
    cycles += len(back)
    layout_edges = [
        (target, source) if (source, target) in back else (source, target)
        for source, target in clean
    ]

    rank = _longest_path_ranks(order, layout_edges)
    rank_count = max(rank.values()) + 1
    layers = [
        [node_id for node_id in order if rank[node_id] == index] for index in range(rank_count)
    ]

    neighbours: dict[str, set[str]] = {node_id: set() for node_id in order}
    for source, target in layout_edges:
        neighbours[source].add(target)
        neighbours[target].add(source)
    group_of: dict[str, str] = {}
    for container, member_ids in (containers or {}).items():
        for member in member_ids:
            group_of.setdefault(member, container)

    for sweep in range(_SWEEPS):
        _barycenter_sweep(layers, neighbours, rank, down=sweep % 2 == 0)
        for index, layer in enumerate(layers):
            layers[index] = _regroup_by_container(layer, group_of)

    sizes = {node.id: _extents(node, direction) for node in nodes}
    by_id = {node.id: node for node in nodes}
    main_origin, cross_origin = _origins(direction, origin_x, origin_y)
    offsets = _rank_offsets(
        [[by_id[node_id] for node_id in layer] for layer in layers],
        direction,
        main_origin,
        spacing_main,
    )

    cross_at: dict[str, float] = {}
    for index, layer in enumerate(layers):
        floor = -math.inf
        for position, node_id in enumerate(layer):
            extent = sizes[node_id][1]
            anchors = [
                cross_at[other] + sizes[other][1] / 2.0
                for other in neighbours[node_id]
                if rank[other] == index - 1 and other in cross_at
            ]
            if anchors:
                wanted = sum(anchors) / len(anchors) - extent / 2.0
            else:
                wanted = cross_origin if position == 0 else floor
            cross_at[node_id] = max(wanted, floor)
            floor = cross_at[node_id] + extent + spacing_cross
    shift = cross_origin - min(cross_at.values())
    positions = {
        node_id: _point(offsets[rank[node_id]], value + shift, direction)
        for node_id, value in cross_at.items()
    }
    return Placement(positions=positions, ranks=rank_count, cycles_broken=cycles)


# --- the pass over a real document -------------------------------------------


def _scope_nodes(doc: Document, scope: str | None) -> list[tuple[str, LayoutNode, Point]]:
    """The diagram nodes DIRECTLY under the scope parent, in document order, with their boxes."""
    out: list[tuple[str, LayoutNode, Point]] = []
    for child in doc.resolve_parent(scope):
        if not isinstance(child.tag, str) or read_node_spec(child) is None:
            continue
        node_id = str(child.get_id())
        box = _box_of(doc, node_id)
        if box is None:
            continue
        out.append((node_id, LayoutNode(id=node_id, w=box.w, h=box.h), (box.x, box.y)))
    return out


def layout_diagram(
    doc: Document,
    *,
    algorithm: Algorithm = "layered",
    direction: Direction = "LR",
    scope: str | None = None,
    spacing_main: float | None = None,
    spacing_cross: float | None = None,
    origin_x: float = 20.0,
    origin_y: float = 20.0,
    columns: int | None = None,
) -> DiagramLayout:
    """Lay out every diagram node in one scope, then re-derive the edges and containers on top.

    The node set is the diagram nodes that are DIRECT children of ``scope`` (the document root by
    default); the edges consulted are the ones with both ends in that set, and the containers
    consulted are the ones with a member in it. Everything else in the document is left alone.

    This moves EVERY node in the set — a layout is a decision about the whole picture, so hand
    placement is preserved by not calling it rather than by exempting individual nodes.
    """
    found = _scope_nodes(doc, scope)
    if not found:
        return DiagramLayout()
    nodes = [node for _id, node, _at in found]
    known = {node.id for node in nodes}
    edges = [
        (spec.source, spec.target)
        for _element, spec in _edge_groups(doc)
        if spec.source in known and spec.target in known
    ]
    containers = {}
    for group, spec in _container_groups(doc):
        inside = [member for member in spec.members if member in known]
        if inside:
            containers[str(group.get_id())] = inside

    theme = serving_theme(doc, "service")
    main = spacing_main
    if main is None:
        main = _token(theme, "--gap-layer", _DEFAULT_GAP_LAYER)
    cross = spacing_cross
    if cross is None:
        cross = _token(theme, "--gap-node", _DEFAULT_GAP)

    if algorithm == "grid":
        placement = layout_grid(
            nodes,
            direction=direction,
            spacing_main=main,
            spacing_cross=cross,
            origin_x=origin_x,
            origin_y=origin_y,
            columns=columns,
        )
    elif algorithm == "tree":
        placement = layout_tree(
            nodes,
            edges,
            direction=direction,
            spacing_main=main,
            spacing_cross=cross,
            origin_x=origin_x,
            origin_y=origin_y,
        )
    elif algorithm == "layered":
        placement = layout_layered(
            nodes,
            edges,
            containers,
            direction=direction,
            spacing_main=main,
            spacing_cross=cross,
            origin_x=origin_x,
            origin_y=origin_y,
        )
    else:
        raise InvalidArgument(f"unknown layout algorithm {algorithm!r}: use layered, tree or grid")

    for node_id, _node, (at_x, at_y) in found:
        target = placement.positions[node_id]
        translate_node(doc, node_id, target[0] - at_x, target[1] - at_y)
    flow = reflow(doc, edges=True, containers=True, scope=sorted(known))
    # Auto containers add padding + label headroom OUTSIDE the node extents the placement
    # normalized, so a fitted box can overflow the origin (clipping its label at the canvas
    # edge). Measure the refit boxes and shift the whole scope down/right by any deficit.
    shift_x, shift_y = 0.0, 0.0
    for group, _spec in _container_groups(doc):
        if str(group.get_id()) in containers:
            box = _bbox_xywh(group)
            if box is not None:
                shift_x = max(shift_x, origin_x - box[0])
                shift_y = max(shift_y, origin_y - box[1])
    if shift_x > 0 or shift_y > 0:
        for node_id, _node, _at in found:
            translate_node(doc, node_id, shift_x, shift_y)
        flow = reflow(doc, edges=True, containers=True, scope=sorted(known))
    return DiagramLayout(
        nodes_placed=len(nodes),
        ranks=placement.ranks,
        cycles_broken=placement.cycles_broken,
        edges_rerouted=flow.edges_rerouted,
        containers_refit=flow.containers_refit,
    )
