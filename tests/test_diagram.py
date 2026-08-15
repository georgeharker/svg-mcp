"""Diagram facades: the pure routing engine, then the nodes/edges/reflow built on top of it."""

from __future__ import annotations

import io
import math
import re
from pathlib import Path

import pytest

from svg_mcp import ops
from svg_mcp.model import Document
from svg_mcp.model.errors import InvalidArgument
from svg_mcp.ops.diagram import (
    Box,
    Corridor,
    LabelContext,
    _box_of,
    _overlap_area,
    _shape_for,
    anchor_point,
    auto_side,
    centred_offsets,
    collinear_runs,
    corridor_groups,
    label_candidates,
    label_rect,
    measure_label,
    offset_run,
    orthogonal_waypoints,
    place_label,
    read_edge_spec,
    read_node_spec,
    resolve_sides,
    rounded_path,
    route_edge,
    score_label,
    separate_corridors,
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


# --- 1b. threading a route through lane points -------------------------------


def test_a_threaded_route_drops_into_its_lane_and_runs_along_it() -> None:
    # The shape a rank-spanning edge wants: out of A, down into the lane, along past everything
    # in between, up to B. Every cross-axis change lands between two stations, never on one.
    points = orthogonal_waypoints(
        (100.0, 50.0), "E", (600.0, 60.0), "W", 12.0, [(250.0, 200.0), (400.0, 200.0)]
    )
    assert points[0] == (100.0, 50.0)
    assert points[-1] == (600.0, 60.0)
    assert [at[1] for at in points[1:-1]] == [50.0, 200.0, 200.0, 60.0]
    assert points[1][0] == pytest.approx(181.0)  # half way from the stub end to the first lane
    assert points[-2][0] == pytest.approx(494.0)  # and half way from the last lane to the other
    # The lane points themselves are run straight through, so they leave no vertex behind.
    assert (250.0, 200.0) not in points and (400.0, 200.0) not in points


def test_a_lane_the_route_is_already_level_with_adds_no_jog() -> None:
    points = orthogonal_waypoints(
        (100.0, 50.0), "E", (600.0, 50.0), "W", 12.0, [(250.0, 50.3), (400.0, 50.0)]
    )
    assert points == [(100.0, 50.0), (600.0, 50.0)]  # one straight run, nothing to turn at


def test_the_corners_a_thread_turns_are_rounded_like_any_other() -> None:
    result = route_edge(
        (100.0, 50.0),
        "E",
        (600.0, 50.0),
        "W",
        route="orthogonal",
        stub=12.0,
        radius=8.0,
        via=[(250.0, 200.0), (400.0, 200.0)],
    )
    corners = _corners(result.d)
    assert len(corners) == 4  # down into the lane, along it, and back up: two turns at each end
    assert all(command in ("M", "L", "Q") for command, _points in _segments(result.d))


def test_a_thread_never_reaches_past_the_anchors() -> None:
    a, b = (100.0, 50.0), (600.0, 50.0)
    points = orthogonal_waypoints(a, "E", b, "W", 12.0, [(250.0, 200.0)])
    assert points[0] == a and points[-1] == b


def test_an_auto_side_faces_the_lane_rather_than_the_far_node() -> None:
    # B is due east, so the direct line leaves E — but the lane drops away south first, and an
    # anchor pointing away from its own first segment is what reads as broken.
    source, target = Box(0, 0, 40, 40), Box(400, 0, 40, 40)
    assert resolve_sides(source, target, "auto", "auto") == ("E", "W")
    threaded = resolve_sides(source, target, "auto", "auto", [(20.0, 300.0), (420.0, 300.0)])
    assert threaded == ("S", "S")


def test_an_explicit_anchor_still_beats_the_lane() -> None:
    source, target = Box(0, 0, 40, 40), Box(400, 0, 40, 40)
    assert resolve_sides(source, target, "N", "auto", [(20.0, 300.0)]) == ("N", "W")


def test_a_straight_route_polylines_through_its_lane() -> None:
    result = route_edge(
        (100.0, 50.0),
        "E",
        (600.0, 50.0),
        "W",
        route="straight",
        stub=12.0,
        radius=8.0,
        via=[(250.0, 200.0), (400.0, 200.0)],
    )
    assert _segments(result.d) == [
        ("M", [(100.0, 50.0)]),
        ("L", [(250.0, 200.0)]),
        ("L", [(400.0, 200.0)]),
        ("L", [(600.0, 50.0)]),
    ]
    assert _close(result.label_at, (500.0, 125.0))  # the longest run of the four, as ever


def test_a_threaded_spline_chains_one_cubic_per_hop_and_keeps_its_end_handles() -> None:
    a, b = (100.0, 50.0), (600.0, 50.0)
    via = [(250.0, 200.0), (400.0, 200.0)]
    result = route_edge(a, "E", b, "W", route="spline", stub=12.0, radius=8.0, via=via)
    curves = [points for command, points in _segments(result.d) if command == "C"]
    assert len(curves) == 3  # a→via, via→via, via→b
    first_reach = 0.4 * ((150.0**2 + 150.0**2) ** 0.5)
    assert _close(curves[0][0], (100.0 + first_reach, 50.0))  # still leaves along its own normal
    last_reach = 0.4 * ((200.0**2 + 150.0**2) ** 0.5)
    assert _close(curves[-1][1], (600.0 - last_reach, 50.0))  # and still arrives along b's
    assert _close(curves[-1][2], b)


# --- 1c. scoring where an edge label goes -------------------------------------


def _rect_of(at: Point, dy: float, size: Point) -> Box:
    return label_rect(at, dy, size)


def test_a_lone_route_keeps_the_answer_it_always_had() -> None:
    # Nothing to avoid: the label still sits above the midpoint of the longest segment, because
    # that candidate is generated first and ties are broken towards it.
    points = [(0.0, 0.0), (200.0, 0.0), (200.0, 60.0)]
    at, dy = place_label(points, (40.0, 14.0), LabelContext())
    assert at == (100.0, 0.0)
    assert dy == -4.0


def test_a_label_leaves_the_segment_that_runs_through_a_box() -> None:
    points = [(0.0, 0.0), (200.0, 0.0), (200.0, 100.0)]
    size = (40.0, 14.0)
    blocker = Box(60.0, -40.0, 80.0, 80.0)  # squarely over the longest segment's midpoint
    plain = place_label(points, size, LabelContext())
    at, dy = place_label(points, size, LabelContext(boxes=(blocker,)))
    assert (at, dy) != plain
    assert (
        _rect_of(at, dy, size).x >= blocker.x + blocker.w
        or _rect_of(at, dy, size).y >= blocker.y + blocker.h
    )


def test_a_segment_shorter_than_the_label_is_not_a_candidate() -> None:
    # Only the long run can hold the text, so the stubs at either end are never offered.
    points = [(0.0, 0.0), (10.0, 0.0), (210.0, 0.0), (220.0, 0.0)]
    ats = {at for at, _dy in label_candidates(points, (40.0, 14.0))}
    assert ats == {(110.0, 0.0)}


def test_a_route_with_no_segment_long_enough_still_labels_its_longest() -> None:
    points = [(0.0, 0.0), (12.0, 0.0), (12.0, 20.0)]
    ats = [at for at, _dy in label_candidates(points, (400.0, 14.0))]
    # The 20-unit vertical is the longest, so it is used anyway — offered to either side of the
    # line, and nothing else is offered at all.
    assert [at[1] for at in ats] == [10.0, 10.0]
    assert [at[0] for at in ats] == [12.0 - 204.0, 12.0 + 204.0]


def test_a_vertical_run_offers_both_sides_and_never_sits_on_the_line() -> None:
    points = [(0.0, 0.0), (0.0, 200.0)]
    size = (40.0, 14.0)
    at, dy = place_label(points, size, LabelContext())
    assert at == (-24.0, 100.0)  # half the width plus the side gap, to the left first
    assert dy == -4.0


def test_the_second_label_steps_aside_from_the_first() -> None:
    size = (40.0, 14.0)
    across = [(0.0, 0.0), (200.0, 0.0)]
    down = [(100.0, -100.0), (100.0, 100.0), (300.0, 100.0)]
    first_at, first_dy = place_label(across, size, LabelContext())
    first = _rect_of(first_at, first_dy, size)
    second_at, second_dy = place_label(
        down,
        size,
        LabelContext(placed=(first,), segments=((across[0], across[1]),)),
    )
    second = _rect_of(second_at, second_dy, size)
    assert _overlap_area(first, second) == 0.0
    assert second_at != (76.0, 0.0)  # not the crossing point it would have taken unencumbered


def test_score_charges_area_for_a_box_and_a_count_for_a_crossing() -> None:
    rect = Box(0.0, 0.0, 40.0, 10.0)
    assert score_label(rect, (), (), ()) == 0.0
    half = score_label(rect, (Box(0.0, 0.0, 20.0, 10.0),), (), ())
    whole = score_label(rect, (Box(0.0, 0.0, 40.0, 10.0),), (), ())
    assert half == pytest.approx(whole / 2.0)
    assert whole > score_label(rect, (), (), (((20.0, -50.0), (20.0, 50.0)),))
    assert score_label(rect, (), (), (((20.0, -50.0), (20.0, 50.0)),)) > 0.0


def test_placement_is_the_same_every_time_for_the_same_inputs() -> None:
    points = [(0.0, 0.0), (200.0, 0.0), (200.0, 100.0), (400.0, 100.0)]
    context = LabelContext(
        boxes=(Box(60.0, -40.0, 80.0, 80.0),),
        placed=(Box(180.0, 20.0, 40.0, 14.0),),
        segments=(((300.0, 0.0), (300.0, 200.0)),),
    )
    once = place_label(points, (40.0, 14.0), context)
    assert all(place_label(points, (40.0, 14.0), context) == once for _ in range(5))


# --- 1d. pulling shared corridors apart ---------------------------------------


def test_consecutive_segments_on_one_lane_are_a_single_run() -> None:
    runs = collinear_runs({"a": [(0.0, 0.0), (50.0, 0.0), (200.0, 0.0), (200.0, 80.0)]})
    assert [(run.axis, run.lo, run.hi, run.start, run.end) for run in runs] == [
        ("h", 0.0, 200.0, 0, 2),
        ("v", 0.0, 80.0, 2, 3),
    ]


def test_runs_group_when_they_share_a_lane_and_not_when_they_merely_touch() -> None:
    runs = collinear_runs(
        {
            "a": [(0.0, 0.0), (0.0, 100.0), (200.0, 100.0)],
            "b": [(0.5, 50.0), (0.5, 200.0)],  # within the corridor tolerance, and overlapping
            "c": [(0.0, -100.0), (0.0, 0.0)],  # meets a end to end: not on top of it
            "d": [(80.0, 0.0), (80.0, 100.0)],  # a different lane entirely
        }
    )
    groups = corridor_groups(runs)
    assert len(groups) == 1
    assert [run.edge for run in groups[0]] == ["a", "b"]


def test_offsets_are_centred_on_the_corridor() -> None:
    assert centred_offsets(1, 4.0) == [0.0]
    assert centred_offsets(2, 4.0) == [-2.0, 2.0]
    assert centred_offsets(3, 4.0) == [-4.0, 0.0, 4.0]
    assert centred_offsets(4, 4.0) == [-6.0, -2.0, 2.0, 6.0]


def _corridor_pair() -> dict[str, list[Point]]:
    """Two routes down the same vertical lane, each with a stub at either end."""
    return {
        "zed": [(-20.0, 0.0), (0.0, 0.0), (0.0, 100.0), (20.0, 100.0)],
        "alf": [(-20.0, 20.0), (0.0, 20.0), (0.0, 120.0), (20.0, 120.0)],
    }


def test_a_shared_corridor_fans_out_in_edge_id_order() -> None:
    out = separate_corridors(_corridor_pair(), pitch=4.0)
    assert [point[0] for point in out["alf"]] == [-20.0, -2.0, -2.0, 20.0]
    assert [point[0] for point in out["zed"]] == [-20.0, 2.0, 2.0, 20.0]
    assert [point[1] for point in out["zed"]] == [0.0, 0.0, 100.0, 100.0]  # nothing else moved


def test_separation_does_not_depend_on_the_order_the_routes_arrive_in() -> None:
    forwards = _corridor_pair()
    backwards = {key: list(forwards[key]) for key in reversed(list(forwards))}
    assert separate_corridors(backwards, pitch=4.0) == separate_corridors(forwards, pitch=4.0)


def test_an_offset_run_keeps_its_anchor_and_tapers_the_stub() -> None:
    points = [(0.0, 0.0), (0.0, 100.0), (50.0, 100.0)]
    run = Corridor(edge="a", axis="v", coord=0.0, lo=0.0, hi=100.0, start=0, end=1)
    moved = offset_run(points, run, 3.0)
    assert moved[0] == (0.0, 0.0)  # the anchor is the node's face: it cannot move
    assert moved[1] == (3.0, 100.0)  # the corner takes the whole shift — the stub leans over
    assert moved[2] == (50.0, 100.0)


def test_an_interior_run_moves_whole_and_re_joins_its_corners() -> None:
    points = [(0.0, 0.0), (20.0, 0.0), (20.0, 100.0), (60.0, 100.0)]
    run = Corridor(edge="a", axis="v", coord=20.0, lo=0.0, hi=100.0, start=1, end=2)
    moved = offset_run(points, run, -3.0)
    assert moved == [(0.0, 0.0), (17.0, 0.0), (17.0, 100.0), (60.0, 100.0)]
    assert moved[0][1] == moved[1][1] and moved[2][1] == moved[3][1]  # no jog appeared


def test_a_run_shorter_than_twice_the_pitch_is_left_alone() -> None:
    routes = {
        "a": [(-20.0, 0.0), (0.0, 0.0), (0.0, 6.0), (20.0, 6.0)],
        "b": [(-20.0, 2.0), (0.0, 2.0), (0.0, 8.0), (20.0, 8.0)],
    }
    assert separate_corridors(routes, pitch=4.0) == routes


def test_a_group_tightens_its_pitch_rather_than_crossing_a_box() -> None:
    routes = _corridor_pair()
    # A box that the full -2 offset would put "alf" inside, but a halved one would not.
    blocker = Box(-3.0, 40.0, 2.0, 20.0)
    out = separate_corridors(routes, pitch=4.0, obstacles={"alf": [blocker]})
    assert [point[0] for point in out["alf"]] == [-20.0, -1.0, -1.0, 20.0]
    assert [point[0] for point in out["zed"]] == [-20.0, 1.0, 1.0, 20.0]


def test_a_corridor_keeps_its_overlap_when_every_pitch_would_cross_a_box() -> None:
    routes = _corridor_pair()
    blocker = Box(-4.0, 40.0, 4.0, 20.0)  # covers every offset the group could try
    assert separate_corridors(routes, pitch=4.0, obstacles={"alf": [blocker]}) == routes


def test_a_pinned_route_holds_its_slot_and_its_geometry() -> None:
    routes = _corridor_pair()
    out = separate_corridors(routes, pitch=4.0, pinned=["alf"])
    assert out["alf"] == routes["alf"]  # what the author typed is drawn as the author typed it
    assert [point[0] for point in out["zed"]] == [-20.0, 2.0, 2.0, 20.0]  # the free one steps off


def test_a_run_already_inside_a_box_is_still_separated_from_its_twin() -> None:
    # The offset must not be blamed for a crossing that was there before it: refusing to move
    # would leave the reader looking at ONE line through the box instead of two.
    routes = _corridor_pair()
    blocker = Box(-10.0, 40.0, 20.0, 20.0)
    out = separate_corridors(routes, pitch=4.0, obstacles={"alf": [blocker], "zed": [blocker]})
    assert out != routes
    assert [point[0] for point in out["alf"]] == [-20.0, -2.0, -2.0, 20.0]


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
    # Placed coordinates are stored rounded to 3 decimals; the recomputed sum is not.
    assert second.x == 20.0 and second.y == pytest.approx(first.y + first.h + 24.0, abs=2e-3)
    assert third.y == pytest.approx(second.y + second.h + 24.0, abs=2e-3)


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
        "themed": True,
        "auto": False,
    }
    # The dict comparison above is exhaustive, so it already says there is no `pinned` key: a
    # node the layout owns stores none, the same way an edge nobody routed by hand stores no
    # waypoints.


