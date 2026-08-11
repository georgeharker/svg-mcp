"""The last two tier-1 facades: a table that measures itself, and a standalone callout card.

The table tests are mostly about MEASUREMENT — that a column is as wide as what is in it, that a
row grows to the cell that wrapped, and that a column of numbers aligns itself — because every
one of those is something a hand-laid table gets wrong the first time the data changes. The card
tests are mostly about DERIVATION: its height is never given, so the words have to decide it.
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
    _is_number,
    read_card_spec,
    read_table_spec,
    resolve_col_align,
    table_columns,
)
from svg_mcp.ops.diagram import measure_label
from svg_mcp.query import get_params
from svg_mcp.render import get_renderer
from svg_mcp.render.base import RenderRequest
from svg_mcp.serialize import export_svg
from svg_mcp.session import DocumentStore

FIXTURES = Path(__file__).parent / "fixtures" / "themes"

# What the bundled default gives `--pad-cell`, and the size the table arithmetic measures at.
PAD = 8.0
SIZE = 11.0


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


def _width(text: str) -> float:
    return measure_label(text, "sans-serif", SIZE)[0]


def _labels(doc: Document, node_id: str) -> list[str]:
    return [str(node.text) for node in _under(doc, node_id) if _tag(node) == "text"]


def _wearing(doc: Document, node_id: str, class_name: str) -> list[BaseElement]:
    return [
        node for node in _under(doc, node_id) if class_name in _classes(doc, str(node.get_id()))
    ]


def _box(node: BaseElement) -> tuple[float, float, float, float]:
    return (_num(node, "x"), _num(node, "y"), _num(node, "width"), _num(node, "height"))


def _one(doc: Document, node_id: str, class_name: str) -> BaseElement:
    found = _wearing(doc, node_id, class_name)
    assert len(found) == 1, f"expected one .{class_name}, found {len(found)}"
    return found[0]


def _transform(doc: Document, node_id: str) -> str:
    return str(doc.resolve(node_id).get("transform"))


def _rows() -> list[list[str]]:
    return [["ingest", "12"], ["router", "8"], ["writer", "40"]]


# --- 1. what a table refuses -------------------------------------------------


def test_a_ragged_table_is_refused_rather_than_padded() -> None:
    doc = _doc()
    with pytest.raises(InvalidArgument, match="same number of cells"):
        ops.add_table(doc, rows=[["a", "b"], ["c"]])


def test_a_header_that_does_not_match_the_rows_is_refused() -> None:
    doc = _doc()
    with pytest.raises(InvalidArgument, match="one entry per column"):
        ops.add_table(doc, rows=[["a", "b"]], header=["only one"])


def test_a_table_with_no_columns_at_all_is_refused() -> None:
    doc = _doc()
    with pytest.raises(InvalidArgument, match="at least one column"):
        ops.add_table(doc, rows=[])


def test_a_column_too_narrow_to_wrap_into_is_refused() -> None:
    doc = _doc()
    with pytest.raises(InvalidArgument, match="wrap text into"):
        ops.add_table(doc, rows=_rows(), max_col_width=4)


def test_an_alignment_list_of_the_wrong_length_is_refused() -> None:
    doc = _doc()
    with pytest.raises(InvalidArgument, match="one alignment per column"):
        ops.add_table(doc, rows=_rows(), col_align=["left"])


def test_an_alignment_nobody_can_draw_is_refused() -> None:
    doc = _doc()
    with pytest.raises(InvalidArgument, match="not a column alignment"):
        ops.add_table(doc, rows=_rows(), col_align=["left", "justified"])


def test_the_column_count_comes_from_the_header_when_there_are_no_rows() -> None:
    assert table_columns([], ["a", "b", "c"]) == 3


# --- 2. measurement ----------------------------------------------------------


def test_a_column_is_as_wide_as_its_widest_cell_plus_two_paddings() -> None:
    doc = _doc()
    rows = [["a", "x"], ["a much longer cell", "y"]]
    placed = ops.add_table(doc, rows=rows, header=["h", "second header"], x=20, y=20)
    first = max(_width(text) for text in ("h", "a", "a much longer cell"))
    second = max(_width(text) for text in ("second header", "x", "y"))
    assert placed.columns == [
        pytest.approx(first + 2 * PAD),
        pytest.approx(second + 2 * PAD),
    ]
    assert placed.w == pytest.approx(first + second + 4 * PAD)
    # ...and the border rect is exactly that wide, so the measurement is what got drawn.
    border = next(node for node in _under(doc, placed.ref.id) if _tag(node) == "rect")
    assert _num(border, "width") == pytest.approx(placed.w)


def test_a_column_stops_growing_at_max_col_width() -> None:
    doc = _doc()
    long_cell = "one two three four five six seven eight nine ten eleven twelve"
    assert _width(long_cell) > 120.0
    placed = ops.add_table(doc, rows=[[long_cell]], max_col_width=120, x=20, y=20)
    assert placed.columns == [pytest.approx(120.0 + 2 * PAD)]


def test_a_cell_that_wraps_makes_its_row_taller() -> None:
    doc = _doc()
    long_cell = "one two three four five six seven eight nine ten eleven twelve"
    wide = ops.add_table(doc, rows=[[long_cell]], max_col_width=400, x=20, y=20)
    narrow = ops.add_table(doc, rows=[[long_cell]], max_col_width=80, x=20, y=200)
    assert narrow.h > wide.h
    # The row grew because the CELL wrapped: one line became several, all of them drawn.
    assert len(_labels(doc, wide.ref.id)) == 1
    assert len(_labels(doc, narrow.ref.id)) > 1
    assert " ".join(_labels(doc, narrow.ref.id)) == long_cell


def test_a_row_is_as_tall_as_the_tallest_cell_in_it() -> None:
    doc = _doc()
    long_cell = "one two three four five six seven eight nine ten"
    placed = ops.add_table(
        doc, rows=[["short", long_cell], ["short", "short"]], max_col_width=60, x=20, y=20
    )
    stripes = _wearing(doc, placed.ref.id, "default-table-stripe")
    single = ops.add_table(doc, rows=[["short", "short"]], x=20, y=300)
    plain = _wearing(doc, single.ref.id, "default-table-stripe")
    assert _num(stripes[0], "height") > _num(plain[0], "height")


def test_the_cell_padding_can_be_overruled_without_touching_the_theme() -> None:
    doc = _doc()
    default = ops.add_table(doc, rows=[["a"]], x=20, y=20)
    padded = ops.add_table(doc, rows=[["a"]], x=20, y=100, x_pad=20)
    assert padded.columns[0] - default.columns[0] == pytest.approx(2 * (20.0 - PAD))
    with pytest.raises(InvalidArgument, match="cannot be negative"):
        ops.add_table(doc, rows=[["a"]], x_pad=-1)


# --- 3. alignment ------------------------------------------------------------


@pytest.mark.parametrize(
    "cell", ["12", "-3", "4.5", "1,200", "$14", "45%", "0", " 7 ", "1.2e3", "$1,204.50"]
)
def test_things_that_read_as_a_number(cell: str) -> None:
    assert _is_number(cell)


@pytest.mark.parametrize("cell", ["", "  ", "n/a", "12 requests", "one", "1.2.3", "-", "%"])
def test_things_that_do_not(cell: str) -> None:
    assert not _is_number(cell)


def test_a_column_of_numbers_right_aligns_itself_and_a_mixed_one_does_not() -> None:
    doc = _doc()
    placed = ops.add_table(
        doc,
        rows=[["ingest", "1,204", "3 hits"], ["router", "982", "many"]],
        header=["service", "requests", "notes"],
        x=20,
        y=20,
    )
    assert placed.col_align == ["left", "right", "left"]
    # The HEADER is not consulted: a numeric column under a word is still numeric.
    assert resolve_col_align([["1"], ["2"]], 1, None) == ("right",)
    assert resolve_col_align([["1"], ["x"]], 1, None) == ("left",)
    # An empty table body has nothing to measure, so nothing is claimed about it.
    assert resolve_col_align([], 2, None) == ("left", "left")


def test_an_explicit_alignment_overrules_what_the_column_holds() -> None:
    doc = _doc()
    placed = ops.add_table(
        doc, rows=[["a", "1"], ["b", "2"]], col_align=["center", "left"], x=20, y=20
    )
    assert placed.col_align == ["center", "left"]
    anchors = {
        str(node.style.get("text-anchor"))
        for node in _under(doc, placed.ref.id)
        if _tag(node) == "text"
    }
    assert anchors == {"middle", "start"}


def test_a_right_aligned_column_hangs_its_text_off_the_columns_right_edge() -> None:
    doc = _doc()
    placed = ops.add_table(doc, rows=[["a", "1"], ["b", "2"]], x=20, y=20)
    ends = [
        _num(node, "x")
        for node in _under(doc, placed.ref.id)
        if _tag(node) == "text" and str(node.style.get("text-anchor")) == "end"
    ]
    assert ends == [pytest.approx(placed.w - PAD, abs=0.01)] * 2


# --- 4. anatomy --------------------------------------------------------------


def test_zebra_stripes_land_on_the_even_body_rows_and_nowhere_else() -> None:
    doc = _doc()
    placed = ops.add_table(doc, rows=_rows(), header=["a", "b"], x=20, y=20)
    header = _one(doc, placed.ref.id, "default-table-header")
    stripes = _wearing(doc, placed.ref.id, "default-table-stripe")
    assert len(stripes) == 2  # rows 0 and 2 of three
    row_h = _num(stripes[0], "height")
    body_top = _num(header, "y") + _num(header, "height")
    assert _num(stripes[0], "y") == pytest.approx(body_top)
    assert _num(stripes[1], "y") == pytest.approx(body_top + 2 * row_h)
    # ...and they run the whole width, so a wide row is led across rather than half-led.
    assert _num(stripes[0], "width") == pytest.approx(placed.w)


def test_zebra_can_be_turned_off_entirely() -> None:
    doc = _doc()
    placed = ops.add_table(doc, rows=_rows(), header=["a", "b"], zebra=False, x=20, y=20)
    assert _wearing(doc, placed.ref.id, "default-table-stripe") == []


def test_a_header_brings_a_wash_and_a_rule_under_it_and_no_header_brings_neither() -> None:
    doc = _doc()
    titled = ops.add_table(doc, rows=_rows(), header=["service", "count"], x=20, y=20)
    header = _one(doc, titled.ref.id, "default-table-header")
    rule = _one(doc, titled.ref.id, "default-table-rule")
    assert _tag(rule) == "line"
    assert _num(rule, "y1") == pytest.approx(_num(header, "y") + _num(header, "height"))
    assert _num(rule, "x1") == 0.0
    assert _num(rule, "x2") == pytest.approx(titled.w, abs=0.01)

    bare = ops.add_table(doc, rows=_rows(), x=20, y=300)
    assert _wearing(doc, bare.ref.id, "default-table-header") == []
    assert _wearing(doc, bare.ref.id, "default-table-rule") == []
    assert titled.h > bare.h  # the header row is real height, not a repaint


def test_a_title_sits_above_the_table_and_is_set_like_a_charts() -> None:
    doc = _doc()
    bare = ops.add_table(doc, rows=_rows(), x=20, y=20)
    titled = ops.add_table(doc, rows=_rows(), title="Last hour", x=20, y=200)
    assert titled.h > bare.h
    title = _one(doc, titled.ref.id, "default-chart-title")
    border = next(node for node in _under(doc, titled.ref.id) if _tag(node) == "rect")
    assert str(title.text) == "Last hour"
    assert _num(title, "y") < _num(border, "y")  # above the panel, not inside it


def test_the_header_text_and_the_body_text_wear_different_classes() -> None:
    doc = _doc()
    placed = ops.add_table(doc, rows=[["a", "b"]], header=["h", "i"], x=20, y=20)
    assert len(_wearing(doc, placed.ref.id, "default-table-header-text")) == 2
    assert len(_wearing(doc, placed.ref.id, "default-table-cell")) == 2


def test_an_empty_cell_draws_nothing_at_all() -> None:
    doc = _doc()
    placed = ops.add_table(doc, rows=[["a", ""], ["", "b"]], x=20, y=20)
    assert _labels(doc, placed.ref.id) == ["a", "b"]
    assert placed.h > 0  # ...but the row it is in still has its height


# --- 5. placement and editing ------------------------------------------------


def test_an_auto_placed_table_stacks_under_what_is_already_there() -> None:
    doc = _doc()
    node = ops.add_diagram_node(doc, kind="service", label="API", x=20, y=20, width=80, height=40)
    table = ops.add_table(doc, rows=_rows())
    assert table.y > node.y + node.h
    assert table.auto is True
    # ...and the next facade stacks under the TABLE, not back under the node.
    following = ops.add_diagram_node(doc, kind="service", label="Next")
    assert following.y > table.y


def test_a_rebuild_keeps_the_group_its_id_and_its_position() -> None:
    doc = _doc()
    placed = ops.add_table(doc, rows=[["a"], ["b"]], header=["h"], x=37, y=41)
    before_children = len(list(doc.resolve(placed.ref.id)))
    before_transform = _transform(doc, placed.ref.id)
    result = ops.edit_table(doc, placed.ref.id, rows=[["a"], ["b"], ["c"], ["d"]])
    assert result.ref.id == placed.ref.id
    assert _transform(doc, placed.ref.id) == before_transform
    assert result.children > before_children
    assert _labels(doc, placed.ref.id)[-1] == "d"


def test_a_rebuild_leaves_exactly_one_generation_of_children_behind() -> None:
    doc = _doc()
    placed = ops.add_table(doc, rows=_rows(), header=["a", "b"], x=20, y=20)
    first = len(_under(doc, placed.ref.id))
    ops.edit_table(doc, placed.ref.id, title="Key")
    ops.edit_table(doc, placed.ref.id, title="")
    assert len(_under(doc, placed.ref.id)) == first


def test_an_edit_can_take_the_header_away_and_turn_the_zebra_off() -> None:
    doc = _doc()
    placed = ops.add_table(doc, rows=_rows(), header=["a", "b"], x=20, y=20)
    ops.edit_table(doc, placed.ref.id, header=[], zebra=False)
    spec = read_table_spec(doc.resolve(placed.ref.id))
    assert spec is not None and spec.header is None and spec.zebra is False
    assert _wearing(doc, placed.ref.id, "default-table-rule") == []
    assert _wearing(doc, placed.ref.id, "default-table-stripe") == []


def test_new_rows_do_not_silently_re_align_a_column_somebody_set() -> None:
    doc = _doc()
    placed = ops.add_table(doc, rows=[["a"], ["b"]], col_align=["center"], x=20, y=20)
    kept = ops.edit_table(doc, placed.ref.id, rows=[["1"], ["2"]])
    assert kept.col_align == ["center"]  # numbers now, but the alignment was a decision
    moved = ops.edit_table(doc, placed.ref.id, rows=[["1"], ["2"]], col_align=["right"])
    assert moved.col_align == ["right"]


def test_a_column_count_that_changes_re_derives_the_alignments() -> None:
    doc = _doc()
    placed = ops.add_table(doc, rows=[["a"], ["b"]], x=20, y=20)
    result = ops.edit_table(doc, placed.ref.id, rows=[["a", "1"], ["b", "2"]])
    assert result.col_align == ["left", "right"]


def test_an_edit_re_validates_the_shape_it_is_given() -> None:
    doc = _doc()
    placed = ops.add_table(doc, rows=_rows(), header=["a", "b"], x=20, y=20)
    with pytest.raises(InvalidArgument, match="same number of cells"):
        ops.edit_table(doc, placed.ref.id, rows=[["a", "b"], ["c"]])
    with pytest.raises(InvalidArgument, match="one entry per column"):
        ops.edit_table(doc, placed.ref.id, header=["a", "b", "c"])


def test_editing_something_that_is_not_a_table_is_refused() -> None:
    doc = _doc()
    rect = ops.add_rect(doc, x=0, y=0, width=10, height=10)
    with pytest.raises(InvalidArgument, match="is not a table"):
        ops.edit_table(doc, rect.id, title="nope")


# --- 6. which theme paints it ------------------------------------------------


def test_a_themeless_document_still_gets_a_styled_table() -> None:
    doc = _doc()
    assert doc.theme_meta == {}  # nothing loaded, nothing routed
    placed = ops.add_table(doc, rows=_rows(), header=["a", "b"], title="Key", x=20, y=20)
    assert _classes(doc, placed.ref.id) == ["default-table"]
    svg = export_svg(doc)
    for rule in (".default-table", ".default-table-header", ".default-table-stripe"):
        assert rule in svg


def test_an_unthemed_table_wears_nothing_and_stays_that_way_across_an_edit() -> None:
    doc = _doc()
    placed = ops.add_table(doc, rows=_rows(), header=["a", "b"], x=20, y=20, themed=False)
    worn = [cls for node in _under(doc, placed.ref.id) for cls in _classes(doc, str(node.get_id()))]
    assert worn == []
    ops.edit_table(doc, placed.ref.id, title="Key")
    worn = [cls for node in _under(doc, placed.ref.id) for cls in _classes(doc, str(node.get_id()))]
    assert worn == []


def test_get_params_hands_back_the_spec_a_table_was_built_from() -> None:
    doc = _doc()
    placed = ops.add_table(
        doc, rows=[["a", "1"], ["b", "2"]], header=["h", "n"], title="Key", x=20, y=20
    )
    reported = get_params(doc, placed.ref.id)
    assert reported["kind"] == "table"
    assert reported["parametric"] is True
    params = reported["params"]
    assert isinstance(params, dict)
    assert params["rows"] == [["a", "1"], ["b", "2"]]
    assert params["header"] == ["h", "n"]
    assert (params["title"], params["zebra"], params["auto"]) == ("Key", True, False)
    assert params["col_align"] == ["left", "right"]
    assert (params["max_col_width"], params["x_pad"]) == (220.0, None)


def test_a_headerless_table_reports_no_header_rather_than_a_broken_spec() -> None:
    doc = _doc()
    placed = ops.add_table(doc, rows=[["a"]], x=20, y=20)
    params = get_params(doc, placed.ref.id)["params"]
    assert isinstance(params, dict)
    assert params["header"] == []


# --- 7. callout cards: what the words decide ---------------------------------


def _body() -> str:
    return "the write path is not idempotent yet, so a retry writes the row a second time"


def test_a_cards_height_is_derived_from_what_its_body_wrapped_to() -> None:
    doc = _doc()
    wide = ops.add_callout_card(doc, title="Careful", body=_body(), width=320, x=20, y=20)
    narrow = ops.add_callout_card(doc, title="Careful", body=_body(), width=140, x=20, y=200)
    assert narrow.lines > wide.lines
    assert narrow.h > wide.h
    assert (wide.w, narrow.w) == (320.0, 140.0)


def test_a_card_with_no_body_is_a_title_and_is_shorter_for_it() -> None:
    doc = _doc()
    full = ops.add_callout_card(doc, title="Reaper", body="idle since friday", x=20, y=20)
    bare = ops.add_callout_card(doc, title="Reaper", x=20, y=200)
    assert bare.lines == 0
    assert bare.h < full.h
    assert _labels(doc, bare.ref.id) == ["Reaper"]


def test_a_card_with_nothing_to_say_is_refused() -> None:
    doc = _doc()
    with pytest.raises(InvalidArgument, match="empty rectangle"):
        ops.add_callout_card(doc, title="   ", body="")


def test_a_card_too_narrow_for_its_own_padding_is_refused() -> None:
    doc = _doc()
    with pytest.raises(InvalidArgument, match="at least 60"):
        ops.add_callout_card(doc, title="hi", width=20)


def test_the_accent_bar_is_flush_with_the_left_edge_and_four_units_wide() -> None:
    doc = _doc()
    placed = ops.add_callout_card(doc, title="Careful", body=_body(), x=20, y=20)
    accent = _one(doc, placed.ref.id, "default-card-accent")
    assert _box(accent)[:3] == (0.0, 0.0, 4.0)
    assert _num(accent, "height") == pytest.approx(placed.h, abs=0.01)
    # It is drawn AFTER the card, so it covers the border it sits on rather than under it.
    rects = [node for node in _under(doc, placed.ref.id) if _tag(node) == "rect"]
    assert rects[0].get("class") is None and rects[1] is accent


def test_a_card_carries_no_leader_at_all() -> None:
    doc = _doc()
    placed = ops.add_callout_card(doc, title="Careful", body=_body(), x=20, y=20)
    assert [_tag(node) for node in _under(doc, placed.ref.id) if _tag(node) == "line"] == []
    assert ops.reflow(doc).callouts_reanchored == 0


@pytest.mark.parametrize("kind", ["note", "info", "warning", "success", "danger"])
def test_every_card_kind_hooks_a_class_of_its_own(kind: str) -> None:
    doc = _doc()
    placed = ops.add_callout_card(doc, title="hello", kind=kind, x=20, y=20)
    assert _classes(doc, placed.ref.id) == [f"default-{kind}"]
    assert f".default-{kind} .default-card-accent" in export_svg(doc)


def test_a_kind_is_any_role_a_resident_theme_serves() -> None:
    doc = _doc()
    ops.load_theme(doc, "atlas", search_paths=[FIXTURES])
    placed = ops.add_callout_card(doc, title="atlas says", kind="flow", x=20, y=20)
    assert _classes(doc, placed.ref.id) == ["atlas-flow"]
    # ...and a kind NOTHING serves is refused rather than drawn unpainted.
    with pytest.raises(InvalidArgument, match="role 'nonesuch'"):
        ops.add_callout_card(doc, title="?", kind="nonesuch", x=20, y=200)


def test_changing_the_kind_swaps_the_class_and_leaves_the_card_where_it_is() -> None:
    doc = _doc()
    placed = ops.add_callout_card(doc, title="Careful", body=_body(), kind="info", x=20, y=20)
    before = _transform(doc, placed.ref.id)
    ops.edit_callout_card(doc, placed.ref.id, kind="danger")
    assert _classes(doc, placed.ref.id) == ["default-danger"]
    assert _transform(doc, placed.ref.id) == before
    spec = read_card_spec(doc.resolve(placed.ref.id))
    assert spec is not None and spec.kind == "danger"


def test_new_words_are_re_wrapped_and_the_card_re_sized_around_them() -> None:
    doc = _doc()
    placed = ops.add_callout_card(doc, title="Careful", body="short", width=200, x=20, y=20)
    result = ops.edit_callout_card(doc, placed.ref.id, body=_body())
    assert result.lines > placed.lines
    assert result.h > placed.h
    accent = _one(doc, placed.ref.id, "default-card-accent")
    assert _num(accent, "height") == pytest.approx(result.h)  # the accent grew with the card


def test_an_edit_that_would_empty_a_card_is_refused() -> None:
    doc = _doc()
    placed = ops.add_callout_card(doc, title="Careful", x=20, y=20)
    with pytest.raises(InvalidArgument, match="empty rectangle"):
        ops.edit_callout_card(doc, placed.ref.id, title="")


def test_editing_something_that_is_not_a_card_is_refused() -> None:
    doc = _doc()
    rect = ops.add_rect(doc, x=0, y=0, width=10, height=10)
    with pytest.raises(InvalidArgument, match="is not a callout card"):
        ops.edit_callout_card(doc, rect.id, title="nope")


def test_an_auto_placed_card_stacks_under_what_is_already_there() -> None:
    doc = _doc()
    table = ops.add_table(doc, rows=_rows(), x=20, y=20)
    card = ops.add_callout_card(doc, title="about the table above", body=_body())
    assert card.y > table.y + table.h
    assert card.auto is True


def test_get_params_hands_back_the_spec_a_card_was_built_from() -> None:
    doc = _doc()
    placed = ops.add_callout_card(
        doc, title="Careful", body=_body(), kind="warning", width=260, x=20, y=20
    )
    reported = get_params(doc, placed.ref.id)
    assert reported["kind"] == "callout_card"
    params = reported["params"]
    assert isinstance(params, dict)
    assert (params["title"], params["body"]) == ("Careful", _body())
    assert (params["kind"], params["width"], params["auto"]) == ("warning", 260.0, False)


# --- 8. what it actually renders ---------------------------------------------


def _render(doc: Document) -> object:
    renderer = get_renderer()
    if not renderer.available():
        pytest.skip("no resvg backend available")
    from PIL import Image

    return Image.open(io.BytesIO(renderer.render(RenderRequest(svg=export_svg(doc))).png)).convert(
        "RGBA"
    )


def _pixel(image: object, at: tuple[float, float]) -> tuple[int, int, int, int]:
    pixel = image.getpixel((int(at[0]), int(at[1])))  # type: ignore[attr-defined]
    assert isinstance(pixel, tuple)
    return (int(pixel[0]), int(pixel[1]), int(pixel[2]), int(pixel[3]))


def _grid(doc: Document, placed: ops.PlacedTable) -> dict[str, tuple[float, float]]:
    """Three sample points, in page coordinates: the header wash, a stripe, and a bare row.

    Each is taken near the RIGHT edge of the table, inside the border and inside the padding that
    every left-aligned column keeps clear — so what is under the sample is the wash and nothing
    else.
    """
    header = _one(doc, placed.ref.id, "default-table-header")
    stripes = _wearing(doc, placed.ref.id, "default-table-stripe")
    at_x = placed.x + placed.w - 4.0
    row_h = _num(stripes[0], "height")
    return {
        "header": (at_x, placed.y + _num(header, "y") + _num(header, "height") / 2.0),
        "stripe": (at_x, placed.y + _num(stripes[0], "y") + row_h / 2.0),
        "bare": (at_x, placed.y + _num(stripes[0], "y") + row_h * 1.5),
    }


def test_a_tables_header_its_stripes_and_its_canvas_are_three_different_grounds() -> None:
    doc = _doc()
    placed = ops.add_table(
        doc,
        rows=[["ingest", "hot", "yes"], ["router", "warm", "no"], ["writer", "cold", "yes"]],
        header=["service", "state", "paged"],
        x=40,
        y=40,
    )
    image = _render(doc)
    at = _grid(doc, placed)
    canvas = _pixel(image, at["bare"])
    stripe = _pixel(image, at["stripe"])
    header = _pixel(image, at["header"])
    assert canvas[:3] == (0xFF, 0xFF, 0xFF)  # the table's own --canvas ground
    assert stripe[:3] != canvas[:3], "a zebra stripe that renders as canvas is not a stripe"
    assert header[:3] != canvas[:3]
    assert header[:3] != stripe[:3], "the header has to outrank the zebra under it"
    # Both are washes of the ink, so both are DARKER than the ground, and the header more so.
    assert header[0] < stripe[0] < canvas[0]


def test_an_info_cards_accent_renders_in_the_kinds_stroke_colour() -> None:
    doc = _doc()
    placed = ops.add_callout_card(doc, title="Careful", body=_body(), kind="info", x=40, y=40)
    image = _render(doc)
    assert _pixel(image, (placed.x + 2, placed.y + placed.h / 2.0))[:3] == (0x3F, 0x7C, 0xC0)
    # ...and the card beside it is the info WASH, not the accent: the bar is an edge, not a fill.
    assert _pixel(image, (placed.x + 12, placed.y + placed.h - 4))[:3] == (0xE6, 0xF0, 0xFB)


def test_the_dark_variant_repaints_both_the_stripes_and_the_accent() -> None:
    doc = _doc()
    table = ops.add_table(
        doc,
        rows=[["ingest", "hot"], ["router", "warm"], ["writer", "cold"]],
        header=["service", "state"],
        x=40,
        y=40,
    )
    card = ops.add_callout_card(doc, title="Careful", body=_body(), kind="info", x=300, y=40)
    at = _grid(doc, table)
    accent = (card.x + 2, card.y + card.h / 2.0)

    light = _render(doc)
    light_stripe = _pixel(light, at["stripe"])
    light_accent = _pixel(light, accent)
    ops.set_theme_variant(doc, "dark")
    dark = _render(doc)

    assert _pixel(dark, at["stripe"])[:3] != light_stripe[:3]
    assert _pixel(dark, accent)[:3] != light_accent[:3]
    assert _pixel(dark, accent)[:3] == (0x6E, 0xA8, 0xE0)  # --stroke-info, dark
    # The stripe is still a wash of ink over the canvas, so on a dark ground it is now LIGHTER.
    assert _pixel(dark, at["stripe"])[0] > _pixel(dark, at["bare"])[0]
    # ...and nothing moved: the same sample point is still inside the same stripe.
    assert _grid(doc, table)["stripe"] == at["stripe"]
