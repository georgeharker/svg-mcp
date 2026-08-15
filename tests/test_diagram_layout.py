"""Diagram layout: the three placement algorithms as pure data, then the pass that applies one."""

from __future__ import annotations

import io
import math
import re
from collections.abc import Iterator, Sequence

import pytest

from svg_mcp import ops
from svg_mcp.model import Document
from svg_mcp.ops.diagram import Box, _box_of, _container_groups, _edge_groups, read_node_spec
from svg_mcp.ops.diagram_layout import (
    Direction,
    LayoutNode,
    layout_grid,
    layout_layered,
    layout_tree,
)
from svg_mcp.query.outline import _bbox_xywh
from svg_mcp.render import get_renderer
from svg_mcp.render.base import RenderRequest
from svg_mcp.serialize import export_svg
from svg_mcp.session import DocumentStore

Point = tuple[float, float]

# How far inside a node's own box an edge has to run before it counts as crossing it: half a
# unit, which is under the stroke width and well under anything visible.
TOL = 0.5
_CURVE_STEPS = 12


def _doc() -> Document:
    return DocumentStore().create(900, 600)[1]


def _nodes(*ids: str, w: float = 40.0, h: float = 40.0) -> list[LayoutNode]:
    return [LayoutNode(id=node_id, w=w, h=h) for node_id in ids]


def _by_cross(placement: dict[str, Point]) -> list[str]:
    """The node ids in cross-axis (y, for LR) order — the ordering a rank actually drew."""
    return [node_id for node_id, _at in sorted(placement.items(), key=lambda item: item[1][1])]


# --- 0. layout invariants ----------------------------------------------------
#
# A layered layout is a heuristic, so its exact coordinates are an implementation detail that
# every improvement to the algorithm is allowed to change. What must NOT change is what makes the
# drawing readable, and these are those properties, stated once and asserted by name. Pinning
# coordinates instead would make each improvement look like a regression.


def ranks_advance(positions: Sequence[Point], direction: Direction) -> bool:
    """True when positions given in RANK order never go backwards along the main axis."""
    main = [at[0] if direction == "LR" else at[1] for at in positions]
    return all(first <= second for first, second in zip(main, main[1:], strict=False))


def _node_boxes(doc: Document) -> dict[str, Box]:
    """Every diagram node's world box, by id — what a layout separates and a route stays out of."""
    boxes: dict[str, Box] = {}
    for node in doc.svg.iter():
        if isinstance(node.tag, str) and read_node_spec(node) is not None:
            node_id = str(node.get_id())
            box = _box_of(doc, node_id)
            if box is not None:
                boxes[node_id] = box
    return boxes


def _gap(a: Box, b: Box) -> float:
    """The clear space between two boxes: negative when they overlap, by how deep."""
    across = max(a.x - (b.x + b.w), b.x - (a.x + a.w))
    down = max(a.y - (b.y + b.h), b.y - (a.y + a.h))
    return max(across, down)


def min_gap(doc: Document) -> float:
    """The tightest gap between any two diagram nodes; infinite when there are fewer than two."""
    boxes = list(_node_boxes(doc).values())
    gaps = [_gap(a, b) for index, a in enumerate(boxes) for b in boxes[index + 1 :]]
    return min(gaps, default=math.inf)


def no_box_overlap(doc: Document) -> bool:
    """True when no two diagram-node boxes intersect."""
    return min_gap(doc) > -TOL


def _bezier(points: Sequence[Point]) -> Iterator[Point]:
    """A curve sampled as a polyline — enough resolution to catch it clipping a corner."""
    order = len(points) - 1
    for step in range(1, _CURVE_STEPS + 1):
        t = step / _CURVE_STEPS
        x = sum(
            math.comb(order, i) * (1 - t) ** (order - i) * t**i * point[0]
            for i, point in enumerate(points)
        )
        y = sum(
            math.comb(order, i) * (1 - t) ** (order - i) * t**i * point[1]
            for i, point in enumerate(points)
        )
        yield (x, y)


def path_points(d: str) -> list[Point]:
    """The polyline a route draws, curves sampled — every command this module emits is absolute."""
    out: list[Point] = []
    at: Point = (0.0, 0.0)
    for command, body in re.findall(r"([MLQC])([^MLQC]*)", d):
        numbers = [float(n) for n in re.findall(r"-?\d+(?:\.\d+)?", body)]
        points = list(zip(numbers[0::2], numbers[1::2], strict=True))
        if command in ("M", "L"):
            out.extend(points)
        else:
            out.extend(_bezier([at, *points]))
        at = points[-1]
    return out


def passes_through(points: Sequence[Point], at: Point, tolerance: float) -> bool:
    """True when a drawn polyline comes within ``tolerance`` of ``at`` anywhere along it.

    Anywhere ALONG it, not at a vertex: a point the route runs straight over is dropped from the
    vertex list (and a corner is replaced by its rounding), so vertex equality would be asking
    about the encoding rather than about the drawing.
    """
    for start, end in zip(points, points[1:], strict=False):
        span = math.dist(start, end)
        if span <= 1e-12:
            near = start
        else:
            along = (
                (at[0] - start[0]) * (end[0] - start[0]) + (at[1] - start[1]) * (end[1] - start[1])
            ) / span**2
            held = min(1.0, max(0.0, along))
            near = (start[0] + held * (end[0] - start[0]), start[1] + held * (end[1] - start[1]))
        if math.dist(near, at) <= tolerance:
            return True
    return False