def test_a_pinned_node_round_trips_through_get_params() -> None:
    doc = _doc()
    placed = ops.add_diagram_node(
        doc, kind="service", label="Fixed", width=80, height=40, pinned=True
    )
    params = get_params(doc, placed.ref.id)["params"]
    assert isinstance(params, dict)
    assert params["pinned"] is True
    spec = read_node_spec(doc.resolve(placed.ref.id))
    assert spec is not None and spec.pinned is True


def test_pinning_is_set_and_cleared_by_edit_and_survives_every_other_edit() -> None:
    doc = _doc()
    placed = ops.add_diagram_node(doc, kind="service", label="A", width=80, height=40)
    ops.edit_diagram_node(doc, placed.ref.id, pinned=True)

    # A re-label and a re-kind rewrite the whole spec; neither is a statement about placement.
    ops.edit_diagram_node(doc, placed.ref.id, label="A renamed")
    ops.edit_diagram_node(doc, placed.ref.id, kind="note")
    spec = read_node_spec(doc.resolve(placed.ref.id))
    assert spec is not None and spec.pinned is True and spec.kind == "note"

    ops.edit_diagram_node(doc, placed.ref.id, pinned=False)
    cleared = read_node_spec(doc.resolve(placed.ref.id))
    assert cleared is not None and cleared.pinned is False
    params = get_params(doc, placed.ref.id)["params"]
    assert isinstance(params, dict)
    assert "pinned" not in params  # cleared back to an absent key, not a stored false


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


