"""Chart facades: the pure scales first, then the schemas, then the facade built on both."""

from __future__ import annotations

import io
import math
from pathlib import Path

import pytest
from pydantic import ValidationError

from svg_mcp import ops
from svg_mcp.model import Document
from svg_mcp.model.errors import InvalidArgument
from svg_mcp.ops.chart import (
    BarData,
    DonutData,
    LineData,
    PointSeries,
    ScatterData,
    Series,
    Slice,
    SparklineData,
    bar_bands,
    donut_angles,
    include_zero,
    nice_step,
    nice_ticks,
    plot_margins,
    read_chart_spec,
    tick_text,
)
from svg_mcp.query import get_params
from svg_mcp.render import get_renderer
from svg_mcp.render.base import RenderRequest
from svg_mcp.serialize import export_svg
from svg_mcp.session import DocumentStore

FIXTURES = Path(__file__).parent / "fixtures" / "themes"


def _doc() -> Document:
    return DocumentStore().create(600, 400)[1]


def _classes(doc: Document, node_id: str) -> list[str]:
    return str(doc.resolve(node_id).get("class") or "").split()


def _kids(doc: Document, node_id: str) -> list[object]:
    return [child for child in doc.resolve(node_id) if isinstance(child.tag, str)]


def _tags_under(doc: Document, node_id: str) -> list[str]:
    return [
        str(node.tag).rsplit("}", 1)[-1]
        for node in doc.resolve(node_id).iter()
        if isinstance(node.tag, str)
    ]


# --- 1. the scales, with no document in sight -------------------------------


def test_the_step_ladder_only_ever_offers_one_two_or_five() -> None:
    for raw in (0.03, 0.4, 1.1, 3.0, 7.0, 14.6, 260.0, 90000.0):
        step = nice_step(raw)
        mantissa = step / 10 ** math.floor(math.log10(step))
        assert round(mantissa, 6) in (1.0, 2.0, 5.0)
        assert step >= raw


def test_a_zero_to_seventy_three_axis_runs_zero_to_eighty_by_twenties() -> None:
    assert nice_ticks(0.0, 73.0) == [0.0, 20.0, 40.0, 60.0, 80.0]


def test_ticks_always_cover_the_data_they_are_drawn_for() -> None:
    for lo, hi in ((0.0, 73.0), (3.0, 4.0), (-17.0, 6.0), (0.004, 0.031), (120.0, 9800.0)):
        ticks = nice_ticks(lo, hi)
        assert ticks[0] <= lo and ticks[-1] >= hi
        assert len(ticks) >= 3


def test_a_negative_span_ticks_the_same_way_a_positive_one_does() -> None:
    assert nice_ticks(-73.0, 0.0) == [-80.0, -60.0, -40.0, -20.0, 0.0]
    assert nice_ticks(-30.0, 45.0) == [-40.0, -20.0, 0.0, 20.0, 40.0, 60.0]


def test_a_flat_series_is_padded_rather_than_divided_by_zero() -> None:
    ticks = nice_ticks(5.0, 5.0)
    assert ticks[0] < 5.0 < ticks[-1]
    zero = nice_ticks(0.0, 0.0)
    assert zero[0] < 0.0 < zero[-1]


def test_a_reversed_span_is_taken_as_written_the_right_way_round() -> None:
    assert nice_ticks(80.0, 0.0) == nice_ticks(0.0, 80.0)


def test_bars_include_zero_before_they_are_ticked() -> None:
    # 40..73 alone would start the axis at 40, which makes a bar of 41 look like nothing.
    assert include_zero(40.0, 73.0) == (0.0, 73.0)
    assert include_zero(-12.0, -3.0) == (-12.0, 0.0)
    assert nice_ticks(*include_zero(40.0, 73.0))[0] == 0.0


def test_tick_text_writes_a_step_at_its_own_precision() -> None:
    assert tick_text(20.0, 0) == "20"
    assert tick_text(0.5, 1) == "0.5"
    assert tick_text(-0.0, 1) == "0"


