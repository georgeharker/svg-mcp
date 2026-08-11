"""Diagram facades: the pure routing engine, then the nodes/edges/reflow built on top of it."""

from __future__ import annotations

import io
import re
from pathlib import Path

import pytest

from svg_mcp import ops
from svg_mcp.model import Document
from svg_mcp.model.errors import InvalidArgument
from svg_mcp.ops.diagram import (
    Box,
    _shape_for,
    anchor_point,
    auto_side,
    measure_label,
    orthogonal_waypoints,
    read_edge_spec,
    read_node_spec,
    resolve_sides,
    rounded_path,
    route_edge,
    spread_fractions,
)
from svg_mcp.ops.themes import ServingTheme, serving_theme
from svg_mcp.query import get_params
from svg_mcp.render import get_renderer
from svg_mcp.render.base import RenderRequest
from svg_mcp.serialize import export_svg
from svg_mcp.session import DocumentStore
from svg_mcp.theme import load_theme as read_theme

FIXTURES = Path(__file__).parent / "fixtures" / "themes"
TOL = 0.5

Point = tuple[float, float]


def _doc() -> Document:
    return DocumentStore().create(600, 400)[1]


def _classes(doc: Document, node_id: str) -> list[str]:
    return str(doc.resolve(node_id).get("class") or "").split()


def _segments(d: str) -> list[tuple[str, list[Point]]]:
    """Parse absolute path data into (command, points) — enough for M/L/Q/C route assertions."""
    out: list[tuple[str, list[Point]]] = []
    for command, body in re.findall(r"([MLQC])([^MLQC]*)", d):
        numbers = [float(n) for n in re.findall(r"-?\d+(?:\.\d+)?", body)]
        out.append((command, list(zip(numbers[0::2], numbers[1::2], strict=True))))
    return out


def _corners(d: str) -> list[Point]:
    """The vertices a rounded route turns at — a Q's control point IS the original corner."""
    return [points[0] for command, points in _segments(d) if command == "Q"]


def _close(a: Point, b: Point) -> bool:
    return abs(a[0] - b[0]) <= TOL and abs(a[1] - b[1]) <= TOL


# --- 1. the routing engine, with no document in sight -----------------------


def test_auto_side_picks_the_dominant_axis_in_every_quadrant() -> None:
    home = Box(0, 0, 20, 20)
    assert auto_side(home, Box(100, 5, 20, 20)) == "E"
    assert auto_side(home, Box(-100, 5, 20, 20)) == "W"
    assert auto_side(home, Box(5, 100, 20, 20)) == "S"
    assert auto_side(home, Box(5, -100, 20, 20)) == "N"


def test_a_diagonal_tie_goes_to_the_horizontal_axis() -> None:
    assert auto_side(Box(0, 0, 10, 10), Box(50, 50, 10, 10)) == "E"


def test_target_side_faces_the_source() -> None:
    source, target = Box(0, 0, 40, 40), Box(200, 10, 40, 40)
    assert resolve_sides(source, target, "auto", "auto") == ("E", "W")
    assert resolve_sides(target, source, "auto", "auto") == ("W", "E")


def test_an_explicit_anchor_overrides_the_geometry() -> None:
    source, target = Box(0, 0, 40, 40), Box(200, 0, 40, 40)
    assert resolve_sides(source, target, "S", "auto") == ("S", "W")
    assert resolve_sides(source, target, "auto", "N") == ("E", "N")


def test_anchor_point_runs_left_to_right_and_top_to_bottom() -> None:
    box = Box(10, 20, 100, 50)
    assert anchor_point(box, "N", 0.25) == (35.0, 20.0)
    assert anchor_point(box, "S", 0.75) == (85.0, 70.0)
    assert anchor_point(box, "E", 0.5) == (110.0, 45.0)
    assert anchor_point(box, "W", 0.5) == (10.0, 45.0)


