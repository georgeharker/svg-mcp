"""The annotation facades: a legend generated from the document, and a callout that points.

The legend tests are mostly about PROVENANCE — that a swatch wears the class the kind is really
painted by, so the key cannot drift from the picture — and the callout tests are mostly about
SURVIVAL: that the leader is derived rather than drawn, so moving the target moves the line.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from inkex import BaseElement

from svg_mcp import ops
from svg_mcp.model import Document
from svg_mcp.model.errors import InvalidArgument
from svg_mcp.ops.annotate import (
    LegendItem,
    read_callout_spec,
    read_legend_spec,
    roomiest_side,
    scan_legend_entries,
    wrap_words,
)
from svg_mcp.ops.diagram import Box, measure_label
from svg_mcp.query import get_params
from svg_mcp.render import get_renderer
from svg_mcp.render.base import RenderRequest
from svg_mcp.serialize import export_svg
from svg_mcp.session import DocumentStore

FIXTURES = Path(__file__).parent / "fixtures" / "themes"


def _doc(width: float = 700, height: float = 500) -> Document:
    return DocumentStore().create(width, height)[1]


def _classes(doc: Document, node_id: str) -> list[str]:
    return str(doc.resolve(node_id).get("class") or "").split()


def _under(doc: Document, node_id: str) -> list[BaseElement]:
    return [node for node in doc.resolve(node_id).iter() if isinstance(node.tag, str)]


def _tag(node: BaseElement) -> str:
    return str(node.tag).rsplit("}", 1)[-1]


def _num(node: BaseElement, attr: str) -> float:
    return float(str(node.get(attr)))


def _worn(doc: Document, node_id: str) -> list[str]:
    """Every class worn anywhere under a facade, in document order."""
    return [cls for node in _under(doc, node_id) for cls in str(node.get("class") or "").split()]


def _labels(doc: Document, node_id: str) -> list[str]:
    return [str(node.text) for node in _under(doc, node_id) if _tag(node) == "text"]


def _part(doc: Document, callout_id: str, part: str) -> BaseElement:
    return next(node for node in _under(doc, callout_id) if node.get("data-callout-part") == part)


def _swatch(doc: Document, legend_id: str, swatch_class: str) -> BaseElement:
    return next(
        node for node in _under(doc, legend_id) if swatch_class in str(node.get("class") or "")
    )


def _card(doc: Document, callout_id: str) -> Box:
    card = _part(doc, callout_id, "card")
    return Box(_num(card, "x"), _num(card, "y"), _num(card, "width"), _num(card, "height"))


def _ends(doc: Document, callout_id: str) -> tuple[tuple[float, float], tuple[float, float]]:
    """The leader's (card end, target end) — the dot marks which is which, so assert on that."""
    line = _part(doc, callout_id, "leader")
    return (
        (_num(line, "x1"), _num(line, "y1")),
        (_num(line, "x2"), _num(line, "y2")),
    )


def _scene(doc: Document) -> tuple[ops.PlacedNode, ops.PlacedNode]:
    """Two kinds, one edge kind, one container — the smallest thing worth a key."""
    api = ops.add_diagram_node(doc, kind="service", label="API", x=60, y=60, width=100, height=50)
    store = ops.add_diagram_node(
        doc, kind="datastore", label="DB", x=280, y=60, width=100, height=50
    )
    ops.add_diagram_edge(doc, source=api.ref.id, target=store.ref.id, kind="data")
    return api, store


# --- 1. what a generated legend actually says --------------------------------


def test_a_generated_legend_names_every_kind_the_document_uses_in_reading_order() -> None:
    doc = _doc()
    api, store = _scene(doc)
    ops.add_diagram_container(doc, members=[api.ref.id, store.ref.id], kind="cluster")
    ops.add_chart(
        doc,
        kind="bar",
        data=ops.BarData(
            categories=["q1", "q2"],
            series=[
                ops.Series(name="north", values=[1.0, 2.0]),
                ops.Series(name="south", values=[3.0, 4.0]),
            ],
        ),
        x=0,
        y=300,
    )
    entries = scan_legend_entries(doc)
    assert [entry.label for entry in entries] == [
        "service",
        "datastore",
        "data",
        "cluster",
        "north",
        "south",
    ]
    # A series entry points at the palette SLOT it was handed, not at its own name.
    assert [entry.swatch for entry in entries][-2:] == ["series-1", "series-2"]