def _enters(start: Point, end: Point, box: Box) -> bool:
    """True when the segment ``start``→``end`` gets inside ``box`` by more than the tolerance."""
    left, top = box.x + TOL, box.y + TOL
    right, bottom = box.x + box.w - TOL, box.y + box.h - TOL
    if right <= left or bottom <= top:
        return False
    near, far = 0.0, 1.0
    for delta, from_, low, high in (
        (end[0] - start[0], start[0], left, right),
        (end[1] - start[1], start[1], top, bottom),
    ):
        if abs(delta) < 1e-12:
            if from_ < low or from_ > high:
                return False
            continue
        first, second = (low - from_) / delta, (high - from_) / delta
        near, far = max(near, min(first, second)), min(far, max(first, second))
        if near > far:
            return False
    return True


def edges_avoid_boxes(doc: Document) -> list[str]:
    """The edges that run THROUGH a node box that is not one of their own two endpoints.

    The acceptance invariant lanes exist for: an edge crossing a box it has nothing to do with is
    the one routing fault a reader always notices, and before dummy nodes the layered layout had
    no way to avoid it on a rank-spanning edge.
    """
    boxes = _node_boxes(doc)
    offenders: list[str] = []
    for element, spec in _edge_groups(doc):
        path = next((child for child in element if str(getattr(child, "TAG", "")) == "path"), None)
        if path is None:
            continue
        points = path_points(str(path.get("d")))
        obstacles = [
            box for node_id, box in boxes.items() if node_id not in (spec.source, spec.target)
        ]
        if any(
            _enters(start, end, box)
            for start, end in zip(points, points[1:], strict=False)
            for box in obstacles
        ):
            offenders.append(str(element.get_id()))
    return offenders


# --- 1. grid -----------------------------------------------------------------


def test_a_grid_defaults_to_a_square_of_columns_and_fills_row_major() -> None:
    nodes = [LayoutNode(id="wide", w=80.0, h=30.0), *_nodes("b", "c", "d", "e", w=40.0, h=20.0)]
    placement = layout_grid(nodes, spacing_main=10.0, spacing_cross=5.0, origin_x=0.0, origin_y=0.0)
    # 5 nodes → ceil(sqrt(5)) = 3 columns, cells sized to the largest node (80 x 30).
    assert placement.ranks == 2
    assert placement.positions["wide"] == (0.0, 0.0)
    assert placement.positions["b"] == (110.0, 5.0)  # cell 1 starts at 90; centered in an 80 cell
    assert placement.positions["c"] == (200.0, 5.0)
    assert placement.positions["d"] == (20.0, 40.0)  # wrapped to row 1
    assert placement.positions["e"] == (110.0, 40.0)


def test_an_explicit_column_count_reshapes_the_grid() -> None:
    placement = layout_grid(
        _nodes("a", "b", "c", "d", "e"),
        spacing_main=10.0,
        spacing_cross=10.0,
        origin_x=0.0,
        origin_y=0.0,
        columns=2,
    )
    assert placement.ranks == 3
    assert placement.positions["c"] == (0.0, 50.0)
    assert placement.positions["e"] == (0.0, 100.0)


def test_a_top_to_bottom_grid_fills_down_the_columns() -> None:
    placement = layout_grid(
        _nodes("a", "b", "c", "d", "e"),
        direction="TB",
        spacing_main=10.0,
        spacing_cross=10.0,
        origin_x=0.0,
        origin_y=0.0,
        columns=3,
    )
    # 3 columns of 2: a and b go down the first column, not across the first row.
    assert placement.positions["a"] == (0.0, 0.0)
    assert placement.positions["b"] == (0.0, 50.0)
    assert placement.positions["c"] == (50.0, 0.0)


# --- 2. tree -----------------------------------------------------------------


def test_a_chain_ranks_by_depth_along_the_main_axis() -> None:
    placement = layout_tree(
        _nodes("a", "b", "c"),
        [("a", "b"), ("b", "c")],
        spacing_main=50.0,
        spacing_cross=10.0,
        origin_x=0.0,
        origin_y=0.0,
    )
    assert placement.ranks == 3
    assert [placement.positions[node][0] for node in ("a", "b", "c")] == [0.0, 90.0, 180.0]
    assert {placement.positions[node][1] for node in ("a", "b", "c")} == {0.0}


def test_a_parent_is_centered_over_its_two_children() -> None:
    placement = layout_tree(
        _nodes("root", "left", "right"),
        [("root", "left"), ("root", "right")],
        spacing_main=50.0,
        spacing_cross=10.0,
        origin_x=0.0,
        origin_y=0.0,
    )
    assert placement.positions["left"][1] == 0.0
    assert placement.positions["right"][1] == 50.0  # one slot down: 40 wide plus the 10 gap
    assert placement.positions["root"][1] == 25.0  # exactly between them


def test_a_tree_skips_the_edge_that_would_give_a_node_two_parents() -> None:
    placement = layout_tree(
        _nodes("a", "b", "c"),
        [("a", "b"), ("b", "c"), ("a", "c")],
        spacing_main=50.0,
        spacing_cross=10.0,
        origin_x=0.0,
        origin_y=0.0,
    )
    assert placement.cycles_broken == 1  # a→c arrives at a node the DFS has already placed
    assert placement.positions["c"][0] == 180.0  # c stays where b's child belongs


def test_a_graph_that_is_all_cycle_still_gets_a_root() -> None:
    placement = layout_tree(
        _nodes("a", "b"),
        [("a", "b"), ("b", "a")],
        spacing_main=50.0,
        spacing_cross=10.0,
        origin_x=0.0,
        origin_y=0.0,
    )
    assert placement.cycles_broken == 1
    assert placement.positions["a"][0] == 0.0  # the first node in document order took the role
    assert placement.positions["b"][0] == 90.0