def test_a_lone_edge_takes_the_middle_of_its_face() -> None:
    assert spread_fractions((0.0, 0.0), "E", [(100.0, 40.0)]) == [0.5]


def test_three_edges_on_one_face_spread_in_port_order() -> None:
    # Fed lowest-first; the E face must still hand out ports top-to-bottom.
    fractions = spread_fractions((0.0, 0.0), "E", [(100.0, 80.0), (100.0, -80.0), (100.0, 0.0)])
    assert fractions == [0.75, 0.25, 0.5]


def test_the_bottom_face_spreads_left_to_right() -> None:
    # A global atan2 would order these right-to-left, which is what makes edges cross.
    fractions = spread_fractions((0.0, 0.0), "S", [(80.0, 100.0), (-80.0, 100.0)])
    assert fractions == [pytest.approx(2 / 3), pytest.approx(1 / 3)]


def test_opposite_faces_make_a_z_route_split_on_the_free_axis() -> None:
    points = orthogonal_waypoints((100.0, 50.0), "E", (300.0, 150.0), "W", 12.0)
    assert points[0] == (100.0, 50.0)
    assert points[1] == (112.0, 50.0)
    assert points[2] == (200.0, 50.0)  # midway between the two stub ends
    assert points[3] == (200.0, 150.0)
    assert points[4] == (288.0, 150.0)
    assert points[5] == (300.0, 150.0)


def test_aligned_opposite_faces_make_one_straight_run() -> None:
    points = orthogonal_waypoints((100.0, 50.0), "E", (300.0, 50.3), "W", 12.0)
    assert [p[0] for p in points] == [100.0, 112.0, 288.0, 300.0]  # no jog inserted


def test_perpendicular_faces_turn_once() -> None:
    points = orthogonal_waypoints((100.0, 50.0), "E", (300.0, 200.0), "N", 10.0)
    assert points == [
        (100.0, 50.0),
        (110.0, 50.0),
        (300.0, 50.0),  # the vertical stub's x, the horizontal stub's y
        (300.0, 190.0),
        (300.0, 200.0),
    ]


def test_the_same_face_twice_makes_a_u_route() -> None:
    # Both stubs push out to the further of the two (x = 112), then one run joins them.
    assert orthogonal_waypoints((100.0, 50.0), "E", (60.0, 200.0), "E", 12.0) == [
        (100.0, 50.0),
        (112.0, 50.0),
        (112.0, 200.0),
        (72.0, 200.0),
        (60.0, 200.0),
    ]


def test_a_west_u_route_extends_to_the_leftmost_stub() -> None:
    assert orthogonal_waypoints((100.0, 50.0), "W", (60.0, 200.0), "W", 12.0) == [
        (100.0, 50.0),
        (88.0, 50.0),
        (48.0, 50.0),
        (48.0, 200.0),
        (60.0, 200.0),
    ]


def test_corners_are_rounded_and_clamp_to_half_the_shorter_segment() -> None:
    generous = _segments(rounded_path([(0.0, 0.0), (100.0, 0.0), (100.0, 100.0)], 8.0))
    assert generous[1] == ("L", [(92.0, 0.0)])
    assert generous[2] == ("Q", [(100.0, 0.0), (100.0, 8.0)])
    # A 10-unit run cannot host an 8-unit radius: it is clamped to half the segment.
    tight = _segments(rounded_path([(0.0, 0.0), (10.0, 0.0), (10.0, 100.0)], 8.0))
    assert tight[1] == ("L", [(5.0, 0.0)])
    assert tight[2] == ("Q", [(10.0, 0.0), (10.0, 5.0)])


def test_a_straight_run_is_not_given_a_corner() -> None:
    assert _corners(rounded_path([(0.0, 0.0), (50.0, 0.0), (100.0, 0.0)], 8.0)) == []