def test_bands_split_a_category_seventy_thirty_and_share_the_rest_evenly() -> None:
    bands = bar_bands(300.0, 3, 2)
    assert [len(row) for row in bands] == [2, 2, 2]
    # band 100 wide, bars fill 70 of it, so 15 of clear air on each side and 35 per bar.
    assert (bands[0][0].x, bands[0][0].w) == (15.0, 35.0)
    assert (bands[0][1].x, bands[0][1].w) == (50.0, 35.0)
    assert (bands[2][1].x, bands[2][1].w) == (250.0, 35.0)


def test_one_series_still_leaves_the_same_gap_between_categories() -> None:
    bands = bar_bands(300.0, 3, 1)
    assert (bands[0][0].x, bands[0][0].w) == (15.0, 70.0)
    assert bands[1][0].x - (bands[0][0].x + bands[0][0].w) == pytest.approx(30.0)


def test_donut_slices_tile_the_whole_turn_exactly() -> None:
    angles = donut_angles([45.0, 30.0, 15.0, 10.0])
    assert sum(end - start for start, end in angles) == pytest.approx(math.tau)
    assert angles[0][0] == pytest.approx(-math.pi / 2.0)
    for (_, end), (start, _) in zip(angles, angles[1:], strict=False):
        assert end == pytest.approx(start)  # no seam between two slices
    assert angles[-1][1] == pytest.approx(-math.pi / 2.0 + math.tau)


def test_a_lone_slice_covers_the_turn_and_a_zero_total_is_refused() -> None:
    assert donut_angles([7.0])[0] == pytest.approx((-math.pi / 2.0, -math.pi / 2.0 + math.tau))
    with pytest.raises(InvalidArgument):
        donut_angles([])


def test_wider_tick_labels_buy_themselves_a_wider_left_margin() -> None:
    narrow = plot_margins(
        ["0", "500"], font="sans-serif", title=False, x_label=False, y_label=False
    )
    wide = plot_margins(
        ["0", "500000"], font="sans-serif", title=False, x_label=False, y_label=False
    )
    assert wide.left > narrow.left
    assert (wide.top, wide.right, wide.bottom) == (narrow.top, narrow.right, narrow.bottom)


def test_a_title_and_axis_titles_each_claim_their_own_side() -> None:
    bare = plot_margins(["0"], font="sans-serif", title=False, x_label=False, y_label=False)
    full = plot_margins(["0"], font="sans-serif", title=True, x_label=True, y_label=True)
    assert full.top > bare.top
    assert full.bottom > bare.bottom
    assert full.left > bare.left


# --- 2. the data schemas ----------------------------------------------------


def test_a_bar_series_must_carry_one_value_per_category() -> None:
    with pytest.raises(ValidationError, match="one per category"):
        BarData(categories=["a", "b", "c"], series=[Series(name="s", values=[1.0, 2.0])])


def test_a_slice_with_no_value_has_no_angle_and_is_refused() -> None:
    with pytest.raises(ValidationError):
        Slice(label="nothing", value=0.0)
    with pytest.raises(ValidationError):
        DonutData(slices=[Slice(label="a", value=1.0), Slice(label="b", value=-2.0)])


def test_an_unknown_key_is_rejected_rather_than_dropped() -> None:
    with pytest.raises(ValidationError, match="bogus"):
        BarData.model_validate(
            {"categories": ["a"], "series": [{"name": "s", "values": [1]}], "bogus": 1}
        )
    with pytest.raises(ValidationError, match="colour"):
        SparklineData.model_validate({"values": [1, 2], "colour": "red"})


def test_an_empty_chart_has_nothing_to_draw_and_says_so() -> None:
    with pytest.raises(ValidationError):
        BarData(categories=[], series=[])
    with pytest.raises(ValidationError):
        SparklineData(values=[1.0])


def test_a_bare_series_list_moves_between_line_and_scatter_but_an_option_does_not() -> None:
    doc = _doc()
    points = [PointSeries(name="s", points=[(0.0, 1.0), (1.0, 4.0)])]
    ops.add_chart(doc, kind="scatter", data=LineData(series=points))  # the same payload, really
    with pytest.raises(InvalidArgument, match="does not fit a scatter chart"):
        ops.add_chart(doc, kind="scatter", data=LineData(series=points, area=True))