def test_a_component_the_roots_cannot_reach_is_still_placed() -> None:
    placement = layout_tree(
        _nodes("a", "b", "c", "d"),
        [("a", "b"), ("c", "d"), ("d", "c")],
        spacing_main=50.0,
        spacing_cross=10.0,
        origin_x=0.0,
        origin_y=0.0,
    )
    assert set(placement.positions) == {"a", "b", "c", "d"}


def test_top_to_bottom_transposes_the_whole_drawing() -> None:
    nodes = _nodes("root", "left", "right")
    edges = [("root", "left"), ("root", "right")]
    across = layout_tree(
        nodes, edges, spacing_main=50.0, spacing_cross=10.0, origin_x=0.0, origin_y=0.0
    )
    down = layout_tree(
        nodes,
        edges,
        direction="TB",
        spacing_main=50.0,
        spacing_cross=10.0,
        origin_x=0.0,
        origin_y=0.0,
    )
    assert {node: (y, x) for node, (x, y) in across.positions.items()} == down.positions


# --- 3. layered --------------------------------------------------------------


def test_a_layered_chain_advances_one_rank_at_a_time() -> None:
    placement = layout_layered(
        _nodes("a", "b", "c"),
        [("a", "b"), ("b", "c")],
        spacing_main=50.0,
        spacing_cross=10.0,
        origin_x=0.0,
        origin_y=0.0,
    )
    assert placement.ranks == 3
    assert [placement.positions[node][0] for node in ("a", "b", "c")] == [0.0, 90.0, 180.0]


def test_a_diamond_puts_both_middles_in_one_rank_and_the_join_after_them() -> None:
    placement = layout_layered(
        _nodes("a", "b", "c", "d"),
        [("a", "b"), ("a", "c"), ("b", "d"), ("c", "d")],
        spacing_main=50.0,
        spacing_cross=10.0,
        origin_x=0.0,
        origin_y=0.0,
    )
    assert placement.ranks == 3
    assert placement.positions["b"][0] == placement.positions["c"][0] == 90.0
    assert placement.positions["d"][0] == 180.0
    assert placement.positions["d"][1] == 25.0  # the join centers between the two it joins


def test_the_barycenter_sweeps_untangle_a_crossing() -> None:
    # Document order puts x before y, but a→y and b→x means that order draws two crossed edges.
    placement = layout_layered(
        _nodes("a", "b", "x", "y"),
        [("a", "y"), ("b", "x")],
        spacing_main=50.0,
        spacing_cross=10.0,
        origin_x=0.0,
        origin_y=0.0,
    )
    assert _by_cross(placement.positions) == ["a", "y", "b", "x"]
    assert placement.positions["y"][1] < placement.positions["x"][1]  # swept past each other


def test_nodes_sharing_a_container_are_pulled_together_within_their_rank() -> None:
    nodes = _nodes("root", "a", "b", "c")
    edges = [("root", "a"), ("root", "b"), ("root", "c")]
    loose = layout_layered(
        nodes, edges, spacing_main=50.0, spacing_cross=10.0, origin_x=0.0, origin_y=0.0
    )
    grouped = layout_layered(
        nodes,
        edges,
        {"box": ["a", "c"]},
        spacing_main=50.0,
        spacing_cross=10.0,
        origin_x=0.0,
        origin_y=0.0,
    )

    # The rank the container constrains, read in cross order. Root is left out of the comparison
    # because it is not in that rank: the coordinate pass centers it on its children's median, so
    # WHERE it sorts relative to them is a fact about parents, not about containers.
    def rank_one(placement: dict[str, Point]) -> list[str]:
        return [node for node in _by_cross(placement) if node != "root"]

    assert rank_one(loose.positions) == ["a", "b", "c"]  # document order, b in between
    assert rank_one(grouped.positions) == ["a", "c", "b"]  # the container's two, adjacent
    assert grouped.positions["c"][1] - grouped.positions["a"][1] == 50.0  # one slot apart


def test_a_cycle_is_broken_by_reversing_one_edge_and_counted() -> None:
    placement = layout_layered(
        _nodes("a", "b"),
        [("a", "b"), ("b", "a")],
        spacing_main=50.0,
        spacing_cross=10.0,
        origin_x=0.0,
        origin_y=0.0,
    )
    assert placement.cycles_broken == 1
    assert placement.ranks == 2  # the reversed edge left an acyclic graph to layer


def test_a_self_loop_cannot_be_laid_out_and_is_counted_as_the_cycle_it_is() -> None:
    placement = layout_layered(
        _nodes("a", "b"),
        [("a", "a"), ("a", "b")],
        spacing_main=50.0,
        spacing_cross=10.0,
        origin_x=0.0,
        origin_y=0.0,
    )
    assert placement.cycles_broken == 1
    assert placement.ranks == 2


def test_layered_top_to_bottom_transposes_the_whole_drawing() -> None:
    nodes = _nodes("a", "b", "c", "d")
    edges = [("a", "b"), ("a", "c"), ("b", "d"), ("c", "d")]
    across = layout_layered(
        nodes, edges, spacing_main=50.0, spacing_cross=10.0, origin_x=0.0, origin_y=0.0
    )
    down = layout_layered(
        nodes,
        edges,
        direction="TB",
        spacing_main=50.0,
        spacing_cross=10.0,
        origin_x=0.0,
        origin_y=0.0,
    )
    assert {node: (y, x) for node, (x, y) in across.positions.items()} == down.positions