def test_straight_and_spline_take_the_anchors_directly() -> None:
    a, b = (100.0, 50.0), (300.0, 150.0)
    straight = route_edge(a, "E", b, "W", route="straight", stub=12.0, radius=8.0)
    assert _segments(straight.d) == [("M", [a]), ("L", [b])]
    assert _close(straight.label_at, (200.0, 100.0))

    spline = route_edge(a, "E", b, "W", route="spline", stub=12.0, radius=8.0)
    reach = 0.4 * ((200.0**2 + 100.0**2) ** 0.5)
    curve = _segments(spline.d)[1]
    assert curve[0] == "C"
    assert _close(curve[1][0], (100.0 + reach, 50.0))
    assert _close(curve[1][1], (300.0 - reach, 150.0))
    assert _close(curve[1][2], b)


def test_an_orthogonal_label_sits_on_the_longest_segment() -> None:
    result = route_edge(
        (100.0, 50.0), "E", (300.0, 150.0), "W", route="orthogonal", stub=12.0, radius=8.0
    )
    assert _close(result.label_at, (200.0, 100.0))  # the vertical mid-run is the longest


# --- 2. nodes ----------------------------------------------------------------


def test_an_empty_label_still_gets_the_minimum_box() -> None:
    doc = _doc()
    placed = ops.add_diagram_node(doc, kind="service")
    assert (placed.w, placed.h) == (60.0, 36.0)


def test_a_long_label_widens_the_box_past_the_minimum() -> None:
    doc = _doc()
    short = ops.add_diagram_node(doc, kind="service", label="A")
    long = ops.add_diagram_node(doc, kind="service", label="A considerably longer caption")
    assert short.w == 60.0
    assert long.w > short.w
    text_w, _text_h = measure_label("A considerably longer caption", "sans-serif", 12.0)
    assert long.w == pytest.approx(text_w + 24.0, abs=TOL)


def test_a_decision_is_sized_for_its_inscribed_label() -> None:
    doc = _doc()
    box = ops.add_diagram_node(doc, kind="service", label="Is it valid?")
    diamond = ops.add_diagram_node(doc, kind="decision", label="Is it valid?")
    assert read_node_spec(doc.resolve(diamond.ref.id)) is not None
    assert diamond.w > box.w  # 1.6x the run plus padding
    assert diamond.h >= 48.0 and diamond.h > box.h


def test_explicit_size_wins_over_measurement() -> None:
    doc = _doc()
    placed = ops.add_diagram_node(doc, kind="service", label="x", width=200, height=90)
    assert (placed.w, placed.h) == (200, 90)
    spec = read_node_spec(doc.resolve(placed.ref.id))
    assert spec is not None and spec.auto is False


def test_nodes_auto_place_by_stacking_under_the_previous_one() -> None:
    doc = _doc()
    first = ops.add_diagram_node(doc, kind="service", label="one")
    second = ops.add_diagram_node(doc, kind="service", label="two")
    third = ops.add_diagram_node(doc, kind="service", label="three")
    assert (first.x, first.y) == (20.0, 20.0)
    assert second.x == 20.0 and second.y == pytest.approx(first.y + first.h + 24.0)
    assert third.y == pytest.approx(second.y + second.h + 24.0)


def test_stacking_is_per_parent() -> None:
    doc = _doc()
    ops.add_diagram_node(doc, kind="service", label="root one")
    group = ops.create_group(doc, name="col")
    nested = ops.add_diagram_node(doc, kind="service", label="nested", parent=group.id)
    assert (nested.x, nested.y) == (20.0, 20.0)


def test_a_themeless_document_takes_its_shape_from_the_bundled_default() -> None:
    doc = _doc()
    placed = ops.add_diagram_node(doc, kind="service", label="Auth")
    spec = read_node_spec(doc.resolve(placed.ref.id))
    assert spec is not None and spec.shape == "squircle"
    assert _classes(doc, placed.ref.id) == ["default-service"]
    assert str(doc.resolve(placed.ref.id).get("data-prim")) == "squircle"


