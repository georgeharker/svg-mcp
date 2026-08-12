"""Chart facades: the pure scales first, then the schemas, then the facade built on both."""

from __future__ import annotations

import hashlib
import io
import math
import re
from pathlib import Path

import pytest
from inkex import BaseElement
from lxml import etree
from pydantic import ValidationError

from svg_mcp import ops
from svg_mcp.model import Document
from svg_mcp.model.errors import InvalidArgument
from svg_mcp.ops.chart import (
    AxesSpec,
    BarData,
    DonutData,
    LineData,
    PointSeries,
    ScatterData,
    Series,
    Sizes,
    Slice,
    SparklineData,
    TickFormat,
    bar_bands,
    donut_angles,
    format_tick,
    include_zero,
    log_ticks,
    marker_points,
    nice_step,
    nice_ticks,
    plot_margins,
    read_chart_spec,
    resolve_axis,
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


def _axis_x(doc: Document, chart_id: str, theme: str = "default") -> float:
    axis = next(
        node
        for node in doc.resolve(chart_id).iter()
        if isinstance(node.tag, str) and f"{theme}-axis" in str(node.get("class") or "")
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


# --- 7. tick labels: the closed formatting vocabulary ------------------------


def test_a_plain_tick_is_the_number_and_nothing_else() -> None:
    assert format_tick(20.0) == "20"
    assert format_tick(0.5) == "0.5"
    assert format_tick(-1234.5) == "-1234.5"
    assert format_tick(-0.0) == "0"  # a negative zero is a zero
    assert format_tick(0.0) == "0"


def test_thousands_grouping_reaches_the_integer_part_and_leaves_the_rest() -> None:
    assert format_tick(1204.0, TickFormat(thousands=True)) == "1,204"
    assert format_tick(-1204.0, TickFormat(thousands=True)) == "-1,204"
    assert format_tick(1204.5, TickFormat(thousands=True)) == "1,204.5"
    assert format_tick(204.0, TickFormat(thousands=True)) == "204"


def test_a_percent_tick_is_the_fraction_times_a_hundred() -> None:
    percent = TickFormat(style="percent")
    assert format_tick(0.35, percent) == "35%"
    assert format_tick(1.0, percent) == "100%"
    assert format_tick(0.0, percent) == "0%"
    assert format_tick(-0.075, TickFormat(style="percent", decimals=1)) == "-7.5%"


def test_currency_owns_the_symbol_and_keeps_the_sign_outside_it() -> None:
    money = TickFormat(style="currency", thousands=True)
    assert format_tick(1200.0, money) == "$1,200"
    assert format_tick(-1200.0, money) == "-$1,200"
    assert format_tick(0.0, money) == "$0"
    assert format_tick(3.5, TickFormat(style="currency", prefix="€")) == "€3.5"


def test_an_si_tick_carries_its_unit_above_and_below_one() -> None:
    si = TickFormat(style="si")
    assert format_tick(1500.0, si) == "1.5k"
    assert format_tick(1_000_000.0, si) == "1M"
    assert format_tick(-2.5e9, si) == "-2.5G"
    assert format_tick(4e12, si) == "4T"
    assert format_tick(0.05, si) == "50m"
    assert format_tick(2.5e-6, si) == "2.5µ"
    assert format_tick(7e-9, si) == "7n"
    assert format_tick(0.0, si) == "0"  # zero has no magnitude, so it takes no prefix
    assert format_tick(1234.0, TickFormat(style="si", decimals=2)) == "1.23k"


def test_a_fixed_tick_keeps_the_zeros_it_was_asked_for() -> None:
    assert format_tick(3.0, TickFormat(style="fixed", decimals=2)) == "3.00"
    assert format_tick(3.456, TickFormat(style="fixed")) == "3"
    assert format_tick(-3.456, TickFormat(style="fixed", decimals=1)) == "-3.5"
    # rounded down to nothing is zero, not minus zero
    assert format_tick(-0.004, TickFormat(style="fixed", decimals=2)) == "0.00"


def test_prefix_and_suffix_wrap_whatever_the_style_produced() -> None:
    fmt = TickFormat(style="percent", prefix="~", suffix=" MoM")
    assert format_tick(0.35, fmt) == "~35% MoM"
    assert format_tick(12.0, TickFormat(suffix="ms")) == "12ms"
    assert format_tick(1500.0, TickFormat(style="si", suffix="B")) == "1.5kB"


# --- 8. the scales the axes model adds --------------------------------------


def test_a_tick_target_actually_changes_how_many_ticks_there_are() -> None:
    few, many = nice_ticks(0.0, 100.0, 2), nice_ticks(0.0, 100.0, 10)
    assert few == [0.0, 50.0, 100.0]
    assert len(many) > len(few)
    assert many[0] <= 0.0 and many[-1] >= 100.0


def test_a_short_log_span_is_subdivided_one_two_five() -> None:
    assert log_ticks(1.0, 100.0) == [1.0, 2.0, 5.0, 10.0, 20.0, 50.0, 100.0]
    # ...and the ends open out to the ladder rungs that enclose the data.
    assert log_ticks(3.0, 80.0) == [2.0, 5.0, 10.0, 20.0, 50.0, 100.0]
    assert log_ticks(0.02, 0.9) == [0.02, 0.05, 0.1, 0.2, 0.5, 1.0]


def test_a_long_log_span_ticks_by_whole_decades_thinned_to_the_target() -> None:
    assert log_ticks(1.0, 1e6) == [1.0, 100.0, 10000.0, 1000000.0]
    assert log_ticks(1.0, 1e6, 10) == [1.0, 10.0, 100.0, 1000.0, 10000.0, 100000.0, 1000000.0]


def test_a_flat_log_series_gets_a_decade_either_side_rather_than_a_division() -> None:
    ticks = log_ticks(50.0, 50.0)
    assert ticks[0] < 50.0 < ticks[-1]


def test_a_log_axis_refuses_a_limit_that_has_no_logarithm_and_names_it() -> None:
    with pytest.raises(InvalidArgument, match="0.0 is not one"):
        log_ticks(0.0, 100.0)
    with pytest.raises(InvalidArgument, match="-5.0 is not one"):
        log_ticks(-5.0, 100.0)


def test_pinning_one_end_of_an_axis_leaves_the_other_derived() -> None:
    axis = resolve_axis([3.0, 47.0], hi_pin=50.0)
    assert axis.scale.hi == 50.0
    assert axis.scale.lo <= 3.0 and axis.pinned
    low = resolve_axis([3.0, 47.0], lo_pin=1.0)
    assert low.scale.lo == 1.0
    assert low.scale.hi >= 47.0
    both = resolve_axis([3.0, 47.0], lo_pin=0.0, hi_pin=50.0)
    assert (both.scale.lo, both.scale.hi) == (0.0, 50.0)
    assert not resolve_axis([3.0, 47.0]).pinned


def test_an_empty_pinned_range_is_refused_rather_than_drawn_inside_out() -> None:
    with pytest.raises(InvalidArgument, match="empty range"):
        resolve_axis([1.0, 2.0], lo_pin=50.0, hi_pin=10.0)


def test_a_bar_stops_including_zero_at_the_end_the_caller_pinned() -> None:
    assert resolve_axis([40.0, 73.0], zero=True).scale.lo == 0.0
    pinned = resolve_axis([40.0, 73.0], zero=True, lo_pin=35.0)
    assert pinned.scale.lo == 35.0  # the pin outranks the zero rule
    assert resolve_axis([-12.0, -3.0], zero=True, hi_pin=-1.0).scale.hi == -1.0


def test_an_explicit_tick_list_is_used_verbatim_and_trimmed_to_the_range() -> None:
    axis = resolve_axis([0.0, 100.0], lo_pin=0.0, hi_pin=100.0, ticks=[0.0, 33.0, 66.0, 200.0])
    assert axis.ticks == (0.0, 33.0, 66.0)  # 200 is outside, and dropped rather than refused
    assert axis.labels == ("0", "33", "66")


def test_a_tick_format_is_what_writes_an_axis_labels() -> None:
    axis = resolve_axis(
        [0.0, 1.0], lo_pin=0.0, hi_pin=1.0, ticks=4, fmt=TickFormat(style="percent")
    )
    assert axis.labels[0] == "0%" and axis.labels[-1] == "100%"


def test_a_marker_shape_is_the_polygon_its_name_says() -> None:
    diamond = marker_points("diamond", 0.0, 0.0, 2.0)
    assert diamond == [(0.0, -2.0), (2.0, 0.0), (0.0, 2.0), (-2.0, 0.0)]
    square = marker_points("square", 0.0, 0.0, 2.0)
    assert square[0] == (-2.0, -2.0) and len(square) == 4
    triangle = marker_points("triangle", 0.0, 0.0, 2.0)
    assert len(triangle) == 3
    assert triangle[0][0] == pytest.approx(0.0) and triangle[0][1] == pytest.approx(-2.0)
    assert marker_points("circle", 0.0, 0.0, 2.0) == []
    assert marker_points("none", 0.0, 0.0, 2.0) == []


def test_the_size_tokens_are_what_the_margins_are_measured_with() -> None:
    small = plot_margins(["0", "100"], font="sans-serif", title=True, x_label=True, y_label=True)
    big = plot_margins(
        ["0", "100"],
        font="sans-serif",
        title=True,
        x_label=True,
        y_label=True,
        sizes=Sizes(tick=16.0, axis_label=11.0, title=13.0, gap=8.0),
    )
    assert big.left > small.left and big.bottom > small.bottom


def test_tick_marks_buy_themselves_room_on_both_sides() -> None:
    bare = plot_margins(["0"], font="sans-serif", title=False, x_label=False, y_label=False)
    marked = plot_margins(
        ["0"], font="sans-serif", title=False, x_label=False, y_label=False, tick_marks=6.0
    )
    assert marked.left == pytest.approx(bare.left + 6.0)
    assert marked.bottom == pytest.approx(bare.bottom + 6.0)


def test_turned_x_labels_claim_the_height_they_actually_occupy() -> None:
    level = plot_margins(
        ["0"],
        font="sans-serif",
        title=False,
        x_label=False,
        y_label=False,
        x_tick_labels=["a very long category name"],
    )
    turned = plot_margins(
        ["0"],
        font="sans-serif",
        title=False,
        x_label=False,
        y_label=False,
        x_tick_labels=["a very long category name"],
        x_tick_rotate=-45.0,
    )
    assert turned.bottom > level.bottom


# --- 9. the axes model, as a chart actually draws it -------------------------


def _themed(doc: Document) -> None:
    ops.load_theme(doc, "metricfree", search_paths=[FIXTURES])


def _wearing(doc: Document, chart_id: str, class_name: str) -> list[BaseElement]:
    return [
        node
        for node in doc.resolve(chart_id).iter()
        if isinstance(node.tag, str) and class_name in str(node.get("class") or "").split()
    ]


def _texts(doc: Document, chart_id: str, class_name: str) -> list[str]:
    return [str(node.text or "") for node in _wearing(doc, chart_id, class_name)]


def _bar_chart(doc: Document, values: list[float], axes: AxesSpec | None = None) -> ops.PlacedChart:
    return ops.add_chart(
        doc,
        kind="bar",
        data=BarData(
            categories=[f"c{index}" for index in range(len(values))],
            series=[Series(name="s", values=values)],
        ),
        x=0,
        y=0,
        width=320,
        height=200,
        axes=axes,
    )


def test_a_chart_with_no_axes_argument_stores_no_axes_at_all() -> None:
    doc = _doc()
    placed = _bar_chart(doc, [1.0, 2.0])
    assert "axes" not in str(doc.resolve(placed.ref.id).get("data-chart"))
    spec = read_chart_spec(doc.resolve(placed.ref.id))
    assert spec is not None and spec.axes is None


def test_pinned_limits_reach_the_axis_the_chart_draws() -> None:
    doc = _doc()
    placed = _bar_chart(doc, [10.0, 40.0], AxesSpec(y_min=0, y_max=50))
    assert _texts(doc, placed.ref.id, "default-tick-label")[:6] == [
        "0",
        "10",
        "20",
        "30",
        "40",
        "50",
    ]


def test_data_outside_a_pinned_range_is_clipped_to_the_plot_rect() -> None:
    doc = _doc()
    placed = _bar_chart(doc, [10.0, 900.0], AxesSpec(y_max=100))
    group = doc.resolve(placed.ref.id)
    series = [
        child for child in group if str(child.get("class") or "").startswith("default-series")
    ]
    assert series and all(str(child.get("clip-path")).startswith("url(#") for child in series)

    clip = doc.resolve(str(series[0].get("clip-path"))[len("url(#") : -1])
    assert str(clip.tag).endswith("clipPath")
    window = next(iter(clip))
    rects = [node for node in series[0] if str(node.tag).endswith("rect")]
    # The 900 bar really does overflow — the clip is load-bearing, not decoration.
    assert min(float(rect.get("y")) for rect in rects) < float(window.get("y"))
    assert float(window.get("height")) > 0


def test_a_chart_nobody_pinned_needs_no_clip_path_at_all() -> None:
    doc = _doc()
    _bar_chart(doc, [10.0, 900.0])
    assert "clipPath" not in export_svg(doc)


def test_rebuilding_a_clipped_chart_leaves_one_clip_behind_and_not_two() -> None:
    doc = _doc()
    placed = _bar_chart(doc, [10.0, 900.0], AxesSpec(y_max=100))
    ops.edit_chart(doc, placed.ref.id, title="again")
    ops.edit_chart(doc, placed.ref.id, title="and again")
    assert export_svg(doc).count("<clipPath") == 1


def test_an_explicit_tick_list_is_what_the_chart_labels() -> None:
    doc = _doc()
    placed = _bar_chart(doc, [0.0, 100.0], AxesSpec(y_min=0, y_max=100, ticks=[0, 33, 66, 250]))
    assert _texts(doc, placed.ref.id, "default-tick-label")[:3] == ["0", "33", "66"]


def test_a_tick_count_asks_for_that_many_ticks() -> None:
    doc = _doc()
    few = _bar_chart(doc, [0.0, 100.0], AxesSpec(ticks=2))
    many = _bar_chart(doc, [0.0, 100.0], AxesSpec(ticks=10))
    horizontal = [
        len([node for node in _wearing(doc, chart.ref.id, "default-gridline")])
        for chart in (few, many)
    ]
    assert horizontal[1] > horizontal[0]


def test_a_tick_format_reaches_the_labels_the_axis_draws() -> None:
    doc = _doc()
    placed = _bar_chart(
        doc, [0.1, 0.9], AxesSpec(y_min=0, y_max=1, tick_format=TickFormat(style="percent"))
    )
    labels = _texts(doc, placed.ref.id, "default-tick-label")
    assert [label for label in labels if label.endswith("%")] == [
        "0%",
        "20%",
        "40%",
        "60%",
        "80%",
        "100%",
    ]


def test_tick_marks_are_drawn_outside_the_plot_at_every_tick() -> None:
    doc = _doc()
    placed = _bar_chart(doc, [0.0, 100.0], AxesSpec(y_min=0, y_max=100, ticks=[0, 50, 100]))
    assert _wearing(doc, placed.ref.id, "default-tick") == []

    marked = _bar_chart(
        doc, [0.0, 100.0], AxesSpec(y_min=0, y_max=100, ticks=[0, 50, 100], tick_marks=6)
    )
    marks = _wearing(doc, marked.ref.id, "default-tick")
    assert len(marks) == 3 + 2  # three value ticks, two categories
    axis_x = _axis_x(doc, marked.ref.id)
    horizontal = [node for node in marks if float(node.get("y1")) == float(node.get("y2"))]
    assert len(horizontal) == 3
    for node in horizontal:
        assert float(node.get("x2")) == pytest.approx(axis_x)
        assert float(node.get("x2")) - float(node.get("x1")) == pytest.approx(6.0)


def test_tick_marks_push_the_plot_in_to_make_room_for_themselves() -> None:
    doc = _doc()
    bare = _bar_chart(doc, [0.0, 100.0])
    marked = _bar_chart(doc, [0.0, 100.0], AxesSpec(tick_marks=8))
    assert _axis_x(doc, marked.ref.id) == pytest.approx(_axis_x(doc, bare.ref.id) + 8.0)


def test_turning_the_x_tick_labels_puts_a_rotate_on_each_of_them() -> None:
    doc = _doc()
    placed = ops.add_chart(
        doc,
        kind="bar",
        data=BarData(
            categories=["alpha", "beta"], series=[Series(name="s", values=[1.0, 2.0])]
        ),
        axes=AxesSpec(x_tick_rotate=-45),
    )
    labels = _wearing(doc, placed.ref.id, "default-tick-label")
    turned = [node for node in labels if str(node.text or "") in ("alpha", "beta")]
    assert len(turned) == 2
    for node in turned:
        # inkex normalizes a rotate() to its matrix, so the angle is read back out of it.
        matrix = re.findall(r"-?\d+\.?\d*", str(node.get("transform")))
        assert math.degrees(math.atan2(float(matrix[1]), float(matrix[0]))) == pytest.approx(-45.0)
        assert "text-anchor:end" in str(node.get("style"))
    # ...and the value labels, which were never crowded, are left level.
    numbers = [node for node in labels if node not in turned]
    assert all(node.get("transform") is None for node in numbers)


def _grid_split(doc: Document, chart_id: str) -> tuple[int, int]:
    lines = _wearing(doc, chart_id, "default-gridline")
    across = sum(1 for node in lines if float(node.get("y1")) == float(node.get("y2")))
    return across, len(lines) - across


def test_gridlines_draw_exactly_the_sets_they_were_asked_for() -> None:
    doc = _doc()
    default = _bar_chart(doc, [1.0, 2.0, 3.0])
    assert _grid_split(doc, default.ref.id)[1] == 0  # y only, which is the default

    for mode, expected in (("none", (0, 0)), ("x", (0, 3)), ("both", None)):
        chart = _bar_chart(doc, [1.0, 2.0, 3.0], AxesSpec(gridlines=mode))  # type: ignore[arg-type]
        across, down = _grid_split(doc, chart.ref.id)
        if expected is None:
            assert across > 0 and down == 3
        else:
            assert (across, down) == expected


def test_a_theme_that_sets_bigger_tick_type_indents_the_plot_further() -> None:
    doc = _doc()
    _themed(doc)
    small = _bar_chart(doc, [0.0, 100.0])
    ops.set_theme_variant(doc, "bigticks")
    big = _bar_chart(doc, [0.0, 100.0])
    small_x = _axis_x(doc, small.ref.id, "metricfree")
    big_x = _axis_x(doc, big.ref.id, "metricfree")
    assert big_x > small_x  # 16px labels are wider than 10px ones, and the margin knows it
    # ...which is the same thing as the plot getting narrower, since the box did not change.
    def width(chart_id: str) -> float:
        base = next(
            node
            for node in doc.resolve(chart_id).iter()
            if isinstance(node.tag, str)
            and "metricfree-axis" in str(node.get("class") or "").split()
            and float(node.get("y1")) == float(node.get("y2"))
        )
        return float(base.get("x2")) - float(base.get("x1"))

    assert width(big.ref.id) < width(small.ref.id)


# --- 10. the log scale ------------------------------------------------------


def test_a_log_bar_measures_from_the_axis_floor_rather_than_from_zero() -> None:
    doc = _doc()
    placed = _bar_chart(doc, [10.0, 1000.0], AxesSpec(scale="log"))
    rects = sorted(
        (
            node
            for node in doc.resolve(placed.ref.id).iter()
            if isinstance(node.tag, str) and str(node.tag).endswith("rect")
        ),
        key=lambda node: float(node.get("x")),
    )
    heights = [float(node.get("height")) for node in rects]
    assert heights[0] == pytest.approx(0.0)  # 10 IS the floor of a 10..1000 axis
    assert heights[1] > 100.0
    assert _texts(doc, placed.ref.id, "default-tick-label")[:3] == ["10", "20", "50"]


def test_a_log_chart_refuses_a_value_it_cannot_take_the_log_of() -> None:
    doc = _doc()
    with pytest.raises(InvalidArgument, match="no zero and no"):
        _bar_chart(doc, [0.0, 10.0], AxesSpec(scale="log"))
    with pytest.raises(InvalidArgument, match="cannot plot -3.0"):
        _bar_chart(doc, [-3.0, 10.0], AxesSpec(scale="log"))


def test_a_log_chart_refuses_a_pinned_limit_it_cannot_take_the_log_of() -> None:
    doc = _doc()
    with pytest.raises(InvalidArgument, match="cannot plot 0.0"):
        _bar_chart(doc, [1.0, 10.0], AxesSpec(scale="log", y_min=0))


# --- 11. markers, hatching, and a sparkline's flourishes ---------------------


@pytest.mark.parametrize(
    ("shape", "tag"),
    [("circle", "circle"), ("square", "rect"), ("diamond", "path"), ("triangle", "path")],
)
def test_each_marker_shape_draws_its_own_kind_of_element(shape: str, tag: str) -> None:
    doc = _doc()
    placed = ops.add_chart(
        doc,
        kind="scatter",
        data=ScatterData(
            series=[PointSeries(name="s", points=[(1, 2), (2, 3), (3, 4)])],
            marker=shape,  # type: ignore[arg-type]
        ),
    )
    assert _tags_under(doc, placed.ref.id).count(tag) == 3


def test_a_marker_of_none_leaves_the_line_and_drops_the_marks() -> None:
    doc = _doc()
    placed = ops.add_chart(
        doc,
        kind="line",
        data=LineData(
            series=[PointSeries(name="s", points=[(0, 1), (1, 4), (2, 2)])],
            points=True,
            marker="none",
        ),
    )
    tags = _tags_under(doc, placed.ref.id)
    assert tags.count("polyline") == 1 and "circle" not in tags


def test_marker_size_is_the_half_extent_it_says_it_is() -> None:
    doc = _doc()
    placed = ops.add_chart(
        doc,
        kind="scatter",
        data=ScatterData(
            series=[PointSeries(name="s", points=[(1, 2), (2, 3)])], marker_size=6.0
        ),
    )
    radii = {
        float(node.get("r"))
        for node in doc.resolve(placed.ref.id).iter()
        if isinstance(node.tag, str) and str(node.tag).endswith("circle")
    }
    assert radii == {6.0}


def test_hatching_gives_each_series_a_pattern_its_marks_point_at() -> None:
    doc = _doc()
    placed = ops.add_chart(
        doc,
        kind="bar",
        data=BarData(
            categories=["a", "b"],
            series=[Series(name="one", values=[1, 2]), Series(name="two", values=[3, 4])],
            hatch=True,
        ),
    )
    svg = export_svg(doc)
    assert svg.count("<pattern") == 2
    rects = [
        node
        for node in doc.resolve(placed.ref.id).iter()
        if isinstance(node.tag, str) and str(node.tag).endswith("rect")
    ]
    assert rects and all("fill:url(#" in str(node.get("style")) for node in rects)
    # The pattern is painted BY THE THEME, not by a colour copied into it: the line inside wears
    # the series class, so a variant switch moves the hatching with everything else.
    patterns = [
        node
        for node in doc.svg.iter()
        if isinstance(node.tag, str) and str(node.tag).endswith("pattern")
    ]
    inside = [str(next(iter(pattern)).get("class")) for pattern in patterns]
    assert sorted(inside) == ["default-series-1", "default-series-2"]


def test_an_unhatched_chart_defines_no_patterns() -> None:
    doc = _doc()
    _bar_chart(doc, [1.0, 2.0])
    assert "<pattern" not in export_svg(doc)


def test_a_sparkline_can_mark_its_extremes_its_last_point_and_a_baseline() -> None:
    doc = _doc()
    placed = ops.add_chart(
        doc,
        kind="sparkline",
        data=SparklineData(
            values=[3, 9, 4, 6], last_point=True, extremes=True, baseline=5.0
        ),
    )
    tags = _tags_under(doc, placed.ref.id)
    assert tags.count("circle") == 3  # the min, the max, and the last value
    assert tags.count("line") == 1  # the baseline
    assert tags.count("polyline") == 1
    line = _wearing(doc, placed.ref.id, "default-gridline")
    assert len(line) == 1
    at = float(line[0].get("y1"))
    assert 0.0 < at < placed.h
    dots = [
        node
        for node in doc.resolve(placed.ref.id).iter()
        if isinstance(node.tag, str) and str(node.tag).endswith("circle")
    ]
    assert all("default-series-1" in str(node.get("class") or "") for node in dots)


def test_a_sparkline_that_asks_for_nothing_extra_is_still_one_line() -> None:
    doc = _doc()
    placed = ops.add_chart(doc, kind="sparkline", data=SparklineData(values=[3, 9, 4, 6]))
    assert len(_kids(doc, placed.ref.id)) == 1


# --- 12. what the axes layer renders ----------------------------------------


def test_a_hatched_bar_really_is_painted_in_its_series_colour() -> None:
    doc = _doc()
    placed = ops.add_chart(
        doc,
        kind="bar",
        data=BarData(categories=["a"], series=[Series(name="s", values=[80])], hatch=True),
        x=0,
        y=0,
        width=300,
        height=200,
    )
    image = _render(doc)
    rect = next(
        node
        for node in doc.resolve(placed.ref.id).iter()
        if isinstance(node.tag, str) and str(node.tag).endswith("rect")
    )
    left, top = int(float(rect.get("x"))), int(float(rect.get("y")))
    width, height = int(float(rect.get("width"))), int(float(rect.get("height")))
    seen = set()
    for dx in range(0, width, 3):
        for dy in range(0, height, 3):
            pixel = image.getpixel((left + dx, top + dy))  # type: ignore[attr-defined]
            assert isinstance(pixel, tuple)
            seen.add(pixel[:3] if pixel[3] > 200 else None)
    # resvg resolves a CSS class on the line INSIDE the pattern: the hatch is series-1...
    assert (0x44, 0x77, 0xAA) in seen
    # ...and it is hatching, not a flat fill — the gaps between the strokes stay bare.
    assert None in seen


# --- 13. the compatibility pin ----------------------------------------------
#
# The contract for `axes` is that its ABSENCE changes nothing. This hashes the DRAWING of one of
# every kind — ids stripped (they are random) and the spec attribute stripped (it gains the new
# per-kind data fields at their defaults) — against a digest captured from the code as it stood
# before the axes layer was written. The `metricfree` theme is what makes that portable: its
# --font names a family nothing has installed, so every measured margin is the same number on
# every machine instead of a record of this laptop's fonts.

_AXES_NONE_DIGEST = "39b88c54a960e87a30bed0c52056d242423213efa7e6dfd291e7063be6069823"


def _battery(doc: Document) -> list[str]:
    return [
        ops.add_chart(
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
            title="T",
            x_label="X",
            y_label="Y",
        ).ref.id,
        ops.add_chart(
            doc,
            kind="line",
            data=LineData(
                series=[PointSeries(name="s", points=[(0, 1), (1, 4), (2, 2), (3, 6)])],
                points=True,
                area=True,
            ),
            x=0,
            y=0,
            title="L",
        ).ref.id,
        ops.add_chart(
            doc,
            kind="scatter",
            data=ScatterData(
                series=[
                    PointSeries(name="a", points=[(1, 2), (2, 5), (3, 4)]),
                    PointSeries(name="b", points=[(1, 9), (2, 7)]),
                ]
            ),
            x=10,
            y=10,
        ).ref.id,
        ops.add_chart(
            doc,
            kind="donut",
            data=DonutData(
                slices=[
                    Slice(label="a", value=45),
                    Slice(label="b", value=30),
                    Slice(label="c", value=25),
                ]
            ),
            x=0,
            y=0,
            title="D",
        ).ref.id,
        ops.add_chart(
            doc, kind="sparkline", data=SparklineData(values=[3, 5, 4, 8, 6]), x=0, y=0
        ).ref.id,
        ops.add_chart(
            doc,
            kind="bar",
            data=BarData(categories=["a", "b"], series=[Series(name="s", values=[-12, 0.5])]),
            x=0,
            y=0,
            width=500,
            height=300,
            y_label="neg",
        ).ref.id,
    ]


def _drawing_digest(doc: Document, chart_ids: list[str]) -> str:
    blobs = []
    for chart_id in chart_ids:
        clone = etree.fromstring(etree.tostring(doc.resolve(chart_id)))
        for node in clone.iter():
            node.attrib.pop("id", None)
        clone.attrib.pop("data-chart", None)
        blobs.append(etree.tostring(clone))
    return hashlib.sha256(b"\n".join(blobs)).hexdigest()


def test_a_chart_with_no_axes_draws_what_it_drew_before_axes_existed() -> None:
    doc = _doc()
    _themed(doc)
    assert _drawing_digest(doc, _battery(doc)) == _AXES_NONE_DIGEST


def test_passing_axes_none_is_the_same_as_not_passing_axes() -> None:
    doc = _doc()
    _themed(doc)
    implicit = _bar_chart(doc, [10.0, 42.0, 73.0])
    explicit = _bar_chart(doc, [10.0, 42.0, 73.0], axes=None)
    assert _drawing_digest(doc, [implicit.ref.id]) == _drawing_digest(doc, [explicit.ref.id])