# --- 3b. dummy nodes for rank-spanning edges ---------------------------------


def test_an_edge_that_skips_a_rank_gets_one_waypoint_for_it() -> None:
    placement = layout_layered(
        _nodes("a", "b", "c"),
        [("a", "b"), ("b", "c"), ("a", "c")],
        spacing_main=50.0,
        spacing_cross=10.0,
        origin_x=0.0,
        origin_y=0.0,
    )
    assert set(placement.edge_waypoints) == {("a", "c")}  # only the edge that spans two ranks
    lane = placement.edge_waypoints[("a", "c")]
    assert len(lane) == 1
    assert lane[0][0] == pytest.approx(110.0)  # the middle of rank 1's band, not the gap beside it
    assert lane[0][1] > placement.positions["b"][1] + 40.0  # clear of the node it passes


def test_a_longer_span_gets_a_whole_chain_of_them() -> None:
    placement = layout_layered(
        _nodes("a", "b", "c", "d"),
        [("a", "b"), ("b", "c"), ("c", "d"), ("a", "d")],
        spacing_main=50.0,
        spacing_cross=10.0,
        origin_x=0.0,
        origin_y=0.0,
    )
    lane = placement.edge_waypoints[("a", "d")]
    assert len(lane) == 2  # one per rank the edge skips, in rank order
    assert [at[0] for at in lane] == [pytest.approx(110.0), pytest.approx(200.0)]


def test_a_span_of_one_reserves_no_lane_at_all() -> None:
    placement = layout_layered(
        _nodes("a", "b", "c", "d"),
        [("a", "b"), ("a", "c"), ("b", "d"), ("c", "d")],
        spacing_main=50.0,
        spacing_cross=10.0,
        origin_x=0.0,
        origin_y=0.0,
    )
    assert placement.edge_waypoints == {}


def test_a_lane_reads_source_to_target_even_when_the_edge_was_reversed() -> None:
    # c→a closes a cycle, so it is reversed to be laid out; its lane must still come back the
    # way the CALLER wrote the edge, or the router would thread it backwards.
    placement = layout_layered(
        _nodes("a", "b", "c"),
        [("a", "b"), ("b", "c"), ("c", "a")],
        spacing_main=50.0,
        spacing_cross=10.0,
        origin_x=0.0,
        origin_y=0.0,
    )
    assert placement.cycles_broken == 1
    lane = placement.edge_waypoints[("c", "a")]
    assert len(lane) == 1  # a→c spans two ranks once c→a has been turned round
    forward = layout_layered(
        _nodes("a", "b", "c"),
        [("a", "b"), ("b", "c"), ("a", "c")],
        spacing_main=50.0,
        spacing_cross=10.0,
        origin_x=0.0,
        origin_y=0.0,
    )
    assert lane == forward.edge_waypoints[("a", "c")]


def test_a_two_rank_chain_gets_reversed_end_to_end() -> None:
    # Two dummies, so reversal is observable: the lane must run c→a, i.e. right to left.
    placement = layout_layered(
        _nodes("a", "b", "c", "d"),
        [("a", "b"), ("b", "c"), ("c", "d"), ("d", "a")],
        spacing_main=50.0,
        spacing_cross=10.0,
        origin_x=0.0,
        origin_y=0.0,
    )
    lane = placement.edge_waypoints[("d", "a")]
    assert [at[0] for at in lane] == [pytest.approx(200.0), pytest.approx(110.0)]


def test_dummies_take_part_in_the_ordering_sweeps() -> None:
    """The lane for a→e must be pulled ABOVE m, which only a barycenter sweep can decide.

    a leaves from above c, and m hangs off c — so the lane a→e belongs on a's side of m, and the
    picture only reads that way if the dummy standing in for a→e is sorted with the rest of its
    rank. A dummy that merely reserved space at the end of the rank would put the lane under m
    and cross it against c→m→e; crossing minimisation cannot see the long edge until it is a
    chain of short ones, which is the whole reason for the dummy.
    """
    placement = layout_layered(
        _nodes("a", "c", "m", "e"),
        [("c", "m"), ("m", "e"), ("a", "e")],
        spacing_main=50.0,
        spacing_cross=10.0,
        origin_x=0.0,
        origin_y=0.0,
    )
    assert placement.positions["a"][1] < placement.positions["c"][1]  # a above c in rank 0
    lane = placement.edge_waypoints[("a", "e")]
    assert len(lane) == 1
    assert lane[0][1] < placement.positions["m"][1]  # and its lane above m, the way a runs


# --- 3c. the priority coordinate pass ----------------------------------------
#
# The disease these were written against: the greedy cross-axis pass derived every position from
# the one beside it (`max(wanted, floor)`, which could only ever push one way), so a rank was
# dragged across by its anchors and never corrected back — and the drag COMPOUNDED, rank by rank,
# into a drawing that read as a diagonal staircase. Each of these was checked against the old pass
# by stashing this change: the numbers in the comments are what it produced.

_LANE_PITCH = 8.0  # _DUMMY_EXTENT: how straight "straight" has to be to count