def test_a_kind_the_document_never_uses_gets_no_row() -> None:
    doc = _doc()
    _scene(doc)
    placed = ops.add_legend(doc, x=420, y=40)
    assert placed.entries == ["service", "datastore", "data"]
    for absent in ("queue", "external", "decision", "cluster"):
        assert absent not in placed.entries


def test_a_kind_used_forty_times_still_gets_one_row() -> None:
    doc = _doc()
    for index in range(6):
        ops.add_diagram_node(doc, kind="service", label=f"s{index}", x=0, y=index * 60)
    assert ops.add_legend(doc, x=300, y=0).entries == ["service"]


def test_explicit_entries_are_taken_exactly_as_given() -> None:
    doc = _doc()
    _scene(doc)
    placed = ops.add_legend(
        doc,
        entries=[
            LegendItem(label="runs somewhere", swatch="service"),
            LegendItem(label="remembers", swatch="datastore"),
        ],
        title="Key",
        x=420,
        y=40,
    )
    assert placed.entries == ["runs somewhere", "remembers"]
    assert placed.auto is False
    assert _labels(doc, placed.ref.id) == ["Key", "runs somewhere", "remembers"]


def test_a_swatch_is_drawn_wearing_the_real_class_of_the_thing_it_stands_for() -> None:
    doc = _doc()
    api, store = _scene(doc)
    ops.add_diagram_container(doc, members=[api.ref.id, store.ref.id], kind="zone")
    placed = ops.add_legend(doc, x=420, y=40)
    worn = _worn(doc, placed.ref.id)
    assert worn == [
        "default-legend",
        "default-service",
        "default-datastore",
        "default-data",
        "default-zone",
    ]
    # ...and it is in the exported markup, so the stylesheet really reaches it.
    svg = export_svg(doc)
    assert 'class="default-service"' in svg
    assert 'class="default-data"' in svg


def test_an_edge_kind_is_drawn_as_a_line_and_everything_else_as_a_chip() -> None:
    doc = _doc()
    _scene(doc)
    placed = ops.add_legend(doc, x=420, y=40)
    forms = {
        str(node.get("class")): _tag(node)
        for node in _under(doc, placed.ref.id)
        if _tag(node) in ("rect", "line")
    }
    assert forms["default-service"] == "rect"
    assert forms["default-datastore"] == "rect"
    assert forms["default-data"] == "line"


def test_a_swatch_nothing_can_paint_is_refused_rather_than_drawn_blank() -> None:
    doc = _doc()
    with pytest.raises(InvalidArgument, match="paints legend swatch"):
        ops.add_legend(doc, entries=[LegendItem(label="?", swatch="nonesuch")], x=0, y=0)


# --- 2. layout ---------------------------------------------------------------


def _rows(doc: Document, legend_id: str) -> list[tuple[float, float]]:
    """Each swatch's (x, y) — the grid the entries were laid out on."""
    return [
        (_num(node, "x"), _num(node, "y"))
        for node in _under(doc, legend_id)
        if _tag(node) == "rect" and node.get("class")
    ]


def test_two_columns_fill_row_major() -> None:
    doc = _doc()
    for kind in ("service", "datastore", "queue", "external"):
        ops.add_diagram_node(doc, kind=kind, label=kind, x=0, y=0, width=40, height=20)
    placed = ops.add_legend(doc, columns=2, x=300, y=40)
    grid = _rows(doc, placed.ref.id)
    assert len(grid) == 4
    (x0, y0), (x1, y1), (x2, y2), (x3, y3) = grid
    assert y0 == y1 and y2 == y3 and y2 > y0  # two rows of two
    assert x0 == x2 and x1 == x3 and x1 > x0  # ...in two columns
    # Row-major: entry 2 is BELOW entry 1, not beside it.
    assert (x2, y2) == (x0, y2)


def test_a_one_column_legend_is_narrower_and_taller_than_a_two_column_one() -> None:
    doc = _doc()
    for kind in ("service", "datastore", "queue", "external"):
        ops.add_diagram_node(doc, kind=kind, label=kind, x=0, y=0, width=40, height=20)
    tall = ops.add_legend(doc, columns=1, x=300, y=40)
    wide = ops.add_legend(doc, columns=2, x=300, y=200)
    assert wide.w > tall.w
    assert wide.h < tall.h


