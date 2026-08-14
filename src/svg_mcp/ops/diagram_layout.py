"""Whole-scope layout for diagram facades: three algorithms, then the pass that applies one.

The algorithms are pure functions over plain data — a list of ``LayoutNode`` (id and size), a
list of edge pairs, and a container→members map in, top-left positions (and, for the layered one,
a lane per rank-spanning edge) out. They know nothing about documents, elements or themes, so
every ordering rule below is testable on its own.

Layout is deliberately a whole-scope OPT-IN: :func:`layout_diagram` repositions every node in
the scope it is given, explicit prior positions included, and hand placement survives by not
calling it. The ONE exception is a node whose spec says ``pinned``, which keeps both of its
coordinates. That flag arrives with the priority coordinate pass and could not have arrived
before it: the old greedy pass had nowhere to put a fixed node — it packed each rank left to
right and every position was a consequence of the one before it. The priority method already
asks, for every node, "who wins when two of you want the same room?", so a fixed node is just
the answer "this one, always" — an infinite-priority wall the rest of the rank packs around.

A pin is a PLACEMENT override, not a topology one: the node's rank is still computed from the
edges, so a node pinned far from its rank's band gets doubling-back edges. That is the caller's
geometry, drawn exactly as asked.

Both axes are named rather than numbered: the MAIN axis is the one ranks advance along and the
CROSS axis is the one nodes spread across within a rank. ``LR`` maps main→x and cross→y, ``TB``
maps main→y and cross→x, and that pair of maps is the whole of the direction handling.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Iterator, Mapping, Sequence
from collections.abc import Set as AbstractSet
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
# Coordinate sweeps: down, up, down. One fewer than the ORDERING sweeps, and deliberately an odd
# number — a downward pass is the one that leaves every rank sitting on its predecessors' medians,
# so a pass order ending upward leaves the last rank holding a median of positions that moved
# after it was read. Three gives every rank a look in both directions and still finishes facing
# the way the drawing reads.
_PACK_SWEEPS = 3
# The cross-axis room one dummy reserves. Small — a lane is a line, not a box — but not zero, or
# the coordinate pass would stack a lane flush against the node above it.
_DUMMY_EXTENT = 8.0

EdgeKey = tuple[str, str]
"""How a caller matches an edge to its lane: the ``(source, target)`` pair it handed in.