def _staircase(depth: int, width: int = 3) -> tuple[list[LayoutNode], list[tuple[str, str]]]:
    """``depth`` ranks of ``width`` nodes, wired so each rank's first node hangs off the last one.

    The shape that made the old pass drift: rank r+1's first node is anchored at the BOTTOM of
    rank r, so the greedy floor pushed the rest of rank r+1 past it, which pushed rank r+2 further
    still. One rank of drag is a wonky drawing; five ranks of it is a staircase.
    """
    letters = "abcdefgh"[:depth]
    nodes = [LayoutNode(id=f"{rank}{i}", w=80.0, h=40.0) for rank in letters for i in range(width)]
    edges = [
        (f"{rank}{width - 1}", f"{after}0")
        for rank, after in zip(letters, letters[1:], strict=False)
    ]
    edges += [
        (f"{rank}{i}", f"{after}{i}")
        for rank, after in zip(letters, letters[1:], strict=False)
        for i in range(1, width)
    ]
    return nodes, edges


def _spreads(
    placement: dict[str, Point], nodes: Sequence[LayoutNode], gap: float
) -> tuple[list[tuple[float, float]], float]:
    """Per-rank ``(spread, natural width)`` and the whole drawing's cross spread.

    A rank's NATURAL width is what it would occupy packed tight — its extents plus one gap
    between each pair — which is the only honest yardstick for "did this rank spread out".
    """
    ranks: dict[float, list[LayoutNode]] = {}
    for node in nodes:
        ranks.setdefault(placement[node.id][0], []).append(node)
    per_rank = [
        (
            max(placement[node.id][1] + node.h for node in members)
            - min(placement[node.id][1] for node in members),
            sum(node.h for node in members) + gap * (len(members) - 1),
        )
        for members in ranks.values()
    ]
    total = max(placement[node.id][1] + node.h for node in nodes) - min(
        placement[node.id][1] for node in nodes
    )
    return per_rank, total


def test_the_priority_pass_holds_a_rank_to_its_own_width_however_deep_the_graph() -> None:
    """The staircase regression, stated as the two bounds the greedy pass blew through.

    At four ranks of three the old pass drew rank 3 at 1.76x its natural width and spread the
    drawing over 360 units where the widest rank wanted 168 — both over. It got worse with depth
    (2.14x and 424u at five ranks) because the drag compounded. The priority pass holds 1.38x and
    296u at EVERY depth, which is the point: nothing accumulates, so depth costs nothing.
    """
    spreads = []
    for depth in (3, 4, 5):
        nodes, edges = _staircase(depth)
        placement = layout_layered(
            nodes, edges, spacing_main=90.0, spacing_cross=24.0, origin_x=0.0, origin_y=0.0
        )
        per_rank, total = _spreads(placement.positions, nodes, 24.0)
        for spread, natural in per_rank:
            assert spread <= natural * 1.5
        assert total <= 2 * max(natural for _spread, natural in per_rank)
        spreads.append(total)
    assert spreads[0] == spreads[1] == spreads[2]  # depth buys no drift at all


def test_a_chain_stays_straight_through_the_ranks_it_shares_with_distractors() -> None:
    """The priority method's signature result: the busy path is the one that gets to be straight.

    a→b→c→d, with a fan node ahead of each of b and c in its rank and in document order. The old
    pass placed each rank's first node on its anchor and floored the chain node below it, so the
    chain stepped down 64 units a rank (centers 20, 84, 148, 148). b and c are the most connected
    nodes in their ranks, so the priority pass seats them on their median first and shoves the fan
    nodes out of the way instead.

    The last rank has no distractor on purpose: d and a fan node there would both have degree one,
    and which of two equals wins is a question about document order, not about priority.
    """
    # Document order puts the distractors first, which is what made the old pass place them first.
    nodes = _nodes("a", "x1", "x2", "b", "c", "d", w=80.0, h=40.0)
    placement = layout_layered(
        nodes,
        [("a", "b"), ("b", "c"), ("c", "d"), ("a", "x1"), ("b", "x2")],
        spacing_main=90.0,
        spacing_cross=24.0,
        origin_x=0.0,
        origin_y=0.0,
    )
    centers = [placement.positions[node][1] + 20.0 for node in ("a", "b", "c", "d")]
    assert max(centers) - min(centers) <= _LANE_PITCH


def test_the_same_graph_lays_out_the_same_way_every_time() -> None:
    nodes, edges = _staircase(4)
    first = layout_layered(
        nodes, edges, spacing_main=90.0, spacing_cross=24.0, origin_x=0.0, origin_y=0.0
    )
    again = layout_layered(
        nodes, edges, spacing_main=90.0, spacing_cross=24.0, origin_x=0.0, origin_y=0.0
    )
    assert first.positions == again.positions


# --- 3d. pinned nodes in the packer ------------------------------------------


def _hub(pinned: dict[str, Point] | None) -> dict[str, Point]:
    """A rank holding a low-degree node and, after it, the hub that wants to stand where it is."""
    nodes = _nodes("top", "quiet", "hub", "t1", "t2", w=80.0, h=40.0)
    return layout_layered(
        nodes,
        [("top", "quiet"), ("top", "hub"), ("hub", "t1"), ("hub", "t2")],
        spacing_main=90.0,
        spacing_cross=24.0,
        origin_x=0.0,
        origin_y=0.0,
        pinned=pinned,
    ).positions


def test_a_sweep_shoves_a_low_priority_neighbour_aside() -> None:
    """The control for the pin test below: the hub outranks the quiet node and pushes through it.

    Three edges against one, so the hub is placed first and the quiet node ahead of it in the rank
    gives way — which the hub landing exactly level with ``top``, the median it asked for, records.
    """
    loose = _hub(None)
    assert loose["hub"][1] == loose["top"][1]