def test_a_title_adds_a_row_of_its_own() -> None:
    doc = _doc()
    _scene(doc)
    bare = ops.add_legend(doc, x=420, y=40)
    titled = ops.add_legend(doc, title="Key", x=420, y=200)
    assert titled.h > bare.h


def test_a_legend_needs_at_least_one_column() -> None:
    doc = _doc()
    _scene(doc)
    with pytest.raises(InvalidArgument, match="at least one column"):
        ops.add_legend(doc, columns=0)


def test_an_auto_placed_legend_stacks_under_what_is_already_there() -> None:
    doc = _doc()
    node = ops.add_diagram_node(doc, kind="service", label="API", x=20, y=20, width=80, height=40)
    legend = ops.add_legend(doc)
    assert legend.y > node.y + node.h
    # ...and the next facade stacks under the LEGEND, not back under the node.
    following = ops.add_diagram_node(doc, kind="service", label="Next")
    assert following.y > legend.y + legend.h


# --- 3. editing and regenerating ---------------------------------------------


def test_regenerating_picks_up_a_kind_added_since_the_legend_was_drawn() -> None:
    doc = _doc()
    _scene(doc)
    placed = ops.add_legend(doc, x=420, y=40)
    assert placed.entries == ["service", "datastore", "data"]

    ops.add_diagram_node(doc, kind="queue", label="Jobs", x=60, y=200, width=100, height=40)
    unchanged = ops.edit_legend(doc, placed.ref.id)
    assert unchanged.entries == ["service", "datastore", "data"]  # it does not notice by itself

    result = ops.edit_legend(doc, placed.ref.id, regenerate=True)
    # The new kind lands with the other NODE kinds, not on the end: the order is the reading
    # order of the picture, and a regenerated key is generated the same way a new one is.
    assert result.entries == ["service", "datastore", "queue", "data"]
    assert result.regenerated is True
    assert "default-queue" in _worn(doc, placed.ref.id)


def test_a_hand_written_legend_refuses_to_be_regenerated_over() -> None:
    doc = _doc()
    _scene(doc)
    placed = ops.add_legend(
        doc, entries=[LegendItem(label="runs", swatch="service")], x=420, y=40
    )
    with pytest.raises(InvalidArgument, match="nothing to regenerate"):
        ops.edit_legend(doc, placed.ref.id, regenerate=True)


def test_an_edit_keeps_the_legend_exactly_where_it_was_put() -> None:
    doc = _doc()
    _scene(doc)
    placed = ops.add_legend(doc, x=420, y=40)
    ops.edit_legend(doc, placed.ref.id, title="Key", columns=2)
    grid = _rows(doc, placed.ref.id)
    assert grid[0][0] >= 420 and grid[0][1] >= 40
    card = next(node for node in _under(doc, placed.ref.id) if _tag(node) == "rect")
    assert (_num(card, "x"), _num(card, "y")) == (420.0, 40.0)
    spec = read_legend_spec(doc.resolve(placed.ref.id))
    assert spec is not None and (spec.title, spec.columns) == ("Key", 2)


def test_a_rebuild_leaves_exactly_one_generation_of_children_behind() -> None:
    doc = _doc()
    _scene(doc)
    placed = ops.add_legend(doc, x=420, y=40)
    first = len(_under(doc, placed.ref.id))
    ops.edit_legend(doc, placed.ref.id, title="Key")
    ops.edit_legend(doc, placed.ref.id, title="")
    assert len(_under(doc, placed.ref.id)) == first


def test_editing_something_that_is_not_a_legend_is_refused() -> None:
    doc = _doc()
    rect = ops.add_rect(doc, x=0, y=0, width=10, height=10)
    with pytest.raises(InvalidArgument, match="is not a legend"):
        ops.edit_legend(doc, rect.id, title="nope")


def test_reflow_leaves_legends_alone() -> None:
    doc = _doc()
    _scene(doc)
    ops.add_legend(doc, x=420, y=40)
    before = export_svg(doc).split("<style")[0]
    ops.reflow(doc)
    assert export_svg(doc).split("<style")[0] == before


# --- 4. which theme paints the swatch ----------------------------------------