def test_a_scatter_draws_a_mark_per_point_and_never_joins_them() -> None:
    doc = _doc()
    placed = ops.add_chart(
        doc,
        kind="scatter",
        data=ScatterData(
            series=[
                PointSeries(name="a", points=[(1, 2), (2, 5), (3, 4)]),
                PointSeries(name="b", points=[(1, 9), (2, 7)]),
            ]
        ),
    )
    tags = _tags_under(doc, placed.ref.id)
    assert tags.count("circle") == 5
    assert "polyline" not in tags


# --- 3. the facade ----------------------------------------------------------


def _bar_doc(doc: Document) -> ops.PlacedChart:
    return ops.add_chart(
        doc,
        kind="bar",
        data=BarData(
            categories=["a", "b", "c"],
            series=[
                Series(name="one", values=[10, 42, 73]),
                Series(name="two", values=[5, 60, 20]),
            ],
        ),
        x=0,
        y=0,
    )


def test_a_grouped_bar_chart_draws_one_rect_per_category_per_series() -> None:
    doc = _doc()
    placed = _bar_doc(doc)
    rects = [tag for tag in _tags_under(doc, placed.ref.id) if tag == "rect"]
    assert len(rects) == 3 * 2


def test_the_bars_of_one_category_sit_side_by_side_inside_its_band() -> None:
    doc = _doc()
    placed = _bar_doc(doc)
    group = doc.resolve(placed.ref.id)
    series = [child for child in group if child.get("class", "").startswith("default-series")]
    first, second = (
        [float(rect.get("x")) for rect in child if str(rect.tag).endswith("rect")]
        for child in series
    )
    widths = [
        float(rect.get("width")) for rect in series[0] if str(rect.tag).endswith("rect")
    ]
    # Two bars per band, touching: the second starts exactly where the first ends.
    assert second[0] == pytest.approx(first[0] + widths[0])
    # ...and the bands themselves are evenly spaced.
    assert first[1] - first[0] == pytest.approx(first[2] - first[1])


def test_a_line_chart_draws_a_wash_a_line_and_a_marker_per_point() -> None:
    doc = _doc()
    placed = ops.add_chart(
        doc,
        kind="line",
        data=LineData(
            series=[PointSeries(name="s", points=[(0, 1), (1, 4), (2, 2), (3, 6)])],
            points=True,
            area=True,
        ),
    )
    tags = _tags_under(doc, placed.ref.id)
    assert tags.count("polyline") == 1
    assert tags.count("path") == 1  # the area wash
    assert tags.count("circle") == 4  # one marker per point


def test_a_plain_line_chart_draws_neither_wash_nor_markers() -> None:
    doc = _doc()
    placed = ops.add_chart(
        doc,
        kind="line",
        data=LineData(series=[PointSeries(name="s", points=[(0, 1), (1, 4), (2, 2)])]),
    )
    tags = _tags_under(doc, placed.ref.id)
    assert tags.count("polyline") == 1
    assert "path" not in tags
    assert "circle" not in tags


def test_a_donut_draws_one_annular_sector_per_slice_over_the_full_turn() -> None:
    doc = _doc()
    slices = [Slice(label="a", value=45), Slice(label="b", value=30), Slice(label="c", value=25)]
    placed = ops.add_chart(doc, kind="donut", data=DonutData(slices=slices))
    paths = [
        node
        for node in doc.resolve(placed.ref.id).iter()
        if isinstance(node.tag, str) and str(node.tag).endswith("path")
    ]
    assert len(paths) == 3
    spans = donut_angles([piece.value for piece in slices])
    assert sum(end - start for start, end in spans) == pytest.approx(math.tau)
    # Every wedge is a closed ring segment: out, round, in, back round, close.
    for path in paths:
        d = str(path.get("d"))
        assert d.startswith("M ") and d.endswith("Z")
        assert d.count("A ") == 2 and d.count("L ") == 1


def test_a_sparkline_is_the_line_and_nothing_else() -> None:
    doc = _doc()
    placed = ops.add_chart(doc, kind="sparkline", data=SparklineData(values=[3, 5, 4, 8, 6]))
    assert len(_kids(doc, placed.ref.id)) == 1
    tags = _tags_under(doc, placed.ref.id)
    assert tags.count("polyline") == 1
    for absent in ("line", "text", "rect"):
        assert absent not in tags
    assert (placed.w, placed.h) == (120.0, 32.0)