def test_every_bundled_kind_draws_the_primitive_its_manifest_names() -> None:
    doc = _doc()
    drawn = {}
    for kind in ("service", "datastore", "queue", "external", "decision", "note"):
        placed = ops.add_diagram_node(doc, kind=kind, label=kind)
        spec = read_node_spec(doc.resolve(placed.ref.id))
        assert spec is not None
        drawn[kind] = spec.shape
    assert drawn == {
        "service": "squircle",
        "datastore": "rect",
        "queue": "pill",
        "external": "rect",
        "decision": "polygon",
        "note": "rect",
    }


def test_a_routed_theme_supplies_the_shape_and_the_padding() -> None:
    doc = _doc()
    ops.load_theme(doc, "atlas", search_paths=[FIXTURES])
    assert doc.theme_meta["atlas"].kinds == {"service": "pill", "gadget": "cylinder"}
    placed = ops.add_diagram_node(doc, kind="service", label="Auth")
    spec = read_node_spec(doc.resolve(placed.ref.id))
    assert spec is not None and spec.shape == "pill"  # atlas's [kinds], not the default's squircle
    assert _classes(doc, placed.ref.id) == ["atlas-service"]
    text_w, _h = measure_label("Auth", "sans-serif", 12.0)
    assert placed.w == pytest.approx(max(60.0, text_w + 40.0), abs=TOL)  # --pad-node: 20


def test_a_kind_the_serving_theme_does_not_map_falls_back_to_a_rect() -> None:
    house = read_theme("house", [FIXTURES])
    assert house.manifest.kinds["datastore"] == "cylinder"  # a shape we cannot draw
    serving = ServingTheme(name="house", kinds=house.manifest.kinds)
    assert _shape_for(serving, "datastore") == "rect"
    assert _shape_for(serving, "service") == "squircle"
    assert _shape_for(serving, "nothing-like-this") == "rect"


def test_serving_theme_prefers_the_routed_theme_over_the_default() -> None:
    doc = _doc()
    ops.load_theme(doc, "atlas", search_paths=[FIXTURES])
    assert serving_theme(doc, "service").name == "atlas"
    assert serving_theme(doc, "datastore").name == "default"  # nothing routed → the fallback


def test_an_unthemed_node_carries_no_classes_but_is_still_kind_shaped() -> None:
    doc = _doc()
    placed = ops.add_diagram_node(doc, kind="service", label="Auth", themed=False)
    other = ops.add_diagram_node(doc, kind="service", label="Bee", themed=False)
    assert _classes(doc, placed.ref.id) == []
    spec = read_node_spec(doc.resolve(placed.ref.id))
    assert spec is not None and spec.shape == "squircle"
    edge = ops.add_diagram_edge(doc, source=placed.ref.id, target=other.ref.id, themed=False)
    assert _classes(doc, edge.ref.id) == []
    assert edge.edges_rerouted == 1


def test_a_node_spec_round_trips_through_get_params() -> None:
    doc = _doc()
    placed = ops.add_diagram_node(doc, kind="queue", label="Events", width=120, height=40)
    result = get_params(doc, placed.ref.id)
    assert result["kind"] == "diagram_node"
    assert result["parametric"] is True
    assert result["params"] == {
        "kind": "queue",
        "label": "Events",
        "shape": "pill",
        "w": 120.0,
        "h": 40.0,
        "auto": False,
    }


# --- 3. edges ----------------------------------------------------------------


def _pair(doc: Document) -> tuple[str, str]:
    a = ops.add_diagram_node(doc, kind="service", label="A", x=40, y=40, width=80, height=40)
    b = ops.add_diagram_node(doc, kind="service", label="B", x=340, y=140, width=80, height=40)
    return a.ref.id, b.ref.id