def test_a_themeless_document_still_gets_a_styled_legend() -> None:
    doc = _doc()
    assert doc.theme_meta == {}  # nothing loaded, nothing routed
    _scene(doc)
    placed = ops.add_legend(doc, x=420, y=40)
    assert _classes(doc, placed.ref.id) == ["default-legend"]
    svg = export_svg(doc)
    assert ".default-legend" in svg
    assert ".default-service" in svg


def test_a_swatch_follows_whichever_theme_serves_its_kind() -> None:
    doc = _doc()
    ops.load_theme(doc, "atlas", search_paths=[FIXTURES])
    ops.add_diagram_node(doc, kind="service", label="API", x=60, y=60, width=100, height=50)
    placed = ops.add_legend(doc, x=420, y=40)
    worn = _worn(doc, placed.ref.id)
    # The kind is atlas's; the legend's own panel is the bundled default's, since atlas never
    # claimed the role. Both are real, and neither had to be named.
    assert worn == ["default-legend", "atlas-service"]
    assert 'class="atlas-service"' in export_svg(doc)


def test_an_unthemed_legend_wears_nothing_and_stays_that_way_across_an_edit() -> None:
    doc = _doc()
    _scene(doc)
    placed = ops.add_legend(doc, x=420, y=40, themed=False)
    assert _worn(doc, placed.ref.id) == []
    ops.edit_legend(doc, placed.ref.id, title="Key")
    assert _worn(doc, placed.ref.id) == []


def test_get_params_hands_back_the_spec_a_legend_was_built_from() -> None:
    doc = _doc()
    _scene(doc)
    placed = ops.add_legend(doc, title="Key", columns=2, x=420, y=40)
    reported = get_params(doc, placed.ref.id)
    assert reported["kind"] == "legend"
    assert reported["parametric"] is True
    params = reported["params"]
    assert isinstance(params, dict)
    assert (params["title"], params["columns"], params["auto"]) == ("Key", 2.0, True)
    assert params["entries"] == [
        {"label": "service", "swatch": "service", "form": "rect"},
        {"label": "datastore", "swatch": "datastore", "form": "rect"},
        {"label": "data", "swatch": "data", "form": "line"},
    ]


# --- 5. callouts: wrapping ---------------------------------------------------


def _width(text: str) -> float:
    return measure_label(text, "sans-serif", 11.0)[0]


def test_a_greedy_wrap_packs_as_many_words_as_the_width_allows() -> None:
    words = ["alpha", "gamma", "delta", "sigma", "kappa", "theta"]
    text = " ".join(words)
    limit = _width("alpha gamma") + 0.5  # room for two of them, never three
    assert _width("alpha gamma delta") > limit
    lines = wrap_words(text, max_width=limit, font="sans-serif", size=11.0)
    assert lines == ["alpha gamma", "delta sigma", "kappa theta"]
    assert wrap_words(text, max_width=1000.0, font="sans-serif", size=11.0) == [text]


def test_a_word_wider_than_the_card_keeps_its_own_line_rather_than_being_cut() -> None:
    lines = wrap_words(
        "a supercalifragilisticexpialidocious b", max_width=30.0, font="sans-serif", size=11.0
    )
    assert lines == ["a", "supercalifragilisticexpialidocious", "b"]


def test_more_lines_means_a_taller_card() -> None:
    doc = _doc()
    node = ops.add_diagram_node(doc, kind="service", label="API", x=300, y=200, width=80, height=40)
    short = ops.add_callout(doc, target=node.ref.id, text="one", x=20, y=20)
    long = ops.add_callout(
        doc,
        target=node.ref.id,
        text="one two three four five six seven eight nine ten eleven twelve",
        x=20,
        y=300,
        max_width=80,
    )
    assert long.lines > short.lines == 1
    assert long.h > short.h


def test_a_callout_needs_room_to_wrap_into() -> None:
    doc = _doc()
    node = ops.add_diagram_node(doc, kind="service", label="API", x=300, y=200)
    with pytest.raises(InvalidArgument, match="wrap its text into"):
        ops.add_callout(doc, target=node.ref.id, text="nope", max_width=1)


# --- 6. callouts: placement --------------------------------------------------