def _x_at(d: str, y: float) -> float:
    """Where a drawn route sits horizontally as it passes a given height."""
    points = _vertices(d)
    for (sx, sy), (ex, ey) in zip(points, points[1:], strict=False):
        if min(sy, ey) <= y <= max(sy, ey) and abs(ey - sy) > TOL:
            return sx + (ex - sx) * (y - sy) / (ey - sy)
    raise AssertionError(f"the route never reaches y={y}")


def _label_of(doc: Document, edge_id: str) -> tuple[Point, float]:
    text = next(child for child in doc.resolve(edge_id) if child.TAG == "text")
    return (float(str(text.get("x"))), float(str(text.get("y")))), float(str(text.get("dy")))


def test_two_edges_down_one_corridor_are_drawn_as_two_lines() -> None:
    doc = _doc()
    a = ops.add_diagram_node(doc, kind="service", label="A", x=40, y=40, width=80, height=40)
    b = ops.add_diagram_node(doc, kind="service", label="B", x=440, y=320, width=80, height=40)
    c = ops.add_diagram_node(doc, kind="service", label="C", x=40, y=140, width=80, height=40)
    d = ops.add_diagram_node(doc, kind="service", label="D", x=440, y=220, width=80, height=40)
    first = ops.add_diagram_edge(doc, source=a.ref.id, target=b.ref.id)
    second = ops.add_diagram_edge(doc, source=c.ref.id, target=d.ref.id)
    # Both Z routes turn on the same mid-rank lane and run down it together for a while; drawn
    # unseparated they would be one line where the reader has to see two.
    at = 200.0
    apart = abs(
        _x_at(str(doc.resolve(first.ref.id)[0].get("d")), at)
        - _x_at(str(doc.resolve(second.ref.id)[0].get("d")), at)
    )
    assert apart >= 2.0