def test_an_edge_wears_its_kind_class_and_shares_one_arrow_marker() -> None:
    doc = _doc()
    a, b = _pair(doc)
    first = ops.add_diagram_edge(doc, source=a, target=b, kind="control")
    second = ops.add_diagram_edge(doc, source=b, target=a, kind="dependency")
    assert _classes(doc, first.ref.id) == ["default-control"]
    assert _classes(doc, second.ref.id) == ["default-dependency"]
    svg = export_svg(doc)
    assert svg.count("<marker") == 1
    assert svg.count("marker-end:url(#diagram-arrow)") == 2


def test_an_edge_path_is_anchored_to_both_boxes() -> None:
    doc = _doc()
    a, b = _pair(doc)
    edge = ops.add_diagram_edge(doc, source=a, target=b)
    d = str(doc.resolve(edge.ref.id)[0].get("d"))
    start, end = _segments(d)[0][1][0], _segments(d)[-1][1][-1]
    assert _close(start, (120.0, 60.0))  # A's E face, midpoint
    assert _close(end, (340.0, 160.0))  # B's W face, midpoint


def test_an_edge_label_gets_a_canvas_coloured_halo() -> None:
    doc = _doc()
    a, b = _pair(doc)
    edge = ops.add_diagram_edge(doc, source=a, target=b, label="reads")
    text = doc.resolve(edge.ref.id)[1]
    style = {str(k): str(v) for k, v in text.style.items()}
    assert text.text == "reads"
    assert style["paint-order"] == "stroke"
    assert style["stroke"] == "#ffffff"
    assert style["stroke-width"] == "3"
    assert style["text-anchor"] == "middle"
    assert str(text.get("dy")) == "-4"


def test_three_edges_leaving_one_node_fan_out_across_its_face() -> None:
    doc = _doc()
    hub = ops.add_diagram_node(doc, kind="service", label="hub", x=40, y=100, width=80, height=90)
    ys = []
    for index, y in enumerate((20, 100, 220)):
        spoke = ops.add_diagram_node(
            doc, kind="service", label=f"s{index}", x=340, y=y, width=80, height=40
        )
        ops.add_diagram_edge(doc, source=hub.ref.id, target=spoke.ref.id)
    for group in doc.svg.iter():
        if read_edge_spec(group) is not None:
            ys.append(_segments(str(group[0].get("d")))[0][1][0][1])
    assert ys == [pytest.approx(122.5), pytest.approx(145.0), pytest.approx(167.5)]


def test_an_edge_spec_round_trips_through_get_params() -> None:
    doc = _doc()
    a, b = _pair(doc)
    edge = ops.add_diagram_edge(
        doc, source=a, target=b, kind="control", source_anchor="S", route="spline", label="hi"
    )
    result = get_params(doc, edge.ref.id)
    assert result["kind"] == "diagram_edge"
    assert result["params"] == {
        "source": a,
        "target": b,
        "kind": "control",
        "sa": "S",
        "ta": "auto",
        "route": "spline",
        "label": "hi",
    }


def test_an_edge_is_not_placed_inside_either_node() -> None:
    doc = _doc()
    a, b = _pair(doc)
    edge = ops.add_diagram_edge(doc, source=a, target=b)
    parent = doc.resolve(edge.ref.id).getparent()
    assert parent is doc.svg


# --- 4. reflow ---------------------------------------------------------------


def test_reflow_follows_a_node_that_moved() -> None:
    doc = _doc()
    a, b = _pair(doc)
    edge = ops.add_diagram_edge(doc, source=a, target=b)
    before = str(doc.resolve(edge.ref.id)[0].get("d"))
    ops.translate_node(doc, b, 0, 160)
    result = ops.reflow(doc)
    after = str(doc.resolve(edge.ref.id)[0].get("d"))
    assert result.edges_rerouted == 1 and result.skipped == []
    assert result.containers_refit == 0
    assert after != before
    start, end = _segments(after)[0][1][0], _segments(after)[-1][1][-1]
    assert _close(start, (120.0, 60.0))  # still on A's E face
    assert _close(end, (340.0, 320.0))  # and on B's W face, where B is now


