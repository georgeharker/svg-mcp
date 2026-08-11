"""Diagram layout: the three placement algorithms as pure data, then the pass that applies one."""

from __future__ import annotations

import io

import pytest

from svg_mcp import ops
from svg_mcp.model import Document
from svg_mcp.ops.diagram import _box_of
from svg_mcp.ops.diagram_layout import LayoutNode, layout_grid, layout_layered, layout_tree
from svg_mcp.render import get_renderer
from svg_mcp.render.base import RenderRequest
from svg_mcp.serialize import export_svg
from svg_mcp.session import DocumentStore

Point = tuple[float, float]


def _doc() -> Document:
    return DocumentStore().create(900, 600)[1]


def _nodes(*ids: str, w: float = 40.0, h: float = 40.0) -> list[LayoutNode]:
    return [LayoutNode(id=node_id, w=w, h=h) for node_id in ids]


def _by_cross(placement: dict[str, Point]) -> list[str]:
    """The node ids in cross-axis (y, for LR) order — the ordering a rank actually drew."""
    return [node_id for node_id, _at in sorted(placement.items(), key=lambda item: item[1][1])]


# --- 1. grid -----------------------------------------------------------------


def test_a_grid_defaults_to_a_square_of_columns_and_fills_row_major() -> None:
    nodes = [LayoutNode(id="wide", w=80.0, h=30.0), *_nodes("b", "c", "d", "e", w=40.0, h=20.0)]
    placement = layout_grid(
        nodes, spacing_main=10.0, spacing_cross=5.0, origin_x=0.0, origin_y=0.0
    )
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
    assert _by_cross(loose.positions)[1:] == ["a", "b", "c"]  # document order, b in between
    assert _by_cross(grouped.positions)[1:] == ["a", "c", "b"]  # the container's two, adjacent
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
    xs = [box.x for box in boxes if box is not None]
    assert xs[0] < xs[1] == xs[2] < xs[3]  # A, then B and C together, then D


def test_a_layout_pass_uses_the_theme_gaps_when_none_are_given() -> None:
    doc = _doc()
    a = ops.add_diagram_node(doc, kind="service", label="A", width=80, height=40)
    b = ops.add_diagram_node(doc, kind="service", label="B", width=80, height=40)
    ops.add_diagram_edge(doc, source=a.ref.id, target=b.ref.id)
    ops.layout_diagram(doc)
    first, second = _box_of(doc, a.ref.id), _box_of(doc, b.ref.id)
    assert first is not None and second is not None
    assert (first.x, first.y) == (20.0, 20.0)  # the default origin
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
    edge = next(
        child
        for child in doc.svg
        if ids[0] in str(child.get("data-diagram-edge") or "")
    )
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
    outside = ops.add_diagram_node(doc, kind="service", label="out", x=500, y=500, width=80,
                                   height=40)
    group = ops.create_group(doc, name="col")
    inside = _diamond(doc, parent=group.id)
    result = ops.layout_diagram(doc, scope=group.id)
    assert result.nodes_placed == 4
    left_alone = _box_of(doc, outside.ref.id)
    assert left_alone is not None and (left_alone.x, left_alone.y) == (500.0, 500.0)
    moved = _box_of(doc, inside[0])
    assert moved is not None and (moved.x, moved.y) == (20.0, 20.0)


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