def test_a_sparkline_ignores_a_title_it_has_nowhere_to_put() -> None:
    doc = _doc()
    placed = ops.add_chart(
        doc, kind="sparkline", data=SparklineData(values=[1, 2, 3]), title="nope"
    )
    assert "text" not in _tags_under(doc, placed.ref.id)
    spec = read_chart_spec(doc.resolve(placed.ref.id))
    assert spec is not None and spec.title == "nope"  # kept in the spec, simply not drawn


def _axis_x(doc: Document, chart_id: str) -> float:
    axis = next(
        node
        for node in doc.resolve(chart_id).iter()
        if isinstance(node.tag, str) and "default-axis" in str(node.get("class") or "")
    )
    return float(axis.get("x1"))


def test_six_digit_values_indent_the_plot_further_than_three_digit_ones() -> None:
    doc = _doc()
    small = ops.add_chart(
        doc,
        kind="bar",
        data=BarData(categories=["a", "b"], series=[Series(name="s", values=[100, 500])]),
    )
    big = ops.add_chart(
        doc,
        kind="bar",
        data=BarData(categories=["a", "b"], series=[Series(name="s", values=[100000, 500000])]),
    )
    assert _axis_x(doc, big.ref.id) > _axis_x(doc, small.ref.id)


def test_the_series_palette_cycles_after_eight() -> None:
    doc = _doc()
    placed = ops.add_chart(
        doc,
        kind="line",
        data=LineData(
            series=[
                PointSeries(name=f"s{index}", points=[(0.0, float(index)), (1.0, 2.0)])
                for index in range(10)
            ]
        ),
    )
    worn = [
        str(child.get("class"))
        for child in doc.resolve(placed.ref.id)
        if str(child.get("class") or "").startswith("default-series")
    ]
    assert worn[:8] == [f"default-series-{n}" for n in range(1, 9)]
    assert worn[8:] == ["default-series-1", "default-series-2"]


def test_a_themeless_document_still_gets_a_styled_chart() -> None:
    doc = _doc()
    assert doc.theme_meta == {}  # nothing loaded, nothing routed
    placed = _bar_doc(doc)
    assert _classes(doc, placed.ref.id) == ["default-chart", "default-chart--bar"]
    svg = export_svg(doc)
    assert ".default-series-1" in svg
    assert ".default-axis" in svg


def test_the_category_fallback_does_not_leak_into_plain_primitives() -> None:
    # The widening is for `chart` alone: a bare rect in a themeless document stays a bare rect.
    doc = _doc()
    rect = ops.add_rect(doc, x=0, y=0, width=10, height=10)
    assert _classes(doc, rect.id) == []
    assert doc.theme_meta == {}


def test_an_unthemed_chart_wears_nothing_and_stays_that_way_across_an_edit() -> None:
    doc = _doc()
    placed = ops.add_chart(
        doc,
        kind="bar",
        data=BarData(categories=["a"], series=[Series(name="s", values=[1])]),
        themed=False,
    )
    assert _classes(doc, placed.ref.id) == []
    ops.edit_chart(doc, placed.ref.id, title="still bare")
    assert _classes(doc, placed.ref.id) == []
    assert "default-series-1" not in export_svg(doc).split("<style")[-1]


def test_a_second_theme_pasted_over_a_chart_translates_every_part_it_can() -> None:
    doc = _doc()
    placed = _bar_doc(doc)
    ops.apply_theme(doc, placed.ref.id, "drafting", search_paths=[FIXTURES])
    assert "drafting-chart" in _classes(doc, placed.ref.id)
    series = [
        _classes(doc, str(child.get_id()))
        for child in doc.resolve(placed.ref.id)
        if str(child.get("class") or "").startswith("default-series")
    ]
    assert series[0] == ["default-series-1", "drafting-series-1"]
    assert series[1] == ["default-series-2", "drafting-series-2"]
    axis = next(
        node
        for node in doc.resolve(placed.ref.id).iter()
        if "default-axis" in str(node.get("class") or "")
    )
    assert "drafting-axis" in _classes(doc, str(axis.get_id()))