def test_the_roomiest_side_is_the_one_with_the_most_canvas_beyond_it() -> None:
    canvas = (0.0, 0.0, 1000.0, 1000.0)
    assert roomiest_side(Box(10.0, 400.0, 50.0, 50.0), canvas) == "E"
    assert roomiest_side(Box(900.0, 400.0, 50.0, 50.0), canvas) == "W"
    assert roomiest_side(Box(400.0, 900.0, 50.0, 50.0), canvas) == "N"
    assert roomiest_side(Box(400.0, 10.0, 50.0, 50.0), canvas) == "S"
    # Dead centre: every side is equally free, so the tie order decides — N first.
    assert roomiest_side(Box(475.0, 475.0, 50.0, 50.0), canvas) == "N"


def test_an_auto_placed_callout_goes_where_there_is_room_for_it() -> None:
    doc = _doc(700, 500)
    node = ops.add_diagram_node(doc, kind="service", label="API", x=40, y=220, width=80, height=50)
    placed = ops.add_callout(doc, target=node.ref.id, text="hot path")
    assert placed.side == "E"  # 580 of canvas to the east, 40 to the west
    card = _card(doc, placed.ref.id)
    assert card.x == pytest.approx(40 + 80 + 40)  # the node's east edge plus --callout-gap
    assert card.cy == pytest.approx(220 + 25)  # centered on the face it sits off


def test_the_distance_is_honoured_when_it_is_given() -> None:
    doc = _doc()
    node = ops.add_diagram_node(doc, kind="service", label="API", x=40, y=220, width=80, height=50)
    near = ops.add_callout(doc, target=node.ref.id, text="close", distance=10)
    far = ops.add_callout(doc, target=node.ref.id, text="far", distance=120)
    assert _card(doc, near.ref.id).x == pytest.approx(130.0)
    assert _card(doc, far.ref.id).x == pytest.approx(240.0)


def test_a_named_side_overrules_where_there_is_most_room() -> None:
    doc = _doc()
    node = ops.add_diagram_node(doc, kind="service", label="API", x=40, y=220, width=80, height=50)
    placed = ops.add_callout(doc, target=node.ref.id, text="above", side="N", distance=30)
    card = _card(doc, placed.ref.id)
    assert placed.side == "N"
    assert card.y + card.h == pytest.approx(220 - 30)
    assert card.cx == pytest.approx(40 + 40)


def test_explicit_coordinates_place_the_card_verbatim() -> None:
    doc = _doc()
    node = ops.add_diagram_node(doc, kind="service", label="API", x=300, y=220, width=80, height=50)
    placed = ops.add_callout(doc, target=node.ref.id, text="over here", x=30, y=400)
    card = _card(doc, placed.ref.id)
    assert (card.x, card.y) == (30.0, 400.0)
    assert placed.auto is False
    spec = read_callout_spec(doc.resolve(placed.ref.id))
    assert spec is not None and spec.auto is False


def test_a_callout_needs_something_with_a_box_to_point_at() -> None:
    doc = _doc()
    group = ops.create_group(doc)
    with pytest.raises(InvalidArgument, match="no bounding box"):
        ops.add_callout(doc, target=group.id, text="at what?")


# --- 7. callouts: the leader -------------------------------------------------


def test_the_leader_runs_from_the_card_face_to_the_target_face() -> None:
    doc = _doc()
    node = ops.add_diagram_node(doc, kind="service", label="API", x=40, y=220, width=80, height=50)
    placed = ops.add_callout(doc, target=node.ref.id, text="hot path", side="E", distance=40)
    card = _card(doc, placed.ref.id)
    (cx, cy), (tx, ty) = _ends(doc, placed.ref.id)
    assert (tx, ty) == pytest.approx((120.0, 245.0), abs=1.0)  # the node's east face midpoint
    assert (cx, cy) == pytest.approx((card.x, card.cy), abs=1.0)  # the card's west face midpoint


def test_the_leader_ends_in_a_dot_on_the_target_and_carries_no_arrowhead() -> None:
    doc = _doc()
    node = ops.add_diagram_node(doc, kind="service", label="API", x=40, y=220, width=80, height=50)
    placed = ops.add_callout(doc, target=node.ref.id, text="hot path")
    _, target_end = _ends(doc, placed.ref.id)
    dot = _part(doc, placed.ref.id, "dot")
    assert (_num(dot, "cx"), _num(dot, "cy")) == pytest.approx(target_end, abs=1e-6)
    assert _num(dot, "r") == 2.0
    assert "marker-end" not in export_svg(doc).split("data-callout")[-1]