def test_a_pinned_node_is_a_wall_that_no_sweep_can_move() -> None:
    """The same graph with the quiet node pinned: the hub's degree buys it nothing at all now."""
    held = _hub({"quiet": (170.0, 0.0)})
    assert held["quiet"] == (170.0, 0.0)  # exactly where it was pinned, both coordinates
    assert held["hub"][1] != held["top"][1]  # clamped short of its median, not shoved through
    assert held["hub"][1] - held["quiet"][1] >= 64.0  # …and the node gap held (40 + 24)


def test_pinning_anchors_the_frame_so_nothing_is_normalized_onto_the_origin() -> None:
    """The documented rule, asserted: one pin turns the whole scope's normalization off.

    Unpinned, the drawing is slid so its topmost node sits on the cross origin. Pinned, it is not
    slid at all — the pin is an absolute coordinate, and a shifted pin is not a pin — so other
    nodes are free to sit above the origin.
    """
    loose = _hub(None)
    assert min(at[1] for at in loose.values()) == 0.0  # normalized onto origin_y
    held = _hub({"quiet": (170.0, 200.0)})
    assert held["quiet"] == (170.0, 200.0)
    assert min(at[1] for at in held.values()) != 0.0  # the frame belongs to the pin now


def test_an_empty_set_lays_out_to_nothing() -> None:
    for placement in (
        layout_grid([], spacing_main=1.0, spacing_cross=1.0),
        layout_tree([], [], spacing_main=1.0, spacing_cross=1.0),
        layout_layered([], [], spacing_main=1.0, spacing_cross=1.0),
    ):
        assert placement.positions == {}
        assert placement.ranks == 0


# --- 4. the pass over a real document ----------------------------------------


def _diamond(doc: Document, parent: str | None = None) -> list[str]:
    ids = [
        ops.add_diagram_node(
            doc, kind="service", label=name, width=80, height=40, parent=parent
        ).ref.id
        for name in "ABCD"
    ]
    for source, target in ((0, 1), (0, 2), (1, 3), (2, 3)):
        ops.add_diagram_edge(doc, source=ids[source], target=ids[target])
    return ids


def _corner(doc: Document, ids: Sequence[str]) -> Point:
    """The top-left the laid-out set actually starts at — what an origin is a statement about."""
    boxes = [box for box in (_box_of(doc, node_id) for node_id in ids) if box is not None]
    return (min(box.x for box in boxes), min(box.y for box in boxes))


def test_a_layout_pass_places_every_node_and_reports_what_it_did() -> None:
    doc = _doc()
    ids = _diamond(doc)
    result = ops.layout_diagram(doc)
    assert result.nodes_placed == 4
    assert result.ranks == 3
    assert result.cycles_broken == 0
    assert result.edges_rerouted == 4
    boxes = [_box_of(doc, node_id) for node_id in ids]
    assert all(box is not None for box in boxes)
    # A, then B and C together, then D — stated as the invariant it is, not as four coordinates.
    assert ranks_advance([(box.x, box.y) for box in boxes if box is not None], "LR")
    assert no_box_overlap(doc)
    assert min_gap(doc) >= 24.0 - 1e-6  # --gap-node, the cross-axis spacing it laid out with
    assert edges_avoid_boxes(doc) == []


def test_a_layout_pass_uses_the_theme_gaps_when_none_are_given() -> None:
    doc = _doc()
    a = ops.add_diagram_node(doc, kind="service", label="A", width=80, height=40)
    b = ops.add_diagram_node(doc, kind="service", label="B", width=80, height=40)
    ops.add_diagram_edge(doc, source=a.ref.id, target=b.ref.id)
    ops.layout_diagram(doc)
    first, second = _box_of(doc, a.ref.id), _box_of(doc, b.ref.id)
    assert first is not None and second is not None
    assert _corner(doc, [a.ref.id, b.ref.id]) == (20.0, 20.0)  # the default origin
    assert second.x - (first.x + first.w) == 90.0  # --gap-layer


def test_explicit_spacing_overrides_the_theme() -> None:
    doc = _doc()
    ids = _diamond(doc)
    ops.layout_diagram(
        doc, algorithm="grid", spacing_main=100, spacing_cross=60, origin_x=0, origin_y=0, columns=2
    )
    boxes = [_box_of(doc, node_id) for node_id in ids]
    assert [None if box is None else (box.x, box.y) for box in boxes] == [
        (0.0, 0.0),
        (180.0, 0.0),
        (0.0, 100.0),
        (180.0, 100.0),
    ]


def test_a_layout_pass_reroutes_the_edges_it_moved_the_nodes_of() -> None:
    doc = _doc()
    ids = _diamond(doc)
    edge = next(child for child in doc.svg if ids[0] in str(child.get("data-diagram-edge") or ""))
    before = str(edge[0].get("d"))
    ops.layout_diagram(doc)
    assert str(edge[0].get("d")) != before


def test_a_layout_pass_refits_the_containers_around_what_it_moved() -> None:
    doc = _doc()
    ids = _diamond(doc)
    container = ops.add_diagram_container(doc, members=[ids[1], ids[2]], label="middle")
    before = tuple(str(doc.resolve(container.ref.id)[0].get(key)) for key in ("x", "y"))
    result = ops.layout_diagram(doc)
    assert result.containers_refit == 1
    after = tuple(str(doc.resolve(container.ref.id)[0].get(key)) for key in ("x", "y"))
    assert after != before
    box = _box_of(doc, container.ref.id)
    middles = [_box_of(doc, node_id) for node_id in (ids[1], ids[2])]
    assert box is not None
    for member in middles:
        assert member is not None
        assert box.x <= member.x and box.x + box.w >= member.x + member.w