def test_a_chart_routes_the_whole_category_to_a_theme_that_asks_for_it() -> None:
    doc = _doc()
    ops.load_theme(doc, "drafting", search_paths=[FIXTURES])
    assert doc.theme_routing["chart"] == "drafting"
    placed = _bar_doc(doc)
    assert _classes(doc, placed.ref.id) == ["drafting-chart"]  # no .chart--bar in this theme
    worn = [
        str(child.get("class"))
        for child in doc.resolve(placed.ref.id)
        if str(child.get("class") or "").startswith("drafting-series")
    ]
    assert worn == ["drafting-series-1", "drafting-series-2"]


# --- 4. editing -------------------------------------------------------------


def test_editing_the_data_rebuilds_the_picture_and_leaves_the_group_alone() -> None:
    doc = _doc()
    placed = ops.add_chart(
        doc,
        kind="bar",
        data=BarData(categories=["a", "b"], series=[Series(name="one", values=[1, 2])]),
        x=40,
        y=60,
        title="before",
    )
    group = doc.resolve(placed.ref.id)
    before = (str(group.get_id()), list(_classes(doc, placed.ref.id)), str(group.get("transform")))
    rects_before = _tags_under(doc, placed.ref.id).count("rect")

    result = ops.edit_chart(
        doc,
        placed.ref.id,
        data=BarData(
            categories=["a", "b", "c"],
            series=[Series(name="one", values=[1, 2, 3]), Series(name="two", values=[4, 5, 6])],
        ),
        title="after",
    )
    group = doc.resolve(placed.ref.id)
    after = (str(group.get_id()), _classes(doc, placed.ref.id), str(group.get("transform")))
    assert after == before
    assert _tags_under(doc, placed.ref.id).count("rect") == 6 > rects_before
    assert result.children == 4  # the frame, two series groups, the title
    spec = read_chart_spec(group)
    assert spec is not None and spec.title == "after"


def test_a_rebuild_leaves_exactly_one_generation_of_children_behind() -> None:
    doc = _doc()
    placed = _bar_doc(doc)
    first = len(_tags_under(doc, placed.ref.id))
    ops.edit_chart(doc, placed.ref.id, title="a")
    ops.edit_chart(doc, placed.ref.id, title="")
    assert len(_tags_under(doc, placed.ref.id)) == first


def test_an_edit_cannot_turn_one_kind_of_chart_into_another() -> None:
    doc = _doc()
    placed = _bar_doc(doc)
    with pytest.raises(InvalidArgument, match="does not fit a bar chart"):
        ops.edit_chart(doc, placed.ref.id, data=DonutData(slices=[Slice(label="a", value=1)]))


def test_editing_something_that_is_not_a_chart_is_refused() -> None:
    doc = _doc()
    rect = ops.add_rect(doc, x=0, y=0, width=10, height=10)
    with pytest.raises(InvalidArgument, match="is not a chart"):
        ops.edit_chart(doc, rect.id, title="nope")


def test_resizing_a_chart_moves_its_plot_but_not_its_origin() -> None:
    doc = _doc()
    placed = ops.add_chart(
        doc,
        kind="bar",
        data=BarData(categories=["a"], series=[Series(name="s", values=[1])]),
        x=10,
        y=10,
    )
    ops.edit_chart(doc, placed.ref.id, width=500, height=300)
    group = doc.resolve(placed.ref.id)
    assert "translate(10" in str(group.get("transform"))
    spec = read_chart_spec(group)
    assert spec is not None and (spec.w, spec.h, spec.auto) == (500.0, 300.0, False)


# --- 5. inspection and placement --------------------------------------------


def test_get_params_hands_back_the_spec_a_chart_was_built_from() -> None:
    doc = _doc()
    data = BarData(categories=["a", "b"], series=[Series(name="one", values=[1.0, 2.0])])
    placed = ops.add_chart(doc, kind="bar", data=data, title="T", x_label="X", y_label="Y")
    reported = get_params(doc, placed.ref.id)
    assert reported["kind"] == "chart"
    assert reported["parametric"] is True
    params = reported["params"]
    assert isinstance(params, dict)
    assert params["kind"] == "bar"
    assert params["data"] == data.model_dump()
    assert (params["title"], params["x_label"], params["y_label"]) == ("T", "X", "Y")
    assert (params["w"], params["h"], params["auto"]) == (320.0, 200.0, True)