def test_the_leader_wears_the_theme_s_leader_class_at_both_ends() -> None:
    doc = _doc()
    node = ops.add_diagram_node(doc, kind="service", label="API", x=40, y=220)
    placed = ops.add_callout(doc, target=node.ref.id, text="hot path")
    worn = [
        str(part.get("class"))
        for part in _under(doc, placed.ref.id)
        if part.get("data-callout-part") in ("leader", "dot")
    ]
    assert worn == ["default-leader", "default-leader"]
    assert ".default-leader" in export_svg(doc)


def test_moving_the_target_and_reflowing_re_anchors_both_ends_but_not_the_card() -> None:
    doc = _doc()
    node = ops.add_diagram_node(doc, kind="service", label="API", x=40, y=220, width=80, height=50)
    placed = ops.add_callout(doc, target=node.ref.id, text="hot path", side="E", distance=40)
    card_before = _card(doc, placed.ref.id)
    before = _ends(doc, placed.ref.id)

    ops.translate_node(doc, node.ref.id, dx=0, dy=-160)
    result = ops.reflow(doc)
    assert result.callouts_reanchored == 1
    assert result.skipped == []

    card_after = _card(doc, placed.ref.id)
    (cx, cy), (tx, ty) = _ends(doc, placed.ref.id)
    assert (card_after.x, card_after.y) == (card_before.x, card_before.y)  # the card is a decision
    assert (tx, ty) == pytest.approx((120.0, 85.0), abs=1.0)  # the node's face, where it is now
    assert (cx, cy) != pytest.approx(before[0], abs=1.0)  # and the card end re-chose its face
    assert (cx, cy) == pytest.approx((card_after.cx, card_after.y), abs=1.0)  # now the north face


def test_a_layout_pass_drags_the_leader_along_with_the_node() -> None:
    doc = _doc(900, 600)
    api, store = _scene(doc)
    placed = ops.add_callout(doc, target=store.ref.id, text="the write path", x=600, y=420)
    before = _ends(doc, placed.ref.id)
    ops.layout_diagram(doc, algorithm="layered", direction="TB")
    assert _ends(doc, placed.ref.id)[1] != pytest.approx(before[1], abs=1.0)


def test_a_callout_whose_target_is_gone_is_reported_rather_than_left_lying() -> None:
    doc = _doc()
    node = ops.add_diagram_node(doc, kind="service", label="API", x=40, y=220)
    placed = ops.add_callout(doc, target=node.ref.id, text="hot path")
    ops.delete_node(doc, node.ref.id)
    result = ops.reflow(doc)
    assert result.callouts_reanchored == 0
    assert result.skipped == [placed.ref.id]


def test_a_scoped_reflow_only_re_anchors_the_callouts_it_was_asked_about() -> None:
    doc = _doc(900, 600)
    first = ops.add_diagram_node(doc, kind="service", label="A", x=40, y=60, width=80, height=40)
    second = ops.add_diagram_node(doc, kind="service", label="B", x=40, y=300, width=80, height=40)
    ops.add_callout(doc, target=first.ref.id, text="one", x=400, y=60)
    ops.add_callout(doc, target=second.ref.id, text="two", x=400, y=300)
    assert ops.reflow(doc, scope=[first.ref.id]).callouts_reanchored == 1
    assert ops.reflow(doc).callouts_reanchored == 2


# --- 8. callouts: kinds and edits --------------------------------------------


@pytest.mark.parametrize("kind", ["note", "info", "warning", "success", "danger"])
def test_every_callout_kind_hooks_a_class_of_its_own(kind: str) -> None:
    doc = _doc()
    node = ops.add_diagram_node(doc, kind="service", label="API", x=40, y=220)
    placed = ops.add_callout(doc, target=node.ref.id, text="something", kind=kind)
    assert _classes(doc, placed.ref.id) == [f"default-{kind}"]
    assert f".default-{kind}" in export_svg(doc)


def test_changing_the_kind_swaps_the_class_and_leaves_everything_else_alone() -> None:
    doc = _doc()
    node = ops.add_diagram_node(doc, kind="service", label="API", x=40, y=220)
    placed = ops.add_callout(doc, target=node.ref.id, text="something", kind="note")
    card_before = _card(doc, placed.ref.id)
    ops.edit_callout(doc, placed.ref.id, kind="danger")
    assert _classes(doc, placed.ref.id) == ["default-danger"]
    assert _card(doc, placed.ref.id) == card_before
    spec = read_callout_spec(doc.resolve(placed.ref.id))
    assert spec is not None and spec.kind == "danger"