def test_a_label_moves_off_the_longest_segment_when_a_node_sits_on_it() -> None:
    doc = _doc()
    a = ops.add_diagram_node(doc, kind="service", label="A", x=40, y=40, width=80, height=40)
    b = ops.add_diagram_node(doc, kind="service", label="B", x=400, y=200, width=80, height=40)
    edge = ops.add_diagram_edge(doc, source=a.ref.id, target=b.ref.id, label="gateway")
    (free_x, free_y), _dy = _label_of(doc, edge.ref.id)
    assert _close((free_x, free_y), (260.0, 140.0)) or abs(free_y - 140.0) <= TOL  # the mid lane

    blocker = ops.add_diagram_node(
        doc, kind="note", label="in the way", x=200, y=110, width=120, height=60
    )
    ops.reflow(doc)
    (moved_x, moved_y), _moved_dy = _label_of(doc, edge.ref.id)
    box = _box_of(doc, blocker.ref.id)
    assert box is not None
    size = measure_label("gateway", "sans-serif", 12.0)
    assert _overlap_area(label_rect((moved_x, moved_y), _moved_dy, size), box) == 0.0
    assert abs(moved_y - 60.0) <= TOL  # up onto the first horizontal run, which is clear


def test_an_edge_spec_round_trips_through_get_params() -> None:
    doc = _doc()
    a, b = _pair(doc)
    edge = ops.add_diagram_edge(
        doc, source=a, target=b, kind="control", source_anchor="S", route="spline", label="hi"
    )
    result = get_params(doc, edge.ref.id)
    assert result["kind"] == "diagram_edge"
    # No `waypoints` key at all: an edge nobody pinned a route on stores none, and get_params
    # reports the absence rather than inventing a null.
    assert result["params"] == {
        "source": a,
        "target": b,
        "kind": "control",
        "sa": "S",
        "ta": "auto",
        "route": "spline",
        "label": "hi",
        "themed": True,
    }