def test_reflow_re_picks_the_side_when_a_node_moves_past_it() -> None:
    doc = _doc()
    a, b = _pair(doc)
    edge = ops.add_diagram_edge(doc, source=a, target=b)
    ops.translate_node(doc, b, -600, 0)  # B is now well to the LEFT of A
    ops.reflow(doc)
    start = _segments(str(doc.resolve(edge.ref.id)[0].get("d")))[0][1][0]
    assert _close(start, (40.0, 60.0))  # A's W face


def test_reflow_scope_limits_what_is_rewritten() -> None:
    doc = _doc()
    a, b = _pair(doc)
    c = ops.add_diagram_node(doc, kind="service", label="C", x=40, y=300, width=80, height=40)
    kept = ops.add_diagram_edge(doc, source=a, target=b)
    moved = ops.add_diagram_edge(doc, source=a, target=c.ref.id)
    ops.translate_node(doc, b, 0, 120)
    ops.translate_node(doc, c.ref.id, 120, 0)
    before_kept = str(doc.resolve(kept.ref.id)[0].get("d"))
    before_moved = str(doc.resolve(moved.ref.id)[0].get("d"))
    result = ops.reflow(doc, scope=[c.ref.id])
    assert result.edges_rerouted == 1
    assert str(doc.resolve(kept.ref.id)[0].get("d")) == before_kept
    assert str(doc.resolve(moved.ref.id)[0].get("d")) != before_moved


def test_reflow_reports_an_edge_whose_node_is_gone() -> None:
    doc = _doc()
    a, b = _pair(doc)
    edge = ops.add_diagram_edge(doc, source=a, target=b)
    before = str(doc.resolve(edge.ref.id)[0].get("d"))
    ops.delete_node(doc, b)
    result = ops.reflow(doc)
    assert result.edges_rerouted == 0
    assert result.skipped == [edge.ref.id]
    assert str(doc.resolve(edge.ref.id)[0].get("d")) == before  # left exactly as it was


def test_reflow_can_be_told_to_leave_edges_alone() -> None:
    doc = _doc()
    a, b = _pair(doc)
    edge = ops.add_diagram_edge(doc, source=a, target=b)
    before = str(doc.resolve(edge.ref.id)[0].get("d"))
    ops.translate_node(doc, b, 0, 200)
    result = ops.reflow(doc, edges=False)
    assert result.edges_rerouted == 0
    assert str(doc.resolve(edge.ref.id)[0].get("d")) == before


# --- 5. editing --------------------------------------------------------------


def test_editing_a_label_remeasures_an_auto_sized_node() -> None:
    doc = _doc()
    placed = ops.add_diagram_node(doc, kind="service", label="A")
    result = ops.edit_diagram_node(doc, placed.ref.id, label="A considerably longer caption")
    spec = read_node_spec(doc.resolve(placed.ref.id))
    assert result.remeasured is True
    assert spec is not None and spec.w > placed.w and spec.label.startswith("A considerably")
    body = doc.resolve(placed.ref.id)[0]
    assert float(body.bounding_box().width) == pytest.approx(spec.w, abs=TOL)
    text = doc.resolve(placed.ref.id)[1]
    assert float(str(text.get("x"))) == pytest.approx(20.0 + spec.w / 2.0, abs=TOL)


def test_editing_a_label_never_remeasures_an_explicitly_sized_node() -> None:
    doc = _doc()
    placed = ops.add_diagram_node(doc, kind="service", label="A", width=200, height=90)
    result = ops.edit_diagram_node(doc, placed.ref.id, label="something very much longer")
    spec = read_node_spec(doc.resolve(placed.ref.id))
    assert result.remeasured is False
    assert spec is not None and (spec.w, spec.h) == (200.0, 90.0)