def test_scope_limits_the_layout_to_one_parent_s_direct_children() -> None:
    doc = _doc()
    outside = ops.add_diagram_node(
        doc, kind="service", label="out", x=500, y=500, width=80, height=40
    )
    group = ops.create_group(doc, name="col")
    inside = _diamond(doc, parent=group.id)
    result = ops.layout_diagram(doc, scope=group.id)
    assert result.nodes_placed == 4
    left_alone = _box_of(doc, outside.ref.id)
    assert left_alone is not None and (left_alone.x, left_alone.y) == (500.0, 500.0)
    assert _corner(doc, inside) == (20.0, 20.0)  # the scope's own set starts at the origin


def _chain(doc: Document, count: int) -> list[str]:
    ids = [
        ops.add_diagram_node(doc, kind="service", label=name, width=80, height=40).ref.id
        for name in "ABCDE"[:count]
    ]
    for source, target in zip(ids, ids[1:], strict=False):
        ops.add_diagram_edge(doc, source=source, target=target)
    return ids


def test_the_shapes_a_layered_layout_draws_keep_their_edges_out_of_the_boxes() -> None:
    for build in (_diamond, lambda doc: _chain(doc, 3)):
        doc = _doc()
        build(doc)
        ops.layout_diagram(doc)
        assert edges_avoid_boxes(doc) == []
        assert no_box_overlap(doc)


def _gate(doc: Document) -> tuple[str, str]:
    """A four-rank fan whose long edge CANNOT be drawn straight: A ⇉ B ⇉ C ⇉ D, plus A→D.

    A hangs off two middles and D off two more, so the median coordinate pass parks both of them
    level with a middle node rather than off to one side — which is exactly what makes the direct
    A→D route run through the two boxes between them. (A plain chain used to do this too. It no
    longer does: the priority pass offsets a chain's two ends TOWARD their long edge's lane, and
    the direct route then sneaks past the boxes by a couple of units. Better drawing, worse test
    data — hence the fan, where the other edges hold both ends in the band.)
    """
    ends = [
        ops.add_diagram_node(doc, kind="service", label=name, width=80, height=40).ref.id
        for name in ("A", "D")
    ]
    for lane in range(2):
        middles = [
            ops.add_diagram_node(
                doc, kind="service", label=f"{name}{lane}", width=80, height=40
            ).ref.id
            for name in ("B", "C")
        ]
        ops.add_diagram_edge(doc, source=ends[0], target=middles[0])
        ops.add_diagram_edge(doc, source=middles[0], target=middles[1])
        ops.add_diagram_edge(doc, source=middles[1], target=ends[1])
    ops.add_diagram_edge(doc, source=ends[0], target=ends[1])
    return (ends[0], ends[1])


def test_a_lane_keeps_a_rank_spanning_edge_clear_of_what_it_passes() -> None:
    """A→D over four ranks: the one case a direct route cannot draw without a collision.

    The second half is the same document routed WITHOUT lanes, which is what a plain reflow does —
    it is asserted to fail, both to prove the test data really is a collision case and to pin the
    documented semantic that lanes are layout scaffolding rather than stored geometry.
    """
    doc = _doc()
    _gate(doc)
    ops.layout_diagram(doc)
    assert edges_avoid_boxes(doc) == []

    ops.reflow(doc)  # no lanes: every route is re-derived straight from the two boxes
    assert edges_avoid_boxes(doc) != []


def test_pulling_shared_corridors_apart_never_walks_a_route_into_a_box() -> None:
    """The acceptance invariant for corridor separation: it may double a line, never cross a box.

    A chain with three rank-spanning edges over it gives the router plenty of shared lane to fan
    out, and every box in the middle of the chain is something an over-eager offset could push a
    run into.
    """
    doc = _doc()
    ids = _chain(doc, 5)
    ops.add_diagram_edge(doc, source=ids[0], target=ids[3])
    ops.add_diagram_edge(doc, source=ids[1], target=ids[4])
    ops.add_diagram_edge(doc, source=ids[0], target=ids[4])
    ops.layout_diagram(doc)
    assert edges_avoid_boxes(doc) == []
    assert no_box_overlap(doc)


def test_a_pinned_route_wins_over_the_lane_the_layout_would_have_used() -> None:
    doc = _doc()
    ids = _chain(doc, 4)
    long_edge = ops.add_diagram_edge(doc, source=ids[0], target=ids[3], waypoints=[(300.0, 300.0)])
    ops.layout_diagram(doc)
    d = str(doc.resolve(long_edge.ref.id)[0].get("d"))
    # It still goes where it was told, not down the lane the layout reserved for it (which runs
    # just under the rank of boxes, hundreds of units above y=300).
    assert passes_through(path_points(d), (300.0, 300.0), TOL)


def test_a_layout_pass_leaves_a_pinned_node_exactly_where_it_was() -> None:
    """The promise, byte for byte: a pinned node's coordinates come out of layout unchanged.

    Not "close to": the pass skips the translate entirely for a pinned node, so there is no
    rounding to be within.
    """
    doc = _doc()
    ids = _diamond(doc)
    ops.edit_diagram_node(doc, ids[2], pinned=True)
    before = _box_of(doc, ids[2])
    assert before is not None
    ops.layout_diagram(doc)
    after = _box_of(doc, ids[2])
    assert after is not None
    assert (after.x, after.y) == (before.x, before.y)