def test_a_pinned_route_round_trips_through_get_params() -> None:
    doc = _doc()
    a, b = _pair(doc)
    edge = ops.add_diagram_edge(doc, source=a, target=b, waypoints=[(200.0, 300.0), (260.0, 300.0)])
    params = get_params(doc, edge.ref.id)["params"]
    assert isinstance(params, dict)
    assert params["waypoints"] == [[200.0, 300.0], [260.0, 300.0]]


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


def _vertices(d: str) -> list[Point]:
    """The polyline a drawn route follows — a rounded corner standing in for its own vertex."""
    return [points[0] if command == "Q" else points[-1] for command, points in _segments(d)]


def _through(d: str, at: Point) -> bool:
    """True when the drawn route passes over ``at``, at a corner or in the middle of a run.

    A pinned point the route runs straight through leaves no vertex of its own behind, so asking
    about vertices would be asking about the path encoding rather than about the drawing.
    """
    points = _vertices(d)
    for start, end in zip(points, points[1:], strict=False):
        span = math.dist(start, end)
        if span <= 1e-9:
            continue
        along = (
            (at[0] - start[0]) * (end[0] - start[0]) + (at[1] - start[1]) * (end[1] - start[1])
        ) / span**2
        held = min(1.0, max(0.0, along))
        nearest = (start[0] + held * (end[0] - start[0]), start[1] + held * (end[1] - start[1]))
        if _close(nearest, at):
            return True
    return False