def test_an_auto_placed_chart_stacks_under_the_diagram_node_above_it() -> None:
    doc = _doc()
    node = ops.add_diagram_node(doc, kind="service", label="Auth", x=20, y=20, width=80, height=40)
    chart = ops.add_chart(
        doc, kind="sparkline", data=SparklineData(values=[1.0, 2.0, 3.0])
    )
    assert chart.y > node.y + node.h
    # ...and the next node stacks under the CHART, not back under the node.
    following = ops.add_diagram_node(doc, kind="service", label="Next")
    assert following.y > chart.y + chart.h


def test_two_charts_in_a_row_stack_rather_than_overlap() -> None:
    doc = _doc()
    first = ops.add_chart(doc, kind="sparkline", data=SparklineData(values=[1.0, 5.0]))
    second = ops.add_chart(doc, kind="sparkline", data=SparklineData(values=[1.0, 5.0]))
    assert second.y >= first.y + first.h


def test_a_chart_needs_a_positive_box() -> None:
    doc = _doc()
    with pytest.raises(InvalidArgument, match="positive width and height"):
        ops.add_chart(
            doc,
            kind="sparkline",
            data=SparklineData(values=[1.0, 2.0]),
            width=0,
        )


# --- 6. what it actually renders --------------------------------------------


def _render(doc: Document) -> object:
    renderer = get_renderer()
    if not renderer.available():
        pytest.skip("no resvg backend available")
    from PIL import Image

    return Image.open(io.BytesIO(renderer.render(RenderRequest(svg=export_svg(doc))).png)).convert(
        "RGBA"
    )


def test_a_themeless_bar_chart_renders_its_first_bar_in_the_first_series_colour() -> None:
    doc = _doc()
    placed = ops.add_chart(
        doc,
        kind="bar",
        data=BarData(categories=["a", "b"], series=[Series(name="one", values=[80, 80])]),
        x=0,
        y=0,
        width=300,
        height=200,
    )
    image = _render(doc)
    rects = [
        node
        for node in doc.resolve(placed.ref.id).iter()
        if isinstance(node.tag, str) and str(node.tag).endswith("rect")
    ]
    first, second = rects[0], rects[1]
    inside = (
        int(float(first.get("x")) + float(first.get("width")) / 2),
        int(float(first.get("y")) + float(first.get("height")) / 2),
    )
    pixel = image.getpixel(inside)  # type: ignore[attr-defined]
    assert isinstance(pixel, tuple)
    assert pixel[:3] == (0x44, 0x77, 0xAA)  # --series-1, via .default-series-1

    # The 30% of the band that is not a bar: bare canvas, sampled off the gridlines that do
    # cross it (a document has no background, so bare canvas is transparent).
    between = (
        int((float(first.get("x")) + float(first.get("width")) + float(second.get("x"))) / 2),
        inside[1] + 8,
    )
    gap = image.getpixel(between)  # type: ignore[attr-defined]
    assert isinstance(gap, tuple)
    assert gap[3] == 0


def test_a_donut_is_drawn_with_a_hole_in_it() -> None:
    doc = _doc()
    ops.add_chart(
        doc,
        kind="donut",
        data=DonutData(slices=[Slice(label="a", value=60), Slice(label="b", value=40)]),
        x=0,
        y=0,
        width=200,
        height=200,
    )
    image = _render(doc)
    middle = image.getpixel((100, 100))  # type: ignore[attr-defined]
    assert isinstance(middle, tuple)
    assert middle[3] == 0  # the hole is a hole: nothing painted there at all
    # ...and the ring itself is, a few pixels in from its outer edge.
    ring = image.getpixel((100, 12))  # type: ignore[attr-defined]
    assert isinstance(ring, tuple)
    assert (ring[3], ring[:3]) == (255, (0x44, 0x77, 0xAA))


def test_the_dark_variant_repaints_a_chart_without_moving_it() -> None:
    doc = _doc()
    placed = _bar_doc(doc)
    before = export_svg(doc)
    ops.set_theme_variant(doc, "dark")
    after = export_svg(doc)
    assert "#4477aa" in before and "#77aadd" in after  # series 1, light then dark
    assert doc.theme_meta["default"].tokens["--chart-grid"] == "#2b3138"
    spec = read_chart_spec(doc.resolve(placed.ref.id))
    assert spec is not None and (spec.w, spec.h) == (320.0, 200.0)