def test_new_text_is_re_wrapped_and_the_card_re_sized_around_it() -> None:
    doc = _doc()
    node = ops.add_diagram_node(doc, kind="service", label="API", x=300, y=220, width=80, height=40)
    placed = ops.add_callout(doc, target=node.ref.id, text="short", x=20, y=20, max_width=80)
    assert placed.lines == 1
    result = ops.edit_callout(
        doc, placed.ref.id, text="one two three four five six seven eight nine"
    )
    assert result.lines > 1
    assert _labels(doc, placed.ref.id) != ["short"]
    assert _card(doc, placed.ref.id).h > placed.h
    assert result.replaced is False  # an explicit card keeps its corner


def test_an_auto_placed_card_re_places_itself_when_its_text_changes_size() -> None:
    doc = _doc()
    node = ops.add_diagram_node(doc, kind="service", label="API", x=40, y=220, width=80, height=50)
    placed = ops.add_callout(doc, target=node.ref.id, text="short", side="N", distance=30)
    result = ops.edit_callout(
        doc, placed.ref.id, text="one two three four five six seven eight nine ten"
    )
    assert result.replaced is True
    card = _card(doc, placed.ref.id)
    # Taller card, same gap: it grew UPWARDS rather than into the node it points at.
    assert card.y + card.h == pytest.approx(220 - 30)
    assert card.h > placed.h


def test_editing_something_that_is_not_a_callout_is_refused() -> None:
    doc = _doc()
    rect = ops.add_rect(doc, x=0, y=0, width=10, height=10)
    with pytest.raises(InvalidArgument, match="is not a callout"):
        ops.edit_callout(doc, rect.id, text="nope")


def test_get_params_hands_back_the_spec_a_callout_was_built_from() -> None:
    doc = _doc()
    node = ops.add_diagram_node(doc, kind="service", label="API", x=40, y=220)
    placed = ops.add_callout(
        doc, target=node.ref.id, text="hot path", kind="warning", side="S", distance=25
    )
    params = get_params(doc, placed.ref.id)["params"]
    assert isinstance(params, dict)
    assert params["target"] == node.ref.id
    assert (params["text"], params["kind"], params["side"]) == ("hot path", "warning", "S")
    assert (params["distance"], params["max_width"], params["auto"]) == (25.0, 160.0, True)


# --- 9. what it actually renders ---------------------------------------------


def _render(doc: Document) -> object:
    renderer = get_renderer()
    if not renderer.available():
        pytest.skip("no resvg backend available")
    from PIL import Image

    return Image.open(io.BytesIO(renderer.render(RenderRequest(svg=export_svg(doc))).png)).convert(
        "RGBA"
    )


def _pixel(image: object, at: tuple[int, int]) -> tuple[int, int, int, int]:
    pixel = image.getpixel(at)  # type: ignore[attr-defined]
    assert isinstance(pixel, tuple)
    return (int(pixel[0]), int(pixel[1]), int(pixel[2]), int(pixel[3]))


def _swatch_centre(doc: Document, legend_id: str, swatch_class: str) -> tuple[int, int]:
    node = _swatch(doc, legend_id, swatch_class)
    return (
        int(_num(node, "x") + _num(node, "width") / 2),
        int(_num(node, "y") + _num(node, "height") / 2),
    )


def test_a_legend_swatch_renders_in_the_fill_its_kind_is_painted_with() -> None:
    doc = _doc()
    _scene(doc)
    placed = ops.add_legend(doc, x=420, y=40)
    image = _render(doc)
    # --surface-raised, via .default-service: the swatch IS the node's paint, not a copy of it.
    assert _pixel(image, _swatch_centre(doc, placed.ref.id, "default-service"))[:3] == (
        0xEC,
        0xEE,
        0xF1,
    )
    # ...and the datastore chip beside it is the sunken surface, so the two are told apart.
    assert _pixel(image, _swatch_centre(doc, placed.ref.id, "default-datastore"))[:3] == (
        0xE2,
        0xE5,
        0xE9,
    )