def test_the_rest_of_the_drawing_packs_around_a_pinned_node() -> None:
    """A pin is a wall, not a hole: everything else still lays out, and still clears it.

    Laid out first, then one box dragged down its rank and pinned there, which is the workflow the
    flag exists for — the pin stays inside its own rank's band, so the only thing that has to give
    is the cross-axis packing of the node beside it.
    """
    doc = _doc()
    ids = _diamond(doc)
    ops.layout_diagram(doc)
    ops.translate_node(doc, ids[1], 0.0, 220.0)  # well below where the layout put it
    ops.edit_diagram_node(doc, ids[1], pinned=True)
    pinned = _box_of(doc, ids[1])
    assert pinned is not None
    result = ops.layout_diagram(doc)

    assert result.nodes_placed == 4  # the pinned node is still one of the nodes it laid out
    held = _box_of(doc, ids[1])
    assert held is not None and (held.x, held.y) == (pinned.x, pinned.y)
    assert no_box_overlap(doc)
    assert min_gap(doc) >= 24.0 - 1e-6  # the node gap holds against the wall too
    assert edges_avoid_boxes(doc) == []
    # And the whole drawing came down to meet the pin rather than the pin being pulled back up to
    # the origin: with anything pinned, this pass normalizes nothing.
    assert _corner(doc, ids)[1] > 20.0


def test_a_pinned_node_keeps_its_main_axis_coordinate_as_well_as_its_cross() -> None:
    """Both coordinates, which is what makes a pin a placement override rather than a nudge.

    The node's RANK is still computed — it is rank 1 of 3 either way — so pinning it back past
    rank 0 is exactly the doubling-back geometry the docstrings warn about, drawn as asked.
    """
    doc = _doc()
    ids = _diamond(doc)
    ops.edit_diagram_node(doc, ids[1], pinned=True)
    ops.translate_node(doc, ids[1], -60.0, 0.0)  # left of where any rank of this diagram starts
    before = _box_of(doc, ids[1])
    assert before is not None
    ops.layout_diagram(doc)
    after, first = _box_of(doc, ids[1]), _box_of(doc, ids[0])
    assert after is not None and first is not None
    assert (after.x, after.y) == (before.x, before.y)
    assert after.x < first.x  # rank 1 drawn to the LEFT of rank 0: the caller's geometry, kept


def test_a_document_with_no_diagram_nodes_lays_out_to_nothing() -> None:
    doc = _doc()
    ops.add_rect(doc, x=0, y=0, width=10, height=10)
    assert ops.layout_diagram(doc).nodes_placed == 0


# --- 5. render ---------------------------------------------------------------


def test_a_laid_out_diamond_renders_with_its_container_behind_it() -> None:
    renderer = get_renderer()
    if not renderer.available():
        pytest.skip("no resvg backend available")
    from PIL import Image

    doc = _doc()
    ids = _diamond(doc)
    container = ops.add_diagram_container(doc, members=[ids[1], ids[2]], kind="zone")
    ops.layout_diagram(doc, origin_x=40, origin_y=80)

    result = renderer.render(RenderRequest(svg=export_svg(doc)))
    image = Image.open(io.BytesIO(result.png)).convert("RGBA")

    box = _box_of(doc, ids[1])
    assert box is not None
    on_member = image.getpixel((int(box.x + box.w / 2), int(box.y + 4)))
    assert isinstance(on_member, tuple)
    assert on_member[:3] == (236, 238, 241)  # the node paints over the zone behind it

    zone = _box_of(doc, container.ref.id)
    assert zone is not None
    corner = image.getpixel((int(zone.x + 3), int(zone.y + 3)))
    assert isinstance(corner, tuple)
    assert 0 < corner[3] <= 24  # the zone's wash, with nothing of its own drawn over it


def test_a_fitted_container_never_overflows_the_layout_origin() -> None:
    doc = _doc()
    ids = _diamond(doc)
    ops.add_diagram_container(doc, members=[ids[0], ids[1]], label="zone", kind="zone")
    ops.layout_diagram(doc, origin_x=20, origin_y=20)
    for group, _spec in _container_groups(doc):
        box = _bbox_xywh(group)
        assert box is not None
        # The padded, label-headroomed box must sit inside the origin, not just the nodes.
        assert box[0] >= 20 - 1e-6
        assert box[1] >= 20 - 1e-6


def test_lanes_charge_the_lane_pitch_not_the_node_gap() -> None:
    # A rank threading many lanes must not blow its cross extent out to a multiple of the
    # drawing: dummies are lines, not boxes, so the gap on either side of one is the small
    # lane pitch. Observed before the fix: 3752u wide where 792u was right.
    nodes = _nodes("a", "b", "c", "d", "e", "f", w=60.0, h=30.0)
    # a fans out to everything two ranks down: four rank-spanning edges thread rank 1.
    edges = [
        ("a", "b"),
        ("b", "c"),
        ("a", "d"),
        ("a", "e"),
        ("a", "f"),
        ("b", "d"),
        ("b", "e"),
        ("b", "f"),
    ]
    placement = layout_layered(
        nodes,
        edges,
        {},
        direction="TB",
        spacing_main=90.0,
        spacing_cross=40.0,
        origin_x=0.0,
        origin_y=0.0,
    )
    xs = [at[0] for at in placement.positions.values()]
    spread = max(xs) - min(xs)
    # 4 real nodes in the widest rank at 60u + node gaps + 4 thin lanes: generous ceiling of
    # 600u; the node-gap-per-lane bug put this over 800.
    assert spread < 600.0