Deliberately the AUTHORED pair, not the laid-out one — a cycle-broken edge is reversed inside the
algorithm and its waypoints are turned back before they leave, so a caller never has to know."""


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
    """What an algorithm decided: a top-left per node, plus what it had to notice on the way.

    ``edge_waypoints`` is the lane an algorithm reserved for each edge that spans more than one
    rank: world points, in the same frame as ``positions``, reading source→target. Absent for
    every edge that did not need one, and empty for algorithms that reserve none.
    """

    positions: dict[str, Point] = field(default_factory=dict)
    ranks: int = 0
    cycles_broken: int = 0
    edge_waypoints: dict[EdgeKey, list[Point]] = field(default_factory=dict)


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


def _axes(at: Point, direction: Direction) -> tuple[float, float]:
    """The ``(main, cross)`` a world point sits at — :func:`_point` read the other way round."""
    return at if direction == "LR" else (at[1], at[0])


def _rank_offsets(bands: Sequence[Sequence[float]], main_origin: float, gap: float) -> list[float]:
    """Where each rank starts on the main axis: the previous band's widest member, then the gap.

    ``bands`` is the MAIN extent of everything in each rank, in rank order — real nodes and,
    where an algorithm has them, the dummies standing in for edges passing through.
    """
    offsets: list[float] = []
    at = main_origin
    for extents in bands:
        offsets.append(at)
        at += max(extents, default=0.0) + gap
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
    offsets = _rank_offsets(
        [[sizes[node.id][0] for node in members] for members in ranked], main_origin, spacing_main
    )
    positions = {
        node.id: _point(
            offsets[depth[node.id]], centers[node.id] - sizes[node.id][1] / 2.0, direction
        )
        for node in nodes
    }
    return Placement(positions=positions, ranks=rank_count, cycles_broken=cycles)


# --- layered (Sugiyama) ------------------------------------------------------


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


@dataclass(frozen=True, slots=True)
class _Chain:
    """The dummies standing in for one rank-spanning edge, in rank order from its layout head."""

    key: EdgeKey
    dummies: tuple[str, ...]
    flipped: bool  # the edge was reversed to break a cycle, so its lane reads back-to-front


def _dummy_chains(
    edges: Sequence[tuple[str, str]],
    back: set[tuple[str, str]],
    rank: Mapping[str, int],
) -> tuple[list[tuple[str, str]], dict[str, int], list[_Chain]]:
    """Split every rank-spanning edge into unit hops through one dummy per rank it skips.

    This is the Sugiyama step the layout used to leave out, and it buys two different things at
    once. Crossing minimisation only ever compares ADJACENT ranks, so a long edge is invisible to
    it until it has been cut into short ones; and the dummies, once placed, ARE the lane the edge
    is routed down, which is what stops it being drawn through whatever boxes sit in between.

    Returns the unit-length segment list, the dummies' ranks, and one chain per split edge.
    """
    segments: list[tuple[str, str]] = []
    dummy_rank: dict[str, int] = {}
    chains: list[_Chain] = []
    for source, target in edges:
        flipped = (source, target) in back
        head, tail = (target, source) if flipped else (source, target)
        span = rank[tail] - rank[head]
        if span <= 1:
            segments.append((head, tail))
            continue
        dummies: list[str] = []
        for step in range(1, span):
            # A NUL-prefixed name cannot collide with an SVG id, so a dummy is never mistaken for
            # a node — including by the container regrouping, which sees it as an unnamed
            # singleton and leaves it exactly where the sweep put it.
            dummy = f"\0lane{len(dummy_rank)}"
            dummy_rank[dummy] = rank[head] + step
            dummies.append(dummy)
        previous = head
        for dummy in dummies:
            segments.append((previous, dummy))
            previous = dummy
        segments.append((previous, tail))
        chains.append(_Chain(key=(source, target), dummies=tuple(dummies), flipped=flipped))
    return segments, dummy_rank, chains


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


# --- the cross-axis coordinate pass (Sugiyama's priority method) --------------


def _median(values: Sequence[float]) -> float:
    """The middle of a run of values; the mean of the two middles when there is no single one.

    The MEDIAN, not the mean the ordering sweeps use: ordering only needs a key to sort by, but a
    coordinate is a place to stand, and one far-off neighbour should not drag a node out of line
    with the rest of them.
    """
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2.0


Priority = tuple[int, int, int]
"""Who wins when two slots in a rank want the same room: tier, then degree, then document order."""


def _priorities(
    slots: Sequence[str],
    neighbours: Mapping[str, AbstractSet[str]],
    dummies: AbstractSet[str],
    pinned: AbstractSet[str],
) -> dict[str, Priority]:
    """Rank every slot for the coordinate pass, most important first.

    Three tiers. A PINNED node outranks everything, because its position is a fact rather than a
    preference. Then the dummies: a lane that bends reads as a detour the drawing does not have,
    and straightening one is worth more than straightening any single edge. Then the real nodes by
    their degree in the layout edge set — the node with the most neighbours is the one whose
    median settles the most edges at once. Document order breaks the last tie, which makes the
    priorities a TOTAL order: two nodes can never each be the other's wall, so no rank deadlocks.
    """
    return {
        slot: (
            2 if slot in pinned else 1 if slot in dummies else 0,
            len(neighbours.get(slot, ())),
            -index,
        )
        for index, slot in enumerate(slots)
    }


@dataclass(frozen=True, slots=True)
class _Pack:
    """One cross-axis packing in progress: where every slot sits, and who may push whom.

    ``at`` is the LEADING cross edge of each slot (its top, for ``LR``), and it is the one thing
    here that changes — everything else is the rules the pushing obeys.
    """

    at: dict[str, float]
    sizes: Mapping[str, tuple[float, float]]
    dummies: AbstractSet[str]
    pinned: Mapping[str, float]
    priority: Mapping[str, Priority]
    spacing_cross: float

    def gap(self, first: str, second: str) -> float:
        """The clearance owed between two adjacent slots: the lane pitch if either IS a lane.

        A lane is a line, not a box: charging it the full node gap on both sides blows a rank
        threading several of them out to a multiple of the drawing's width.
        """
        if first in self.dummies or second in self.dummies:
            return _DUMMY_EXTENT
        return self.spacing_cross

    def slack(self, layer: Sequence[str], index: int, step: int) -> float:
        """The free room between ``layer[index]`` and the slot one ``step`` along the rank.

        Never negative: two pins can be placed on top of each other (see :meth:`seed`), and a
        would-be mover must read that as "no room", not as room to travel backwards.
        """
        first, second = (index, index + 1) if step > 0 else (index - 1, index)
        left, right = layer[first], layer[second]
        clear = self.at[right] - self.at[left] - self.sizes[left][1] - self.gap(left, right)
        return max(0.0, clear)

    def seed(self, layer: Sequence[str], cross_origin: float) -> None:
        """Pack one rank at minimal pitch, holding its pinned members exactly where they are.

        The starting guess the sweeps then improve on: everything as tight as its extents and gaps
        allow. A pin is placed at its own coordinate, and the unpinned slots BEFORE it give way
        (backwards pass) — their tight packing was a guess, the pin is not. Two pins with no room
        between them are left overlapping: that is the caller's geometry, drawn as asked.
        """
        floor = cross_origin
        for index, slot in enumerate(layer):
            self.at[slot] = self.pinned.get(slot, floor)
            floor = self.at[slot] + self.sizes[slot][1]
            if index + 1 < len(layer):
                floor += self.gap(slot, layer[index + 1])
        for index in range(len(layer) - 2, -1, -1):
            slot, after = layer[index], layer[index + 1]
            if slot in self.pinned:
                continue
            room = self.at[after] - self.gap(slot, after) - self.sizes[slot][1]
            self.at[slot] = min(self.at[slot], room)

    def shove(self, layer: Sequence[str], index: int, delta: float) -> float:
        """Move ``layer[index]`` by ``delta``, pushing lower-priority slots along; how far it got.

        The whole of the priority method is in the two ways this can end. A neighbour of LOWER
        standing is pushed ahead of the mover, recursively, and gives up exactly as much room as
        it has. A neighbour of higher or equal standing — and any pinned node, whatever its degree
        — is a WALL: the mover is clamped short of it rather than the wall being disturbed, so a
        sweep can never undo a placement a more important node has already earned.
        """
        step = 1 if delta > 0 else -1
        want = abs(delta)
        got = want
        neighbour = index + step
        if 0 <= neighbour < len(layer):
            free = self.slack(layer, index, step)
            if want > free:
                mover, blocker = layer[index], layer[neighbour]
                if blocker in self.pinned or self.priority[blocker] >= self.priority[mover]:
                    got = free
                else:
                    got = free + self.shove(layer, neighbour, (want - free) * step)
        self.at[layer[index]] += got * step
        return got

    def pull(
        self,
        layer: Sequence[str],
        neighbours: Mapping[str, AbstractSet[str]],
        ranked: Mapping[str, int],
        adjacent: int,
    ) -> None:
        """Pull one rank toward the medians of its neighbours in the rank ``adjacent`` to it.

        In DESCENDING priority, so the slot that most wants to be somewhere gets there first and
        is a wall to everyone after it. A slot with no neighbour over there has no opinion and
        stays where the packing put it; a pinned one has no opinion either, by definition.
        """
        places = {slot: index for index, slot in enumerate(layer)}
        for slot in sorted(layer, key=lambda slot: self.priority[slot], reverse=True):
            if slot in self.pinned:
                continue
            anchors = [
                self.at[other] + self.sizes[other][1] / 2.0
                for other in neighbours[slot]
                if ranked[other] == adjacent
            ]
            if not anchors:
                continue
            delta = _median(anchors) - self.sizes[slot][1] / 2.0 - self.at[slot]
            if delta:
                self.shove(layer, places[slot], delta)


def _normalization(
    cross_at: Mapping[str, float],
    order: Sequence[str],
    cross_origin: float,
    *,
    pinned: bool,
) -> float:
    """How far the packed drawing has to slide to start at the origin — ZERO if anything is pinned.

    Say it out loud, because it is the one rule where two promises collide. Normally the pass
    normalizes on the REAL nodes: whatever the sweeps did, the drawing is slid so its topmost node
    sits on the cross origin, and a lane that ended up above it simply runs there — the origin is
    a statement about where the DRAWING starts, and a lane is not part of its extent.

    A pin is a promise about absolute coordinates, and a normalizing shift is a promise about
    where the drawing starts; they cannot both be kept. The pin wins, wholesale: ONE pinned node
    anywhere in the scope turns normalization off for EVERYTHING. Not "shift the others and leave
    the pin", which would slide the drawing out from under the node it was pinned relative to and
    make the pin meaningless; not "shift so the pin lands back where it was", which is the same
    number as no shift at all whenever the pin is what sets the minimum, and a lie otherwise. A
    pinned scope keeps the frame the pin is expressed in, and the consequence a caller must expect
    is that unpinned nodes may then sit ABOVE the cross origin (the sweeps move freely in both
    directions) — the drawing is anchored to the pin now, not to ``origin_x``/``origin_y``.
    """
    if pinned:
        return 0.0
    return cross_origin - min(cross_at[node_id] for node_id in order)


def _pack_ranks(
    layers: Sequence[Sequence[str]],
    slots: Sequence[str],
    sizes: Mapping[str, tuple[float, float]],
    neighbours: Mapping[str, AbstractSet[str]],
    ranked: Mapping[str, int],
    *,
    dummies: AbstractSet[str],
    pinned: Mapping[str, float],
    spacing_cross: float,
    cross_origin: float,
) -> dict[str, float]:
    """Cross-axis coordinates by Sugiyama's PRIORITY METHOD: pack tight, then straighten by rank.

    What this replaced was a single greedy pass that read each rank left to right, put every node
    at its neighbours' mean, and then took ``max(wanted, floor)`` so it never overlapped the node
    before it. That ``max`` only ever pushed one way, so a rank could be dragged across but never
    corrected back, and the drift COMPOUNDED rank by rank: on a 16-node acceptance graph the
    drawing spread 2488 units across an axis that wanted about 800, and read as a staircase.

    The cure is to stop deriving a position from the position beside it. Every rank is packed at
    minimal pitch first, so the spread of a rank is decided by what is IN it; then each sweep
    pulls nodes toward the median of their neighbours in the previous (or next) rank, in
    descending priority, and a node moving pushes only slots that matter less than it does.
    Nothing accumulates, because nothing is defined in terms of its neighbour's coordinate.

    Returns the leading cross edge of every slot, dummies included — the lanes read theirs back.
    """
    pack = _Pack(
        at={},
        sizes=sizes,
        dummies=dummies,
        pinned=pinned,
        priority=_priorities(slots, neighbours, dummies, pinned.keys()),
        spacing_cross=spacing_cross,
    )
    for layer in layers:
        pack.seed(layer, cross_origin)
    for sweep in range(_PACK_SWEEPS):
        down = sweep % 2 == 0
        sequence = range(1, len(layers)) if down else range(len(layers) - 2, -1, -1)
        for index in sequence:
            pack.pull(layers[index], neighbours, ranked, index - 1 if down else index + 1)
    return pack.at


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
    pinned: Mapping[str, Point] | None = None,
) -> Placement:
    """Sugiyama: break cycles, layer by longest path, order by barycenter, place by priority.

    Two additions to the textbook version. Every edge that spans more than one rank is cut into
    unit hops through DUMMY nodes, which take part in the ordering sweeps and the coordinate pass
    exactly as real nodes do; their cross-axis centers come back out as that edge's waypoints, so
    a long edge follows a lane the layout reserved for it instead of a straight run through
    whatever it happens to pass over. And the container constraint: after every ordering sweep the
    members of a container are pulled back together within their rank, so a container drawn around
    them afterwards is a box and not a comb. Dummies belong to no container, so regrouping treats
    each of them as its own singleton group and leaves it where the sweep put it.

    ``pinned`` maps a node id to the world top-left it is to KEEP. Such a node is placed on both
    axes by the caller, not by this function: its rank is still computed (and its edges still get
    lanes through it), but the main-axis band its rank sits in does not apply to it, and on the
    cross axis it is an infinite-priority wall the rest of its rank packs around. Pinning also
    turns the final normalization OFF for the whole drawing — see :func:`_normalization`.
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
    segments, dummy_rank, chains = _dummy_chains(clean, back, rank)
    ranked = {**rank, **dummy_rank}
    slots = [*order, *dummy_rank]  # document order first, then the dummies as they were made
    layers = [[slot for slot in slots if ranked[slot] == index] for index in range(rank_count)]

    neighbours: dict[str, set[str]] = {slot: set() for slot in slots}
    for source, target in segments:
        neighbours[source].add(target)
        neighbours[target].add(source)
    group_of: dict[str, str] = {}
    for container, member_ids in (containers or {}).items():
        for member in member_ids:
            group_of.setdefault(member, container)

    for sweep in range(_SWEEPS):
        _barycenter_sweep(layers, neighbours, ranked, down=sweep % 2 == 0)
        for index, layer in enumerate(layers):
            layers[index] = _regroup_by_container(layer, group_of)

    sizes = {node.id: _extents(node, direction) for node in nodes}
    sizes.update(dict.fromkeys(dummy_rank, (_DUMMY_EXTENT, _DUMMY_EXTENT)))
    main_origin, cross_origin = _origins(direction, origin_x, origin_y)
    bands = [[sizes[slot][0] for slot in layer] for layer in layers]
    offsets = _rank_offsets(bands, main_origin, spacing_main)
    # Where a lane crosses each rank: the middle of that rank's band, so the waypoint sits level
    # with the nodes it is threading past rather than in the gap on one side of them.
    band_centers = [
        offset + max(band, default=0.0) / 2.0
        for offset, band in zip(offsets, bands, strict=True)
    ]

    holds = pinned or {}
    held = {
        node_id: _axes(at, direction)[1]
        for node_id, at in holds.items()
        if node_id in rank
    }
    cross_at = _pack_ranks(
        layers,
        slots,
        sizes,
        neighbours,
        ranked,
        dummies=frozenset(dummy_rank),
        pinned=held,
        spacing_cross=spacing_cross,
        cross_origin=cross_origin,
    )
    shift = _normalization(cross_at, order, cross_origin, pinned=bool(held))
    positions = {
        node_id: holds[node_id]
        if node_id in held
        else _point(offsets[rank[node_id]], cross_at[node_id] + shift, direction)
        for node_id in order
    }
    waypoints: dict[EdgeKey, list[Point]] = {}
    for chain in chains:
        lane = [
            _point(
                band_centers[ranked[dummy]],
                cross_at[dummy] + _DUMMY_EXTENT / 2.0 + shift,
                direction,
            )
            for dummy in chain.dummies
        ]
        waypoints[chain.key] = list(reversed(lane)) if chain.flipped else lane
    return Placement(
        positions=positions,
        ranks=rank_count,
        cycles_broken=cycles,
        edge_waypoints=waypoints,
    )