def test_the_dark_variant_repaints_a_legend_swatch_without_moving_it() -> None:
    doc = _doc()
    _scene(doc)
    placed = ops.add_legend(doc, x=420, y=40)
    at = _swatch_centre(doc, placed.ref.id, "default-service")
    light = _pixel(_render(doc), at)
    ops.set_theme_variant(doc, "dark")
    dark = _pixel(_render(doc), at)
    assert light[:3] != dark[:3]
    assert dark[:3] == (0x26, 0x2B, 0x31)  # --surface-raised, dark
    assert _swatch_centre(doc, placed.ref.id, "default-service") == at  # nothing moved


def test_a_leader_is_painted_along_the_whole_run_between_card_and_node() -> None:
    doc = _doc()
    # Height 51 puts the node's centre line on a whole pixel boundary, so the 1px leader lands
    # squarely in one row rather than being split (and halved) across two.
    node = ops.add_diagram_node(doc, kind="service", label="API", x=40, y=200, width=80, height=51)
    placed = ops.add_callout(doc, target=node.ref.id, text="hot path", side="E", distance=40)
    card = _card(doc, placed.ref.id)
    image = _render(doc)
    (_, cy), (tx, _) = _ends(doc, placed.ref.id)
    row = int(cy)
    for fraction in (0.25, 0.5, 0.75):
        at = (int(tx + (card.x - tx) * fraction), row)
        assert _pixel(image, at)[3] > 60, f"no leader at {at}"
    # Just off the run, there is nothing: a leader is a line, not a band.
    assert _pixel(image, (int((tx + card.x) / 2), row + 6))[3] == 0


def test_a_callout_card_renders_in_its_kind_s_wash() -> None:
    doc = _doc()
    node = ops.add_diagram_node(doc, kind="service", label="API", x=40, y=200, width=80, height=50)
    placed = ops.add_callout(
        doc, target=node.ref.id, text="watch this", kind="warning", x=300, y=60
    )
    card = _card(doc, placed.ref.id)
    image = _render(doc)
    # A corner well inside the border and clear of the text: the wash, not the accent stroke.
    assert _pixel(image, (int(card.x + 4), int(card.y + 4)))[:3] == (0xFB, 0xF0, 0xDC)


# --- 10. the half-calls, refused rather than half-honoured -------------------


def test_placing_a_callout_by_half_a_coordinate_is_refused() -> None:
    doc = _doc()
    node = ops.add_diagram_node(doc, kind="service", label="API", x=40, y=220)
    with pytest.raises(InvalidArgument, match="BOTH x and y"):
        ops.add_callout(doc, target=node.ref.id, text="where?", x=300)


def test_writing_the_entries_and_regenerating_them_in_one_call_is_refused() -> None:
    doc = _doc()
    _scene(doc)
    placed = ops.add_legend(doc, x=420, y=40)
    with pytest.raises(InvalidArgument, match="not both in one call"):
        ops.edit_legend(
            doc,
            placed.ref.id,
            entries=[LegendItem(label="runs", swatch="service")],
            regenerate=True,
        )


def test_a_new_side_moves_an_auto_placed_card_rather_than_crossing_it_with_the_leader() -> None:
    doc = _doc()
    node = ops.add_diagram_node(doc, kind="service", label="API", x=300, y=220, width=80, height=50)
    placed = ops.add_callout(doc, target=node.ref.id, text="hot path", side="E", distance=40)
    result = ops.edit_callout(doc, placed.ref.id, side="W")
    assert result.replaced is True
    card = _card(doc, placed.ref.id)
    assert card.x + card.w == pytest.approx(300 - 40)
    (_, _), (tx, _) = _ends(doc, placed.ref.id)
    assert tx == pytest.approx(300.0, abs=1.0)  # the node's west face, which is what it now names


def test_a_new_side_on_a_hand_placed_card_steers_the_leader_and_nothing_else() -> None:
    doc = _doc()
    node = ops.add_diagram_node(doc, kind="service", label="API", x=300, y=220, width=80, height=50)
    placed = ops.add_callout(doc, target=node.ref.id, text="hot path", x=560, y=60)
    before = _card(doc, placed.ref.id)
    result = ops.edit_callout(doc, placed.ref.id, side="S")
    assert result.replaced is False
    assert _card(doc, placed.ref.id) == before
    (_, _), (_, ty) = _ends(doc, placed.ref.id)
    assert ty == pytest.approx(270.0, abs=1.0)  # the node's south face