def test_changing_a_kind_swaps_the_role_class_and_keeps_the_shape() -> None:
    doc = _doc()
    placed = ops.add_diagram_node(doc, kind="service", label="A")
    result = ops.edit_diagram_node(doc, placed.ref.id, kind="note")
    spec = read_node_spec(doc.resolve(placed.ref.id))
    assert result.shape_unchanged is True
    assert _classes(doc, placed.ref.id) == ["default-note"]
    assert spec is not None and spec.kind == "note" and spec.shape == "squircle"


def test_editing_an_edge_reroutes_it() -> None:
    doc = _doc()
    a, b = _pair(doc)
    edge = ops.add_diagram_edge(doc, source=a, target=b)
    before = str(doc.resolve(edge.ref.id)[0].get("d"))
    result = ops.edit_diagram_edge(doc, edge.ref.id, route="straight")
    after = str(doc.resolve(edge.ref.id)[0].get("d"))
    assert result.edges_rerouted == 1
    assert after != before
    assert [command for command, _points in _segments(after)] == ["M", "L"]


def test_editing_an_edge_anchor_moves_where_it_leaves_from() -> None:
    doc = _doc()
    a, b = _pair(doc)
    edge = ops.add_diagram_edge(doc, source=a, target=b)
    ops.edit_diagram_edge(doc, edge.ref.id, source_anchor="S", target_anchor="N")
    d = str(doc.resolve(edge.ref.id)[0].get("d"))
    start, end = _segments(d)[0][1][0], _segments(d)[-1][1][-1]
    assert _close(start, (80.0, 80.0))  # A's S face
    assert _close(end, (380.0, 140.0))  # B's N face


def test_editing_an_edge_kind_swaps_its_class() -> None:
    doc = _doc()
    a, b = _pair(doc)
    edge = ops.add_diagram_edge(doc, source=a, target=b, kind="data")
    ops.edit_diagram_edge(doc, edge.ref.id, kind="control", label="now labelled")
    spec = read_edge_spec(doc.resolve(edge.ref.id))
    assert _classes(doc, edge.ref.id) == ["default-control"]
    assert spec is not None and spec.kind == "control" and spec.label == "now labelled"


def test_editing_a_node_that_is_not_one_is_rejected() -> None:
    doc = _doc()
    rect = ops.add_rect(doc, x=0, y=0, width=10, height=10)
    with pytest.raises(InvalidArgument, match="not a diagram node"):
        ops.edit_diagram_node(doc, rect.id, label="nope")
    with pytest.raises(InvalidArgument, match="not a diagram edge"):
        ops.edit_diagram_edge(doc, rect.id, label="nope")


# --- 6. render ---------------------------------------------------------------


def test_a_themeless_diagram_renders_its_nodes_and_its_edge() -> None:
    renderer = get_renderer()
    if not renderer.available():
        pytest.skip("no resvg backend available")
    from PIL import Image

    doc = _doc()
    a = ops.add_diagram_node(doc, kind="service", label="A", x=40, y=40, width=100, height=60)
    b = ops.add_diagram_node(doc, kind="service", label="B", x=340, y=40, width=100, height=60)
    ops.add_diagram_edge(doc, source=a.ref.id, target=b.ref.id, kind="data")

    result = renderer.render(RenderRequest(svg=export_svg(doc)))
    image = Image.open(io.BytesIO(result.png)).convert("RGBA")

    inside = image.getpixel((60, 55))
    assert isinstance(inside, tuple)
    assert inside[:3] == (236, 238, 241)  # --surface-raised, via .default-service

    on_edge = image.getpixel((240, 70))  # the middle run of the straight-across route
    assert isinstance(on_edge, tuple)
    for channel, expected in zip(on_edge[:3], (91, 102, 114), strict=True):
        assert abs(channel - expected) <= 2  # --stroke-strong, via .default-data (antialiased)