# --- the pass over a real document -------------------------------------------


@dataclass(frozen=True, slots=True)
class _Found:
    """One diagram node the pass is about to move: its identity, its size, where it is, and pins."""

    id: str
    node: LayoutNode
    at: Point
    pinned: bool


def _scope_nodes(doc: Document, scope: str | None) -> list[_Found]:
    """The diagram nodes DIRECTLY under the scope parent, in document order, with their boxes."""
    out: list[_Found] = []
    for child in doc.resolve_parent(scope):
        if not isinstance(child.tag, str):
            continue
        spec = read_node_spec(child)
        if spec is None:
            continue
        node_id = str(child.get_id())
        box = _box_of(doc, node_id)
        if box is None:
            continue
        out.append(
            _Found(
                id=node_id,
                node=LayoutNode(id=node_id, w=box.w, h=box.h),
                at=(box.x, box.y),
                pinned=spec.pinned,
            )
        )
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

    This moves every node in the set except the ones whose spec says ``pinned`` — a layout is a
    decision about the whole picture, so hand placement is preserved by not calling it, and a
    single node's placement by saying so on that node. A pinned node keeps BOTH coordinates and
    the layered packer treats it as an infinite-priority wall the rest of its rank packs around.
    Its RANK is still computed from its edges, so a node pinned far from where its rank lands has
    edges that double back to reach it; that is the caller's geometry, drawn as asked.

    A pin also anchors the drawing's frame: with any node pinned, the pass performs NEITHER the
    normalization onto ``origin_x``/``origin_y`` NOR the container-overflow correction, because
    both are whole-scope shifts and a shifted pin is not a pin. Unpinned nodes may then sit above
    or left of the origin. Pin nothing and both corrections apply exactly as before.

    Rank-spanning edges are routed down the LANES the layered algorithm reserved for them. Those
    lanes are threaded into this pass's reflow and deliberately not stored anywhere: they are
    scaffolding for one placement, and freezing them into the document would leave every later
    edit dragging around geometry that describes a layout nobody can see any more. The visible
    consequence is that a plain ``reflow`` afterwards re-derives DIRECT routes — run the layout
    again, or pin the route with ``edit_diagram_edge(waypoints=...)``, which no layout overrides.

    An edge that already carries pinned waypoints keeps them: layout still MOVES its nodes (pinning
    a route is not pinning the boxes), so a pinned route can end up stale — that is the caller's
    own geometry doing exactly what they asked for.
    """
    found = _scope_nodes(doc, scope)
    if not found:
        return DiagramLayout()
    nodes = [item.node for item in found]
    known = {node.id for node in nodes}
    pinned = {item.id: item.at for item in found if item.pinned}
    edges: list[tuple[str, str]] = []
    routed: dict[EdgeKey, list[str]] = {}
    for element, spec in _edge_groups(doc):
        if spec.source in known and spec.target in known:
            edges.append((spec.source, spec.target))
            routed.setdefault((spec.source, spec.target), []).append(str(element.get_id()))
    containers = {}
    for group, group_spec in _container_groups(doc):
        inside = [member for member in group_spec.members if member in known]
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
            pinned=pinned,
        )
    else:
        raise InvalidArgument(f"unknown layout algorithm {algorithm!r}: use layered, tree or grid")

    # A pinned node is skipped rather than translated by zero: the layered packer already gave it
    # back exactly where it was, but grid and tree have no notion of a pin at all (they place it,
    # and are simply not listened to), and skipping is the only thing that keeps the promise
    # byte-for-byte in every case. Those two may then run something over the top of it — a pin in
    # a layout that has no room to make is the caller asking for exactly that.
    for item in found:
        if item.pinned:
            continue
        target = placement.positions[item.id]
        translate_node(doc, item.id, target[0] - item.at[0], target[1] - item.at[1])
    lanes = {
        edge_id: points
        for key, points in placement.edge_waypoints.items()
        for edge_id in routed.get(key, ())
    }
    flow = reflow(doc, edges=True, containers=True, scope=sorted(known), _via=lanes)
    # Auto containers add padding + label headroom OUTSIDE the node extents the placement
    # normalized, so a fitted box can overflow the origin (clipping its label at the canvas
    # edge). Measure the refit boxes and shift the whole scope down/right by any deficit.
    # A pinned scope is exempt for the same reason it skips normalization: this is a whole-scope
    # shift, and shifting a pin is not honouring it.
    shift_x, shift_y = 0.0, 0.0
    for group, _spec in _container_groups(doc):
        if str(group.get_id()) in containers and not pinned:
            box = _bbox_xywh(group)
            if box is not None:
                shift_x = max(shift_x, origin_x - box[0])
                shift_y = max(shift_y, origin_y - box[1])
    if shift_x > 0 or shift_y > 0:
        for item in found:
            translate_node(doc, item.id, shift_x, shift_y)
        lanes = {
            edge_id: [(x + shift_x, y + shift_y) for x, y in points]
            for edge_id, points in lanes.items()
        }
        flow = reflow(doc, edges=True, containers=True, scope=sorted(known), _via=lanes)
    return DiagramLayout(
        nodes_placed=len(nodes),
        ranks=placement.ranks,
        cycles_broken=placement.cycles_broken,
        edges_rerouted=flow.edges_rerouted,
        containers_refit=flow.containers_refit,
    )