def test_a_pinned_route_keeps_its_middle_and_re_anchors_its_ends() -> None:
    doc = _doc()
    a, b = _pair(doc)
    edge = ops.add_diagram_edge(doc, source=a, target=b, waypoints=[(220.0, 320.0)])
    assert _through(str(doc.resolve(edge.ref.id)[0].get("d")), (220.0, 320.0))

    ops.translate_node(doc, b, 0, 160)
    ops.reflow(doc)
    d = str(doc.resolve(edge.ref.id)[0].get("d"))
    assert _through(d, (220.0, 320.0))  # the middle is what the author typed, not what moved
    end = _segments(d)[-1][1][-1]
    assert _close(end, (340.0, 320.0))  # but the far end followed B to where B is now


def test_clearing_the_pinned_route_hands_the_edge_back_to_the_router() -> None:
    doc = _doc()
    a, b = _pair(doc)
    edge = ops.add_diagram_edge(doc, source=a, target=b, waypoints=[(220.0, 320.0)])
    ops.edit_diagram_edge(doc, edge.ref.id, waypoints=[])
    spec = read_edge_spec(doc.resolve(edge.ref.id))
    assert spec is not None and spec.waypoints is None  # an empty list CLEARS the pin
    assert "waypoints" not in str(doc.resolve(edge.ref.id).get("data-diagram-edge"))
    assert not _through(str(doc.resolve(edge.ref.id)[0].get("d")), (220.0, 320.0))


def test_editing_an_edge_without_naming_waypoints_leaves_the_pinned_route_alone() -> None:
    doc = _doc()
    a, b = _pair(doc)
    edge = ops.add_diagram_edge(doc, source=a, target=b, waypoints=[(220.0, 320.0)])
    ops.edit_diagram_edge(doc, edge.ref.id, label="still pinned")
    spec = read_edge_spec(doc.resolve(edge.ref.id))
    assert spec is not None and spec.waypoints == ((220.0, 320.0),)


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


def test_reflow_rebakes_the_label_halo_after_a_variant_switch() -> None:
    doc = _doc()
    a = ops.add_diagram_node(doc, kind="service", label="A", x=40, y=40)
    b = ops.add_diagram_node(doc, kind="service", label="B", x=300, y=40)
    edge = ops.add_diagram_edge(doc, source=a.ref.id, target=b.ref.id, kind="data", label="hop")

    label = next(t for t in doc.resolve(edge.ref.id) if t.TAG == "text")
    light = doc.theme_meta["default"].tokens["--canvas"]
    assert label.style["stroke"] == light

    ops.set_theme_variant(doc, "dark")
    assert label.style["stroke"] == light  # pinned: the switch alone must not touch it

    ops.reflow(doc)
    dark = doc.theme_meta["default"].tokens["--canvas"]
    assert dark != light
    assert label.style["stroke"] == dark  # the reflow is the re-bake
