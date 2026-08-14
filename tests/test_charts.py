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
from pydantic import BaseModel, ValidationError

from svg_mcp import ops
from svg_mcp.model import Document
from svg_mcp.model.errors import InvalidArgument
from svg_mcp.ops.chart import (
    AxesSpec,
    BarData,
    DonutData,
    HistogramData,
    LineData,
    Marker,
    PointSeries,
    RadarData,
    ReferenceLine,
    Scale,
    ScatterData,
    Segment,
    Series,
    SeriesBand,
    Sizes,
    Slice,
    SparklineData,
    TickFormat,
    bar_bands,
    datum_anchor,
    donut_angles,
    format_tick,
    histogram_counts,
    histogram_edges,
    include_zero,
    label_centre,
    log_ticks,
    marker_points,
    marker_radii,
    marker_strokes,
    minor_ticks,
    nice_step,
    nice_ticks,
    normalize_stacks,
    order_indices,
    plot_margins,
    radar_anchor,
    radar_angle,
    radar_points,
    radar_rings,
    read_chart_spec,
    reference_extent,
    resolve_axis,
    stack_extents,
    stack_segments,
    step_points,
    tick_reach,
    tick_text,
    waterfall_segments,
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


def _described(model: type[BaseModel]) -> int:
    """How many of a model's fields carry a description into the JSON schema."""
    properties = model.model_json_schema()["properties"]
    assert isinstance(properties, dict)
    return sum(1 for spec in properties.values() if "description" in spec)


def test_the_field_docs_reach_the_schema_a_caller_actually_reads() -> None:
    """These models ARE the tool's argument documentation, and pydantic drops attribute
    docstrings unless ``use_attribute_docstrings`` is set — a flag that falls off a model
    silently, taking twenty fields' worth of explanation off the wire with it."""
    minor = AxesSpec.model_json_schema()["properties"]["minor"]
    assert isinstance(minor, dict) and "description" in minor
    assert _described(AxesSpec) >= 15
    # ...and the radar's own frame controls describe themselves too
    radar = RadarData.model_json_schema()["properties"]
    assert all("description" in radar[field] for field in ("axes", "rings", "r_max"))
    assert _described(RadarData) == len(radar)


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
    assert "axes" not in params  # a chart nobody gave axes to stores none, and reports none


def test_get_params_hands_back_the_axes_a_chart_was_given() -> None:
    doc = _doc()
    data = BarData(categories=["a", "b"], series=[Series(name="one", values=[1.0, 2.0])])
    axes = AxesSpec(ticks=4, gridlines="y")
    placed = ops.add_chart(doc, kind="bar", data=data, axes=axes)
    params = get_params(doc, placed.ref.id)["params"]
    assert isinstance(params, dict)
    assert params["axes"] == axes.model_dump()


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


# --- 14. the presentation layer, pure ----------------------------------------


def test_a_stack_runs_positives_up_and_negatives_down_from_zero() -> None:
    assert stack_segments([10.0, 20.0]) == [Segment(0.0, 10.0), Segment(10.0, 30.0)]
    # -5 does not sit on top of the 10: it hangs off zero on its own running total.
    assert stack_segments([10.0, -5.0, -3.0]) == [
        Segment(0.0, 10.0),
        Segment(-5.0, 0.0),
        Segment(-8.0, -5.0),
    ]


def test_a_zero_keeps_its_place_in_the_stacking_order() -> None:
    assert stack_segments([10.0, 0.0, 5.0]) == [
        Segment(0.0, 10.0),
        Segment(10.0, 10.0),
        Segment(10.0, 15.0),
    ]


def test_a_stack_reaches_as_far_down_as_it_reaches_up() -> None:
    assert stack_extents([10.0, -5.0, -3.0]) == (-8.0, 10.0)
    assert stack_extents([10.0, 20.0]) == (0.0, 30.0)


def test_a_post_step_holds_the_value_and_then_rises() -> None:
    assert step_points([(0.0, 1.0), (1.0, 4.0)], "post") == [(0.0, 1.0), (1.0, 1.0), (1.0, 4.0)]


def test_a_pre_step_rises_first_and_then_holds() -> None:
    assert step_points([(0.0, 1.0), (1.0, 4.0)], "pre") == [(0.0, 1.0), (0.0, 4.0), (1.0, 4.0)]


def test_a_step_of_one_point_is_that_point() -> None:
    assert step_points([(2.0, 3.0)], "post") == [(2.0, 3.0)]


def test_a_value_label_sits_outside_its_mark_until_the_edge_is_too_close() -> None:
    # A bar ending at 100 in a plot running 0..200: plenty of room above it.
    assert label_centre(100.0, -1.0, gap=8.0, extent=10.0, lo=0.0, hi=200.0) == 87.0
    # The same bar ending at 4: outside would put half the label off the top, so it flips inside.
    assert label_centre(4.0, -1.0, gap=8.0, extent=10.0, lo=0.0, hi=200.0) == 17.0
    # And the same rule the other way round, for a horizontal bar growing to the right.
    assert label_centre(100.0, 1.0, gap=8.0, extent=20.0, lo=0.0, hi=200.0) == 118.0
    assert label_centre(196.0, 1.0, gap=8.0, extent=20.0, lo=0.0, hi=200.0) == 178.0


def test_a_reference_off_the_axis_is_dropped_but_a_band_is_clamped_to_it() -> None:
    scale = Scale(lo=0.0, hi=80.0)
    assert reference_extent(60.0, None, scale) == (60.0, 60.0)
    assert reference_extent(500.0, None, scale) is None
    assert reference_extent(-50.0, 40.0, scale) == (0.0, 40.0)
    assert reference_extent(60.0, 500.0, scale) == (60.0, 80.0)
    # A band wholly outside has nothing in range to clamp TO, so it is dropped like a line.
    assert reference_extent(200.0, 500.0, scale) is None


def test_ordering_ranks_by_value_by_label_or_by_the_list_it_was_given() -> None:
    labels = ["a", "b", "c"]
    keys = [10.0, 73.0, 42.0]
    assert order_indices(labels, keys, "given") == [0, 1, 2]
    assert order_indices(labels, keys, "value_desc") == [1, 2, 0]
    assert order_indices(labels, keys, "value_asc") == [0, 2, 1]
    assert order_indices(["b", "c", "a"], keys, "label") == [2, 0, 1]
    assert order_indices(labels, keys, ["c", "a"]) == [2, 0, 1]


def test_ordering_by_a_name_the_chart_has_not_got_says_which_one() -> None:
    with pytest.raises(InvalidArgument, match="'nope'"):
        order_indices(["a", "b"], [1.0, 2.0], ["nope", "a"])


# --- 15. value labels ---------------------------------------------------------


def _plot_box(doc: Document, chart_id: str) -> tuple[float, float, float, float]:
    """The plot rect, read back off the two axis lines the chart drew."""
    lines = _wearing(doc, chart_id, "default-axis")
    vertical = next(line for line in lines if line.get("x1") == line.get("x2"))
    horizontal = next(line for line in lines if line.get("y1") == line.get("y2"))
    top, bottom = sorted((float(str(vertical.get("y1"))), float(str(vertical.get("y2")))))
    left, right = sorted((float(str(horizontal.get("x1"))), float(str(horizontal.get("x2")))))
    return (float(str(vertical.get("x1"))), top, right - left, bottom - top)


def _labels_at(doc: Document, chart_id: str) -> dict[str, tuple[float, float]]:
    return {
        str(node.text or ""): (float(str(node.get("x"))), float(str(node.get("y"))))
        for node in _wearing(doc, chart_id, "default-value-label")
    }


def _series_rects(doc: Document, chart_id: str, index: int = 0) -> list[list[float]]:
    group = [
        child
        for child in doc.resolve(chart_id)
        if str(child.get("class") or "").startswith("default-series")
    ][index]
    return [
        [float(str(node.get(key))) for key in ("x", "y", "width", "height")]
        for node in group
        if str(node.tag).endswith("rect")
    ]


def test_a_value_label_is_written_the_way_the_axis_writes_its_ticks() -> None:
    doc = _doc()
    placed = ops.add_chart(
        doc,
        kind="bar",
        data=BarData(
            categories=["a", "b"],
            series=[Series(name="s", values=[0.25, 0.5])],
            value_labels=True,
        ),
        x=0,
        y=0,
        axes=AxesSpec(tick_format=TickFormat(style="percent")),
    )
    assert set(_labels_at(doc, placed.ref.id)) == {"25%", "50%"}


def test_a_value_format_on_the_data_overrules_the_axis_format() -> None:
    doc = _doc()
    placed = ops.add_chart(
        doc,
        kind="bar",
        data=BarData(
            categories=["a"],
            series=[Series(name="s", values=[1500.0])],
            value_labels=True,
            value_format=TickFormat(style="si"),
        ),
        x=0,
        y=0,
        axes=AxesSpec(tick_format=TickFormat(style="plain")),
    )
    assert set(_labels_at(doc, placed.ref.id)) == {"1.5k"}


def test_a_bar_that_reaches_the_top_takes_its_label_inside_instead() -> None:
    doc = _doc()
    placed = ops.add_chart(
        doc,
        kind="bar",
        data=BarData(
            categories=["a", "b"],
            series=[Series(name="s", values=[10.0, 80.0])],
            value_labels=True,
        ),
        x=0,
        y=0,
        axes=AxesSpec(y_min=0, y_max=80),
    )
    labels = _labels_at(doc, placed.ref.id)
    short, tall = _series_rects(doc, placed.ref.id)
    assert labels["10"][1] < short[1]  # the short bar's number is ABOVE its end
    assert labels["80"][1] > tall[1]  # the tall one's would have hung off the plot, so it flipped
    assert labels["80"][1] > _plot_box(doc, placed.ref.id)[1]


def test_value_labels_live_outside_the_window_the_data_is_clipped_to() -> None:
    doc = _doc()
    placed = ops.add_chart(
        doc,
        kind="bar",
        data=BarData(
            categories=["a", "b"],
            series=[Series(name="s", values=[10.0, 900.0])],
            value_labels=True,
        ),
        x=0,
        y=0,
        axes=AxesSpec(y_max=100),
    )
    clipped = [
        node
        for node in _wearing(doc, placed.ref.id, "default-value-label")
        for parent in [node.getparent()]
        if parent is not None and parent.get("clip-path") is not None
    ]
    assert _labels_at(doc, placed.ref.id) and not clipped


def test_a_line_and_a_scatter_write_their_values_above_their_marks() -> None:
    doc = _doc()
    for kind, data in (
        ("line", LineData(series=[PointSeries(name="s", points=[(0, 1), (1, 4)])],
                          value_labels=True)),
        ("scatter", ScatterData(series=[PointSeries(name="s", points=[(0, 1), (1, 4)])],
                                value_labels=True)),
    ):
        placed = ops.add_chart(doc, kind=kind, data=data, x=0, y=0)  # type: ignore[arg-type]
        labels = _labels_at(doc, placed.ref.id)
        assert set(labels) == {"1", "4"}
        assert labels["1"][1] > labels["4"][1]  # 4 is higher up the plot than 1


# --- 16. reference lines and bands -------------------------------------------


def _references(doc: Document, chart_id: str) -> list[BaseElement]:
    return _wearing(doc, chart_id, "default-reference")


def _gridline_at(doc: Document, chart_id: str, index: int) -> float:
    """The y of the index-th horizontal gridline, counting from the bottom of the plot."""
    ys = sorted(
        (float(str(line.get("y1"))) for line in _wearing(doc, chart_id, "default-gridline")),
        reverse=True,
    )
    return ys[index]


def test_a_reference_line_lands_on_the_value_it_names() -> None:
    doc = _doc()
    placed = _bar_chart(doc, [10.0, 73.0], AxesSpec(reference_lines=[ReferenceLine(value=60)]))
    line = next(node for node in _references(doc, placed.ref.id) if str(node.tag).endswith("line"))
    # The ticks are 0/20/40/60/80, so 60 is the fourth gridline up — and the reference is on it.
    assert float(str(line.get("y1"))) == pytest.approx(_gridline_at(doc, placed.ref.id, 3))
    assert line.get("y1") == line.get("y2")


def test_a_reference_line_lands_on_the_value_it_names_on_a_log_axis_too() -> None:
    doc = _doc()
    placed = _bar_chart(
        doc,
        [1.0, 100.0],
        AxesSpec(scale="log", y_min=1, y_max=100, ticks=[1, 10, 100],
                 reference_lines=[ReferenceLine(value=10)]),
    )
    line = next(node for node in _references(doc, placed.ref.id) if str(node.tag).endswith("line"))
    # Halfway up a two-decade axis, which is what makes it a log scale and not a linear one.
    x, top, w, h = _plot_box(doc, placed.ref.id)
    assert float(str(line.get("y1"))) == pytest.approx(top + h / 2.0, abs=0.5)


def test_a_reference_label_is_right_aligned_at_the_end_of_its_line() -> None:
    doc = _doc()
    placed = _bar_chart(
        doc, [10.0, 73.0], AxesSpec(reference_lines=[ReferenceLine(value=60, label="target")])
    )
    label = next(
        node for node in _wearing(doc, placed.ref.id, "default-reference-label")
    )
    x, top, w, _ = _plot_box(doc, placed.ref.id)
    assert str(label.text) == "target"
    assert "text-anchor:end" in str(label.get("style")).replace(" ", "")
    assert float(str(label.get("x"))) == pytest.approx(x + w, abs=4.0)


def test_a_reference_off_the_end_of_the_axis_is_simply_not_drawn() -> None:
    doc = _doc()
    placed = _bar_chart(doc, [10.0, 73.0], AxesSpec(reference_lines=[ReferenceLine(value=500)]))
    assert _references(doc, placed.ref.id) == []


def test_a_reference_band_is_clamped_to_the_axis_and_drawn_behind_the_bars() -> None:
    doc = _doc()
    placed = _bar_chart(
        doc, [10.0, 73.0], AxesSpec(reference_lines=[ReferenceLine(value=-50, to=40)])
    )
    band = next(node for node in _references(doc, placed.ref.id) if str(node.tag).endswith("rect"))
    _, top, _, height = _plot_box(doc, placed.ref.id)
    # -50 is off the bottom, so the band starts at the axis floor: it fills the bottom half.
    assert float(str(band.get("y"))) + float(str(band.get("height"))) == pytest.approx(top + height)
    assert float(str(band.get("height"))) == pytest.approx(height / 2.0, abs=0.5)
    assert "default-reference-band" in str(band.get("class"))

    children = [child for child in doc.resolve(placed.ref.id) if isinstance(child.tag, str)]
    series = next(
        index
        for index, child in enumerate(children)
        if str(child.get("class") or "").startswith("default-series")
    )
    assert children.index(band) < series


def test_a_reference_line_is_drawn_over_the_bars_it_judges() -> None:
    doc = _doc()
    placed = _bar_chart(doc, [10.0, 73.0], AxesSpec(reference_lines=[ReferenceLine(value=60)]))
    children = [child for child in doc.resolve(placed.ref.id) if isinstance(child.tag, str)]
    line = next(node for node in _references(doc, placed.ref.id) if str(node.tag).endswith("line"))
    series = next(
        index
        for index, child in enumerate(children)
        if str(child.get("class") or "").startswith("default-series")
    )
    assert children.index(line) > series


def test_a_value_reference_turns_with_the_bars_it_is_drawn_across() -> None:
    doc = _doc()
    placed = ops.add_chart(
        doc,
        kind="bar",
        data=BarData(
            categories=["a", "b"],
            series=[Series(name="s", values=[10.0, 73.0])],
            orientation="horizontal",
        ),
        x=0,
        y=0,
        axes=AxesSpec(reference_lines=[ReferenceLine(value=60)]),
    )
    line = next(node for node in _references(doc, placed.ref.id) if str(node.tag).endswith("line"))
    # The value axis is x now, so the threshold is a VERTICAL rule three quarters of the way over.
    x, _, w, _ = _plot_box(doc, placed.ref.id)
    assert line.get("x1") == line.get("x2")
    assert float(str(line.get("x1"))) == pytest.approx(x + w * 0.75, abs=0.5)


def test_a_reference_on_a_numeric_x_is_drawn_and_on_a_categorical_one_is_not() -> None:
    doc = _doc()
    on_x = ops.add_chart(
        doc,
        kind="line",
        data=LineData(series=[PointSeries(name="s", points=[(0, 1), (4, 5)])]),
        x=0,
        y=0,
        axes=AxesSpec(reference_lines=[ReferenceLine(value=2, axis="x")]),
    )
    line = next(node for node in _references(doc, on_x.ref.id) if str(node.tag).endswith("line"))
    assert line.get("x1") == line.get("x2")

    categorical = _bar_chart(
        doc, [10.0, 73.0], AxesSpec(reference_lines=[ReferenceLine(value=2, axis="x")])
    )
    assert _references(doc, categorical.ref.id) == []


# --- 17. horizontal bars ------------------------------------------------------


def _horizontal(doc: Document, categories: list[str], values: list[float]) -> ops.PlacedChart:
    return ops.add_chart(
        doc,
        kind="bar",
        data=BarData(
            categories=categories,
            series=[Series(name="s", values=values)],
            orientation="horizontal",
            value_labels=True,
        ),
        x=0,
        y=0,
        width=320,
        height=200,
    )


def test_horizontal_bars_measure_the_left_margin_from_the_category_names() -> None:
    doc = _doc()
    short = _horizontal(doc, ["a", "b"], [10.0, 73.0])
    long = _horizontal(doc, ["a very long category name", "b"], [10.0, 73.0])
    assert _plot_box(doc, long.ref.id)[0] > _plot_box(doc, short.ref.id)[0] + 20.0


def test_horizontal_bars_grow_along_x_from_the_value_axis() -> None:
    doc = _doc()
    placed = _horizontal(doc, ["a", "b"], [10.0, 73.0])
    x, _, w, _ = _plot_box(doc, placed.ref.id)
    small, big = _series_rects(doc, placed.ref.id)
    assert small[0] == pytest.approx(x) and big[0] == pytest.approx(x)  # both start at zero
    assert big[2] > small[2] * 5  # 73 is a lot more than 10
    assert big[1] > small[1]  # and the categories run down the side, in order


def test_a_horizontal_bar_writes_its_value_to_the_right_of_its_end() -> None:
    doc = _doc()
    placed = _horizontal(doc, ["a", "b"], [10.0, 73.0])
    labels = _labels_at(doc, placed.ref.id)
    small, _ = _series_rects(doc, placed.ref.id)
    assert labels["10"][0] > small[0] + small[2]


def test_horizontal_bars_turn_the_gridlines_with_the_axes() -> None:
    doc = _doc()
    placed = _horizontal(doc, ["a", "b"], [10.0, 73.0])
    grid = _wearing(doc, placed.ref.id, "default-gridline")
    assert grid and all(line.get("x1") == line.get("x2") for line in grid)


# --- 18. stacked bars ---------------------------------------------------------


def _stacked(
    doc: Document,
    series: list[Series],
    axes: AxesSpec | None = None,
    *,
    value_labels: bool = False,
    stack_total_labels: bool = False,
) -> ops.PlacedChart:
    return ops.add_chart(
        doc,
        kind="bar",
        data=BarData(
            categories=[f"c{index}" for index in range(len(series[0].values))],
            series=series,
            stacked=True,
            value_labels=value_labels,
            stack_total_labels=stack_total_labels,
        ),
        x=0,
        y=0,
        width=320,
        height=200,
        axes=axes,
    )


def test_a_stacked_segment_starts_where_the_one_below_it_ended() -> None:
    doc = _doc()
    placed = _stacked(
        doc, [Series(name="one", values=[10.0]), Series(name="two", values=[15.0])]
    )
    lower = _series_rects(doc, placed.ref.id, 0)[0]
    upper = _series_rects(doc, placed.ref.id, 1)[0]
    assert upper[1] + upper[3] == pytest.approx(lower[1])  # they touch, and do not overlap
    assert upper[0] == lower[0] and upper[2] == lower[2]  # one band, shared


def test_a_stack_scales_its_axis_to_the_totals_and_not_to_the_parts() -> None:
    doc = _doc()
    stacked = _stacked(
        doc, [Series(name="one", values=[60.0]), Series(name="two", values=[60.0])]
    )
    grouped = _bar_chart(doc, [60.0])
    # The stack is 120 tall, so the axis has to reach past 120 — where the same numbers drawn
    # side by side need an axis of 60.
    assert float(_texts(doc, stacked.ref.id, "default-tick-label")[-2]) >= 120.0
    assert float(_texts(doc, grouped.ref.id, "default-tick-label")[-2]) < 120.0


def test_a_negative_series_stacks_downward_from_zero() -> None:
    doc = _doc()
    placed = _stacked(
        doc, [Series(name="up", values=[10.0]), Series(name="down", values=[-5.0])]
    )
    up = _series_rects(doc, placed.ref.id, 0)[0]
    down = _series_rects(doc, placed.ref.id, 1)[0]
    assert down[1] == pytest.approx(up[1] + up[3])  # it hangs off zero, under the positive bar


def test_a_stacked_segment_too_short_for_its_number_goes_unlabelled() -> None:
    doc = _doc()
    placed = _stacked(
        doc,
        [Series(name="big", values=[100.0]), Series(name="sliver", values=[0.4])],
        value_labels=True,
    )
    assert set(_labels_at(doc, placed.ref.id)) == {"100"}


def test_a_stack_total_is_written_beyond_the_end_of_the_stack() -> None:
    doc = _doc()
    placed = _stacked(
        doc,
        [Series(name="one", values=[10.0]), Series(name="two", values=[15.0])],
        AxesSpec(y_max=100),  # headroom, so the total is not pushed inside by the plot edge
        stack_total_labels=True,
    )
    top = _series_rects(doc, placed.ref.id, 1)[0]
    assert _labels_at(doc, placed.ref.id)["25"][1] < top[1]


def test_stacking_works_turned_on_its_side_too() -> None:
    doc = _doc()
    placed = ops.add_chart(
        doc,
        kind="bar",
        data=BarData(
            categories=["c"],
            series=[Series(name="one", values=[10.0]), Series(name="two", values=[15.0])],
            stacked=True,
            orientation="horizontal",
        ),
        x=0,
        y=0,
    )
    lower = _series_rects(doc, placed.ref.id, 0)[0]
    upper = _series_rects(doc, placed.ref.id, 1)[0]
    assert upper[0] == pytest.approx(lower[0] + lower[2])
    assert upper[1] == lower[1] and upper[3] == lower[3]


def test_a_stack_on_a_log_axis_is_refused_rather_than_fudged() -> None:
    doc = _doc()
    with pytest.raises(InvalidArgument, match="no zero"):
        ops.add_chart(
            doc,
            kind="bar",
            data=BarData(
                categories=["c"], series=[Series(name="one", values=[10.0])], stacked=True
            ),
            x=0,
            y=0,
            axes=AxesSpec(scale="log"),
        )


# --- 19. ordering and steps ---------------------------------------------------


def _drawn_categories(doc: Document, chart_id: str, count: int) -> list[str]:
    """The category names, in the order the chart drew them along the x axis."""
    return _texts(doc, chart_id, "default-tick-label")[-count:]


def _ordered(doc: Document, order: object) -> list[str]:
    placed = ops.add_chart(
        doc,
        kind="bar",
        data=BarData(
            categories=["a", "b", "c"],
            series=[Series(name="s", values=[10.0, 73.0, 42.0])],
            order=order,  # type: ignore[arg-type]
        ),
        x=0,
        y=0,
    )
    return _drawn_categories(doc, placed.ref.id, 3)


def test_every_ordering_mode_draws_the_categories_in_its_own_order() -> None:
    doc = _doc()
    assert _ordered(doc, "given") == ["a", "b", "c"]
    assert _ordered(doc, "value_desc") == ["b", "c", "a"]
    assert _ordered(doc, "value_asc") == ["a", "c", "b"]
    assert _ordered(doc, "label") == ["a", "b", "c"]
    assert _ordered(doc, ["c", "a"]) == ["c", "a", "b"]


def test_a_multi_series_bar_orders_by_the_first_series() -> None:
    doc = _doc()
    placed = ops.add_chart(
        doc,
        kind="bar",
        data=BarData(
            categories=["a", "b", "c"],
            series=[
                Series(name="one", values=[10.0, 73.0, 42.0]),
                Series(name="two", values=[99.0, 1.0, 1.0]),
            ],
            order="value_desc",
        ),
        x=0,
        y=0,
    )
    assert _drawn_categories(doc, placed.ref.id, 3) == ["b", "c", "a"]


def test_a_stack_orders_by_the_total_because_no_one_series_is_the_subject() -> None:
    doc = _doc()
    placed = ops.add_chart(
        doc,
        kind="bar",
        data=BarData(
            categories=["a", "b", "c"],
            series=[
                Series(name="one", values=[10.0, 73.0, 42.0]),
                Series(name="two", values=[99.0, 1.0, 1.0]),
            ],
            stacked=True,
            order="value_desc",
        ),
        x=0,
        y=0,
    )
    assert _drawn_categories(doc, placed.ref.id, 3) == ["a", "b", "c"]


def test_ordering_by_a_category_the_chart_has_not_got_is_refused_by_name() -> None:
    doc = _doc()
    with pytest.raises(InvalidArgument, match="'nope'"):
        ops.add_chart(
            doc,
            kind="bar",
            data=BarData(
                categories=["a"], series=[Series(name="s", values=[1.0])], order=["nope"]
            ),
            x=0,
            y=0,
        )


def _polyline_points(doc: Document, chart_id: str) -> list[tuple[float, float]]:
    line = next(
        node for node in doc.resolve(chart_id).iter() if str(node.tag).endswith("polyline")
    )
    pairs = str(line.get("points")).replace(",", " ").split()
    return [(float(pairs[i]), float(pairs[i + 1])) for i in range(0, len(pairs), 2)]


def _step_chart(doc: Document, mode: str) -> ops.PlacedChart:
    return ops.add_chart(
        doc,
        kind="line",
        data=LineData(
            series=[PointSeries(name="s", points=[(0, 1), (1, 4)])],
            step=mode,  # type: ignore[arg-type]
        ),
        x=0,
        y=0,
    )


def test_a_post_step_polyline_holds_then_rises() -> None:
    doc = _doc()
    placed = _step_chart(doc, "post")
    (x0, y0), (x1, y1), (x2, y2) = _polyline_points(doc, placed.ref.id)
    assert (x1, y1) == pytest.approx((x2, y0))  # along at the old value, then up at the new x
    assert y2 < y0


def test_a_pre_step_polyline_rises_then_holds() -> None:
    doc = _doc()
    placed = _step_chart(doc, "pre")
    (x0, y0), (x1, y1), (x2, y2) = _polyline_points(doc, placed.ref.id)
    assert (x1, y1) == pytest.approx((x0, y2))  # up at the old x, then along at the new value


def test_an_area_wash_follows_the_staircase_and_not_the_slope() -> None:
    doc = _doc()
    placed = ops.add_chart(
        doc,
        kind="line",
        data=LineData(series=[PointSeries(name="s", points=[(0, 1), (1, 4)])], step="post",
                      area=True),
        x=0,
        y=0,
    )
    path = next(
        node for node in doc.resolve(placed.ref.id).iter() if str(node.tag).endswith("path")
    )
    # Three outline points (hold, rise) plus the corner the wash closes on, where the sloped
    # version of the same series would have two and two.
    assert str(path.get("d")).count("L") == 4


# --- 20. donut extras ---------------------------------------------------------


def test_a_donut_hole_carries_the_number_and_its_caption_centred() -> None:
    doc = _doc()
    placed = ops.add_chart(
        doc,
        kind="donut",
        data=DonutData(
            slices=[Slice(label="a", value=60), Slice(label="b", value=40)],
            center_text="97",
            center_subtext="total units",
        ),
        x=0,
        y=0,
        width=320,
        height=200,
    )
    big = _wearing(doc, placed.ref.id, "default-donut-center")[0]
    small = _wearing(doc, placed.ref.id, "default-donut-subtext")[0]
    assert (str(big.text), str(small.text)) == ("97", "total units")
    assert float(str(big.get("x"))) == pytest.approx(160.0)  # the ring's own centre
    assert float(str(small.get("x"))) == pytest.approx(160.0)
    assert float(str(small.get("y"))) > float(str(big.get("y")))


def test_a_donut_with_no_centre_text_writes_none_of_it() -> None:
    doc = _doc()
    placed = ops.add_chart(
        doc, kind="donut", data=DonutData(slices=[Slice(label="a", value=1)]), x=0, y=0
    )
    assert _wearing(doc, placed.ref.id, "default-donut-center") == []


def test_slice_labels_name_each_slice_outside_the_ring() -> None:
    doc = _doc()
    placed = ops.add_chart(
        doc,
        kind="donut",
        data=DonutData(
            slices=[Slice(label="a", value=60), Slice(label="b", value=40)], slice_labels=True
        ),
        x=0,
        y=0,
    )
    assert set(_labels_at(doc, placed.ref.id)) == {"a 60", "b 40"}


def test_a_thin_slice_gets_a_stub_to_hang_its_label_off() -> None:
    doc = _doc()
    fat = ops.add_chart(
        doc,
        kind="donut",
        data=DonutData(
            slices=[Slice(label="a", value=50), Slice(label="b", value=50)], slice_labels=True
        ),
        x=0,
        y=0,
    )
    thin = ops.add_chart(
        doc,
        kind="donut",
        data=DonutData(
            slices=[Slice(label="a", value=99), Slice(label="b", value=1)], slice_labels=True
        ),
        x=0,
        y=0,
    )
    lines = [_tags_under(doc, chart.ref.id).count("line") for chart in (fat, thin)]
    assert lines == [0, 1]


def test_a_donut_still_starts_at_twelve_o_clock_when_nobody_says_otherwise() -> None:
    assert donut_angles([1.0, 1.0])[0][0] == -math.pi / 2.0
    doc = _doc()
    placed = ops.add_chart(
        doc,
        kind="donut",
        data=DonutData(slices=[Slice(label="a", value=60), Slice(label="b", value=40)]),
        x=0,
        y=0,
        width=320,
        height=200,
    )
    wedge = next(
        node for node in doc.resolve(placed.ref.id).iter() if str(node.tag).endswith("path")
    )
    # The first wedge opens straight up from the centre: (cx, cy - outer).
    start = str(wedge.get("d")).split()[1:3]
    assert [float(value) for value in start] == pytest.approx([160.0, 4.0], abs=0.5)


def test_a_start_angle_turns_the_whole_ring() -> None:
    doc = _doc()
    placed = ops.add_chart(
        doc,
        kind="donut",
        data=DonutData(
            slices=[Slice(label="a", value=60), Slice(label="b", value=40)], start_angle=0.0
        ),
        x=0,
        y=0,
        width=320,
        height=200,
    )
    wedge = next(
        node for node in doc.resolve(placed.ref.id).iter() if str(node.tag).endswith("path")
    )
    start = str(wedge.get("d")).split()[1:3]
    assert [float(value) for value in start] == pytest.approx([256.0, 100.0], abs=0.5)


def test_a_donut_orders_its_slices_the_way_it_is_asked_to() -> None:
    doc = _doc()
    placed = ops.add_chart(
        doc,
        kind="donut",
        data=DonutData(
            slices=[
                Slice(label="a", value=10),
                Slice(label="b", value=60),
                Slice(label="c", value=30),
            ],
            order="value_desc",
        ),
        x=0,
        y=0,
    )
    drawn = [
        str(node.get("data-slice"))
        for node in doc.resolve(placed.ref.id)
        if node.get("data-slice") is not None
    ]
    assert drawn == ["b", "c", "a"]


def test_a_stack_total_label_silences_the_segment_label_it_would_sit_on() -> None:
    # A stack reaching the plot edge flips its total INSIDE, onto the top segment's own
    # label; two numbers in one place read as neither, so the segment's is dropped.
    doc = _doc()
    ref = ops.add_chart(
        doc,
        kind="bar",
        data=BarData(
            categories=["Q4"],
            series=[Series(name="core", values=[52.0]), Series(name="addon", values=[22.0])],
            stacked=True,
            stack_total_labels=True,
            value_labels=True,
        ),
        axes=AxesSpec(y_max=74.0),  # the stack ends exactly at the top: the total must flip in
        width=260,
        height=180,
    )
    labels = [
        (node.text or "")
        for node in doc.resolve(ref.ref.id).iter()
        if str(node.TAG).endswith("text")
    ]
    assert "74" in " ".join(labels)  # the total is drawn
    assert "52" in " ".join(labels)  # the roomy bottom segment keeps its label
    assert "22" not in " ".join(labels)  # the one the total landed on is silenced


# --- 21. the mark vocabulary --------------------------------------------------


def _children(doc: Document, node_id: str) -> list[BaseElement]:
    return [child for child in doc.resolve(node_id) if isinstance(child.tag, str)]


def _circles(doc: Document, chart_id: str) -> list[BaseElement]:
    return [
        node
        for node in doc.resolve(chart_id).iter()
        if isinstance(node.tag, str) and str(node.tag).endswith("circle")
    ]


def _paths(doc: Document, chart_id: str) -> list[BaseElement]:
    return [
        node
        for node in doc.resolve(chart_id).iter()
        if isinstance(node.tag, str) and str(node.tag).endswith("path")
    ]


def test_a_downward_triangle_is_the_triangle_turned_over() -> None:
    up = marker_points("triangle", 0.0, 0.0, 10.0)
    down = marker_points("tri_down", 0.0, 0.0, 10.0)
    assert len(up) == len(down) == 3
    assert min(y for _, y in up) == pytest.approx(-10.0)
    assert max(y for _, y in down) == pytest.approx(10.0)
    assert sorted(round(-y, 6) for _, y in up) == sorted(round(y, 6) for _, y in down)


def test_a_star_alternates_ten_corners_between_two_radii() -> None:
    corners = marker_points("star", 0.0, 0.0, 10.0)
    assert len(corners) == 10
    radii = [math.hypot(x, y) for x, y in corners]
    assert radii[0::2] == pytest.approx([10.0] * 5)
    assert radii[1::2] == pytest.approx([3.82] * 5)
    assert corners[0][1] == pytest.approx(-10.0)  # a star points up


def test_the_marks_with_no_area_are_strokes_and_not_polygons() -> None:
    stroked: tuple[Marker, ...] = ("plus", "cross")
    for shape in stroked:
        assert marker_points(shape, 0.0, 0.0, 5.0) == []
        arms = marker_strokes(shape, 0.0, 0.0, 5.0)
        assert len(arms) == 2
        for start, end in arms:
            # both arms reach as far as the circle they stand in for, so no shape carries
            # visibly more ink than another at the same size
            assert math.dist(start, end) == pytest.approx(10.0)
    assert marker_strokes("circle", 0.0, 0.0, 5.0) == []
    assert marker_strokes("square", 0.0, 0.0, 5.0) == []


@pytest.mark.parametrize(
    ("shape", "tag"), [("tri_down", "path"), ("star", "path"), ("square", "rect")]
)
def test_each_new_filled_glyph_draws_its_own_kind_of_element(shape: str, tag: str) -> None:
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


def test_a_stroked_mark_is_one_unfilled_path_wearing_its_series() -> None:
    doc = _doc()
    placed = ops.add_chart(
        doc,
        kind="scatter",
        data=ScatterData(series=[PointSeries(name="s", points=[(1, 2), (2, 3)])], marker="plus"),
    )
    marks = _paths(doc, placed.ref.id)
    assert len(marks) == 2
    for node in marks:
        assert "fill:none" in str(node.get("style"))
        assert "default-series-1" in str(node.get("class") or "").split()
        assert str(node.get("d")).count("M") == 2  # two arms, one element


def test_open_marks_are_hollow_and_painted_by_their_stroke() -> None:
    doc = _doc()
    hollow = ops.add_chart(
        doc,
        kind="scatter",
        data=ScatterData(series=[PointSeries(name="s", points=[(1, 2), (2, 3)])], open=True),
    )
    marks = _circles(doc, hollow.ref.id)
    assert len(marks) == 2
    for node in marks:
        style = str(node.get("style"))
        assert "fill:none" in style and "stroke-width:1.5" in style
        assert "default-series-1" in str(node.get("class") or "").split()

    filled = ops.add_chart(
        doc, kind="scatter", data=ScatterData(series=[PointSeries(name="s", points=[(1, 2)])])
    )
    solid = _circles(doc, filled.ref.id)
    # ...where a filled mark takes the opposite deal: no stroke, and the group above paints it
    assert [str(node.get("class") or "") for node in solid] == [""]
    assert "stroke:none" in str(solid[0].get("style"))


def test_a_bubble_maps_its_numbers_by_area_and_not_by_radius() -> None:
    radii = marker_radii([0.0, 50.0, 100.0], (2.0, 10.0), (0.0, 100.0))
    assert radii[0] == pytest.approx(2.0)
    assert radii[-1] == pytest.approx(10.0)
    # halfway up the range is halfway up the AREA, which is a radius of the root mean square
    assert radii[1] == pytest.approx(math.sqrt((4.0 + 100.0) / 2.0))


def test_sizes_with_no_scale_are_radii_already() -> None:
    assert marker_radii([3.0, 7.0], None, (3.0, 7.0)) == [3.0, 7.0]


def test_one_distinct_size_draws_every_bubble_at_the_top_of_the_range() -> None:
    assert marker_radii([4.0, 4.0], (2.0, 9.0), (4.0, 4.0)) == pytest.approx([9.0, 9.0])


def test_a_bubble_needs_one_size_per_point_and_none_of_them_negative() -> None:
    with pytest.raises(ValidationError):
        PointSeries(name="s", points=[(0, 1), (1, 2)], sizes=[3.0])
    with pytest.raises(ValidationError):
        PointSeries(name="s", points=[(0, 1)], sizes=[-1.0])


def test_a_bubble_scatter_draws_visibly_different_radii() -> None:
    doc = _doc()
    placed = ops.add_chart(
        doc,
        kind="scatter",
        data=ScatterData(
            series=[PointSeries(name="s", points=[(1, 1), (2, 2), (3, 3)], sizes=[1, 4, 16])],
            marker_scale=(3.0, 12.0),
        ),
        x=0,
        y=0,
        width=320,
        height=200,
    )
    radii = [float(str(node.get("r"))) for node in _circles(doc, placed.ref.id)]
    assert len(radii) == 3
    assert radii == sorted(radii)
    assert radii[0] == pytest.approx(3.0) and radii[-1] == pytest.approx(12.0)
    assert radii[-1] - radii[0] > 5.0


def test_a_bubble_pushes_its_value_label_clear_of_its_own_edge() -> None:
    doc = _doc()
    points = [PointSeries(name="s", points=[(1, 1), (2, 2)], sizes=[1.0, 1.0])]
    small = ops.add_chart(
        doc,
        kind="scatter",
        data=ScatterData(series=points, marker_scale=(2.0, 2.0), value_labels=True),
        x=0,
        y=0,
    )
    big = ops.add_chart(
        doc,
        kind="scatter",
        data=ScatterData(series=points, marker_scale=(20.0, 20.0), value_labels=True),
        x=0,
        y=0,
    )
    # The plots are identical but for the marks' size, so the labels differ by exactly that.
    assert _labels_at(doc, big.ref.id)["1"][1] == pytest.approx(
        _labels_at(doc, small.ref.id)["1"][1] - 18.0
    )


def test_markevery_thins_the_marks_and_leaves_the_line_whole() -> None:
    doc = _doc()
    placed = ops.add_chart(
        doc,
        kind="line",
        data=LineData(
            series=[PointSeries(name="s", points=[(index, index % 3) for index in range(10)])],
            points=True,
            markevery=3,
        ),
    )
    tags = _tags_under(doc, placed.ref.id)
    assert tags.count("circle") == 4  # points 0, 3, 6 and 9
    assert tags.count("polyline") == 1


# --- 22. minor ticks, tick direction, inversion and the zero spine -------------


def test_minor_ticks_cut_each_major_interval_into_the_count_plus_one() -> None:
    assert minor_ticks([0.0, 10.0, 20.0], 4) == pytest.approx([2, 4, 6, 8, 12, 14, 16, 18])
    assert minor_ticks([0.0, 10.0], 1) == pytest.approx([5.0])
    assert minor_ticks([0.0], 4) == []  # nothing to subdivide between


def test_a_log_axis_subdivides_by_mantissa_whatever_count_it_is_given() -> None:
    found = minor_ticks([1.0, 10.0, 100.0], 4, log=True)
    assert found == pytest.approx([2, 3, 4, 5, 6, 7, 8, 9, 20, 30, 40, 50, 60, 70, 80, 90])
    assert minor_ticks([1.0, 10.0], 1, log=True) == minor_ticks([1.0, 10.0], 7, log=True)


def test_minor_gridlines_and_marks_are_drawn_at_half_the_majors_length() -> None:
    doc = _doc()
    placed = _bar_chart(
        doc,
        [0.0, 100.0],
        AxesSpec(
            y_min=0,
            y_max=100,
            ticks=[0, 50, 100],
            minor=1,
            minor_gridlines=True,
            tick_marks=6,
        ),
    )
    grid = _wearing(doc, placed.ref.id, "default-gridline-minor")
    assert len(grid) == 2  # 25 and 75
    # a minor gridline is still a gridline: the extra class is what makes it the fainter one
    assert all("default-gridline" in str(node.get("class") or "").split() for node in grid)
    marks = _wearing(doc, placed.ref.id, "default-tick-minor")
    assert len(marks) == 2
    for node in marks:
        assert abs(float(str(node.get("x2"))) - float(str(node.get("x1")))) == pytest.approx(3.0)


def test_minor_positions_are_drawn_for_nobody_who_did_not_ask() -> None:
    doc = _doc()
    placed = _bar_chart(doc, [0.0, 100.0], AxesSpec(minor=4, tick_marks=6))
    assert _wearing(doc, placed.ref.id, "default-gridline-minor") == []
    assert _wearing(doc, placed.ref.id, "default-tick-minor") != []  # marks: tick_marks is set
    bare = _bar_chart(doc, [0.0, 100.0], AxesSpec(minor=4, minor_gridlines=True))
    assert _wearing(doc, bare.ref.id, "default-tick-minor") == []  # no tick_marks, no marks


def test_a_tick_stands_out_in_or_both_ways() -> None:
    assert tick_reach(6.0, "out") == (6.0, 0.0)
    assert tick_reach(6.0, "in") == (0.0, 6.0)
    assert tick_reach(6.0, "inout") == (6.0, 6.0)
    assert tick_reach(-4.0, "out") == (0.0, 0.0)


def test_an_inward_tick_is_drawn_inside_the_plot_and_costs_the_margin_nothing() -> None:
    doc = _doc()
    bare = _bar_chart(doc, [0.0, 100.0])
    inward = _bar_chart(doc, [0.0, 100.0], AxesSpec(tick_marks=8, tick_direction="in"))
    assert _axis_x(doc, inward.ref.id) == pytest.approx(_axis_x(doc, bare.ref.id))
    axis_x = _axis_x(doc, inward.ref.id)
    marks = [
        node
        for node in _wearing(doc, inward.ref.id, "default-tick")
        if node.get("y1") == node.get("y2")
    ]
    assert marks
    for node in marks:
        assert float(str(node.get("x1"))) == pytest.approx(axis_x)
        assert float(str(node.get("x2"))) == pytest.approx(axis_x + 8.0)


def test_a_straddling_tick_reaches_both_ways_from_its_axis() -> None:
    doc = _doc()
    placed = _bar_chart(doc, [0.0, 100.0], AxesSpec(tick_marks=5, tick_direction="inout"))
    axis_x = _axis_x(doc, placed.ref.id)
    marks = [
        node
        for node in _wearing(doc, placed.ref.id, "default-tick")
        if node.get("y1") == node.get("y2")
    ]
    for node in marks:
        assert float(str(node.get("x1"))) == pytest.approx(axis_x - 5.0)
        assert float(str(node.get("x2"))) == pytest.approx(axis_x + 5.0)


def test_turning_the_y_tick_labels_turns_them_and_widens_the_margin() -> None:
    doc = _doc()
    level = _bar_chart(doc, [0.0, 100000.0])
    turned = _bar_chart(doc, [0.0, 100000.0], AxesSpec(y_tick_rotate=-45))
    assert _axis_x(doc, turned.ref.id) < _axis_x(doc, level.ref.id)
    labels = [
        node
        for node in _wearing(doc, turned.ref.id, "default-tick-label")
        if str(node.text or "") == "100000"
    ]
    assert labels and labels[0].get("transform") is not None


def test_inverting_a_scale_counts_the_same_span_from_the_other_end() -> None:
    up = Scale(lo=0.0, hi=10.0)
    down = Scale(lo=0.0, hi=10.0, invert=True)
    for value in (0.0, 2.5, 10.0, 12.0):
        assert down.unit(value) == pytest.approx(1.0 - up.unit(value))
    assert Scale(lo=1.0, hi=100.0, log=True, invert=True).unit(10.0) == pytest.approx(0.5)


def test_an_inverted_value_axis_hangs_the_bars_from_the_top() -> None:
    doc = _doc()
    normal = _bar_chart(doc, [10.0, 80.0])
    flipped = _bar_chart(doc, [10.0, 80.0], AxesSpec(invert_y=True))
    tall = _series_rects(doc, normal.ref.id)[1]
    hung = _series_rects(doc, flipped.ref.id)[1]
    assert hung[1] == pytest.approx(_plot_box(doc, flipped.ref.id)[1])  # starts at the plot top
    assert hung[3] == pytest.approx(tall[3])  # the same length, the other way up


def test_the_zero_spine_stands_on_zero_only_when_zero_is_on_the_axis() -> None:
    doc = _doc()
    crossing = _bar_chart(doc, [12.0, -14.0], AxesSpec(zero_spine=True))
    lines = _wearing(doc, crossing.ref.id, "default-axis")
    base = next(node for node in lines if node.get("y1") == node.get("y2"))
    zero = next(
        node
        for node in _wearing(doc, crossing.ref.id, "default-tick-label")
        if str(node.text or "") == "0"
    )
    assert float(str(base.get("y1"))) == pytest.approx(float(str(zero.get("y"))))
    # ...and the reading edge did not move with it: the labels are still down the left side
    _, top, _, height = _plot_box(doc, crossing.ref.id)
    assert top < float(str(base.get("y1"))) < top + height

    above = _bar_chart(doc, [10.0, 80.0], AxesSpec(zero_spine=True, y_min=5.0, y_max=90.0))
    edge = next(
        node
        for node in _wearing(doc, above.ref.id, "default-axis")
        if node.get("y1") == node.get("y2")
    )
    box_top, box_height = _plot_box(doc, above.ref.id)[1], _plot_box(doc, above.ref.id)[3]
    assert float(str(edge.get("y1"))) == pytest.approx(box_top + box_height)


# --- 23. line composition: mid steps and bands --------------------------------


def test_a_mid_step_changes_halfway_between_the_two_readings() -> None:
    assert step_points([(0.0, 1.0), (2.0, 5.0)], "mid") == [
        (0.0, 1.0),
        (1.0, 1.0),
        (1.0, 5.0),
        (2.0, 5.0),
    ]


def test_a_band_has_to_name_series_this_chart_has() -> None:
    with pytest.raises(ValidationError) as caught:
        LineData(
            series=[PointSeries(name="a", points=[(0, 1)])],
            bands=[SeriesBand(between=("a", "b"))],
        )
    assert "'b'" in str(caught.value)


def test_a_band_needs_the_two_series_sampled_at_the_same_x() -> None:
    with pytest.raises(ValidationError) as caught:
        LineData(
            series=[
                PointSeries(name="a", points=[(0, 1), (1, 2)]),
                PointSeries(name="b", points=[(0, 3), (2, 4)]),
            ],
            bands=[SeriesBand(between=("a", "b"))],
        )
    assert "SAME x" in str(caught.value)


def _banded(doc: Document) -> ops.PlacedChart:
    return ops.add_chart(
        doc,
        kind="line",
        data=LineData(
            series=[
                PointSeries(name="lo", points=[(0, 1), (1, 2), (2, 1.5)]),
                PointSeries(name="hi", points=[(0, 4), (1, 6), (2, 3)]),
            ],
            bands=[SeriesBand(between=("lo", "hi"), label="range")],
        ),
        x=0,
        y=0,
        width=320,
        height=200,
    )


def test_a_band_is_drawn_behind_both_of_the_lines_it_lies_between() -> None:
    doc = _doc()
    placed = _banded(doc)
    kids = _children(doc, placed.ref.id)
    classes = [str(child.get("class") or "") for child in kids]
    first_series = next(
        index for index, name in enumerate(classes) if name.startswith("default-series")
    )
    band_group = kids[first_series - 1]
    filled = [node for node in band_group.iter() if str(node.tag).endswith("path")]
    assert len(filled) == 1  # one region, before the first series group
    assert "fill-opacity:0.15" in str(filled[0].get("style"))
    # it wears the FIRST named series' class, so it reads as that family's range
    assert "default-series-1" in str(filled[0].get("class") or "").split()


def test_a_bands_label_sits_where_the_band_is_widest() -> None:
    doc = _doc()
    placed = _banded(doc)
    labels = _labels_at(doc, placed.ref.id)
    assert set(labels) == {"range"}
    # the widest gap is at x=1 (2 to 6), which is the middle of the three x positions
    left, _, width, _ = _plot_box(doc, placed.ref.id)
    assert labels["range"][0] == pytest.approx(left + width / 2.0, abs=0.01)


# --- 24. waterfalls, normalized stacks and pyramids ---------------------------


def test_a_waterfall_floats_each_bar_from_the_running_total() -> None:
    assert waterfall_segments([100.0, 45.0, -30.0]) == [
        Segment(0.0, 100.0),
        Segment(100.0, 145.0),
        Segment(115.0, 145.0),
    ]


def test_a_waterfall_total_is_one_more_bar_from_the_ground() -> None:
    assert waterfall_segments([10.0, -4.0], total=True)[-1] == Segment(0.0, 6.0)
    assert waterfall_segments([-10.0, 4.0], total=True)[-1] == Segment(-6.0, 0.0)


def test_a_waterfall_is_one_series_and_never_a_stack() -> None:
    with pytest.raises(ValidationError):
        BarData(
            categories=["a"],
            series=[Series(name="x", values=[1]), Series(name="y", values=[2])],
            waterfall=True,
        )
    with pytest.raises(ValidationError):
        BarData(
            categories=["a"],
            series=[Series(name="x", values=[1])],
            waterfall=True,
            stacked=True,
        )


def test_a_waterfall_draws_its_steps_its_total_and_the_connectors_between() -> None:
    doc = _doc()
    placed = ops.add_chart(
        doc,
        kind="bar",
        data=BarData(
            categories=["open", "up", "down"],
            series=[Series(name="arr", values=[100.0, 40.0, -25.0])],
            waterfall=True,
            total_label="close",
        ),
        x=0,
        y=0,
        width=360,
        height=240,
    )
    steps = _series_rects(doc, placed.ref.id, 0)
    total = _series_rects(doc, placed.ref.id, 1)
    assert len(steps) == 3 and len(total) == 1
    assert steps[1][1] + steps[1][3] == pytest.approx(steps[0][1])  # starts where 1 ended
    assert steps[2][1] == pytest.approx(steps[1][1])  # the fall hangs off the run's top
    assert total[0][3] == pytest.approx(steps[0][3] + steps[1][3] - steps[2][3])
    connectors = _wearing(doc, placed.ref.id, "default-waterfall-connector")
    assert len(connectors) == 3  # between the four bars
    for node in connectors:
        assert node.get("y1") == node.get("y2")
    assert float(str(connectors[0].get("y1"))) == pytest.approx(steps[0][1])


def test_a_waterfall_writes_the_signed_step_on_each_bar() -> None:
    doc = _doc()
    placed = ops.add_chart(
        doc,
        kind="bar",
        data=BarData(
            categories=["open", "down"],
            series=[Series(name="arr", values=[100.0, -25.0])],
            waterfall=True,
            total_label="close",
            value_labels=True,
        ),
        x=0,
        y=0,
        width=360,
        height=240,
    )
    assert set(_labels_at(doc, placed.ref.id)) == {"100", "-25", "75"}


def test_a_horizontal_waterfall_walks_along_x_instead() -> None:
    doc = _doc()
    placed = ops.add_chart(
        doc,
        kind="bar",
        data=BarData(
            categories=["open", "up"],
            series=[Series(name="arr", values=[10.0, 5.0])],
            waterfall=True,
            orientation="horizontal",
        ),
        x=0,
        y=0,
        width=360,
        height=240,
    )
    bars = _series_rects(doc, placed.ref.id)
    assert bars[1][0] == pytest.approx(bars[0][0] + bars[0][2])  # the second starts where 1 ended
    connectors = _wearing(doc, placed.ref.id, "default-waterfall-connector")
    assert len(connectors) == 1
    assert connectors[0].get("x1") == connectors[0].get("x2")  # a vertical join, turned with it


def test_normalizing_gives_every_stack_the_same_hundred_to_share() -> None:
    scaled = normalize_stacks([[10.0, 1.0], [30.0, 3.0]])
    assert [sum(row[index] for row in scaled) for index in range(2)] == pytest.approx([100.0] * 2)
    assert scaled[0] == pytest.approx([25.0, 25.0])


def test_a_stack_that_crosses_zero_normalizes_by_its_own_magnitude() -> None:
    scaled = normalize_stacks([[3.0], [-1.0]])
    assert scaled[0] == pytest.approx([75.0]) and scaled[1] == pytest.approx([-25.0])


def test_a_stack_of_nothing_has_no_total_to_take_shares_of() -> None:
    assert normalize_stacks([[0.0], [0.0]]) == [[0.0], [0.0]]


def test_normalizing_needs_a_stack_to_normalize() -> None:
    with pytest.raises(ValidationError):
        BarData(categories=["a"], series=[Series(name="s", values=[1.0])], normalized=True)


def test_normalized_stacks_all_reach_the_same_line() -> None:
    doc = _doc()
    placed = ops.add_chart(
        doc,
        kind="bar",
        data=BarData(
            categories=["a", "b"],
            series=[Series(name="x", values=[10, 30]), Series(name="y", values=[30, 10])],
            stacked=True,
            normalized=True,
            value_labels=True,
        ),
        x=0,
        y=0,
        width=320,
        height=200,
    )
    lower = _series_rects(doc, placed.ref.id, 0)
    upper = _series_rects(doc, placed.ref.id, 1)
    assert lower[0][3] + upper[0][3] == pytest.approx(lower[1][3] + upper[1][3])
    assert upper[0][1] == pytest.approx(upper[1][1])  # both stacks top out on the same line
    assert set(_labels_at(doc, placed.ref.id)) == {"25", "75"}


def test_a_population_pyramid_mirrors_two_series_about_zero() -> None:
    doc = _doc()
    placed = ops.add_chart(
        doc,
        kind="bar",
        data=BarData(
            categories=["0-9", "10-19"],
            series=[
                Series(name="men", values=[-5.0, -6.0]),
                Series(name="women", values=[5.0, 6.0]),
            ],
            orientation="horizontal",
            stacked=True,
        ),
        x=0,
        y=0,
        width=360,
        height=240,
        axes=AxesSpec(zero_spine=True),
    )
    left = _series_rects(doc, placed.ref.id, 0)
    right = _series_rects(doc, placed.ref.id, 1)
    for men, women in zip(left, right, strict=True):
        assert men[0] + men[2] == pytest.approx(women[0])  # they meet at zero
        assert men[2] == pytest.approx(women[2])  # and mirror each other's width
        assert men[1] == pytest.approx(women[1])  # in one band per age group
    spine = next(
        node
        for node in _wearing(doc, placed.ref.id, "default-axis")
        if node.get("x1") == node.get("x2")
    )
    assert float(str(spine.get("x1"))) == pytest.approx(left[0][0] + left[0][2])


# --- 25. histograms -----------------------------------------------------------


def test_an_integer_bin_count_spreads_equal_bins_across_the_data() -> None:
    assert histogram_edges([0.0, 10.0, 5.0], 5) == pytest.approx([0.0, 2.0, 4.0, 6.0, 8.0, 10.0])


def test_explicit_edges_are_taken_exactly_as_written() -> None:
    assert histogram_edges([1.0], [0.0, 1.0, 4.0]) == [0.0, 1.0, 4.0]


def test_a_value_on_an_edge_goes_to_the_bin_above_it_and_the_last_edge_closes() -> None:
    edges = [0.0, 1.0, 2.0]
    assert histogram_counts([0.0, 1.0, 2.0], edges) == [1, 2]
    assert histogram_counts([-0.5, 2.5], edges) == [0, 0]  # nothing outside the edges is counted


def test_a_histogram_of_one_repeated_value_still_gets_a_bin_to_stand_in() -> None:
    edges = histogram_edges([7.0, 7.0, 7.0], 10)
    assert len(edges) == 2 and edges[1] - edges[0] == pytest.approx(1.0)
    assert histogram_counts([7.0, 7.0, 7.0], edges) == [3]
    wide = histogram_edges([40.0, 40.0], 4)
    assert wide[1] - wide[0] == pytest.approx(4.0)  # the axes' own degenerate rule


def test_bins_have_to_be_a_count_or_an_ascending_ladder() -> None:
    ladders: tuple[int | list[float], ...] = (0, [1.0], [2.0, 1.0], [1.0, 1.0])
    for bins in ladders:
        with pytest.raises(ValidationError):
            HistogramData(values=[1.0], bins=bins)


def _histogram(doc: Document, **kwargs: object) -> ops.PlacedChart:
    return ops.add_chart(
        doc,
        kind="histogram",
        data=HistogramData(values=[1, 2, 2, 3, 3, 3, 4, 5, 5, 9], bins=4, **kwargs),  # type: ignore[arg-type]
        x=0,
        y=0,
        width=360,
        height=240,
        title="h",
    )


def test_a_histogram_draws_one_contiguous_bar_per_bin() -> None:
    doc = _doc()
    placed = _histogram(doc)
    rects = _series_rects(doc, placed.ref.id)
    assert len(rects) == 4
    for left, right in zip(rects, rects[1:], strict=False):
        assert abs((left[0] + left[2]) - right[0]) < 0.5  # they touch: x is continuous
    assert all(rect[3] > 0 for rect in rects)


def test_a_histogram_counts_its_own_observations() -> None:
    doc = _doc()
    placed = _histogram(doc, value_labels=True)
    assert sorted(_labels_at(doc, placed.ref.id)) == ["1", "2", "3", "4"]


def test_a_histogram_ticks_its_x_axis_in_round_numbers_and_not_in_edges() -> None:
    doc = _doc()
    placed = _histogram(doc)
    ticks = _texts(doc, placed.ref.id, "default-tick-label")
    assert "0" in ticks and "10" in ticks  # 1..9 of data, ticked 0/2/4/6/8/10
    assert "1" not in ticks or "9" not in ticks  # the bin edges are not what is labelled


def test_a_histogram_takes_the_hatch_and_the_axes_like_any_other_chart() -> None:
    doc = _doc()
    placed = ops.add_chart(
        doc,
        kind="histogram",
        data=HistogramData(values=[1, 2, 3, 4], bins=[0.0, 2.0, 4.0], hatch=True),
        x=0,
        y=0,
        axes=AxesSpec(gridlines="both", tick_marks=4),
    )
    assert "<pattern" in export_svg(doc)
    assert len(_series_rects(doc, placed.ref.id)) == 2
    assert _wearing(doc, placed.ref.id, "default-tick") != []


def test_a_histogram_is_not_a_sparkline_however_alike_the_payloads_look() -> None:
    doc = _doc()
    placed = ops.add_chart(doc, kind="histogram", data=HistogramData(values=[1.0, 2.0, 3.0]))
    spec = read_chart_spec(doc.resolve(placed.ref.id))
    assert spec is not None and isinstance(spec.data, HistogramData)


# --- 26. radar ----------------------------------------------------------------


def test_the_spokes_start_at_twelve_o_clock_and_turn_clockwise() -> None:
    quarters = [radar_angle(index, 4) for index in range(4)]
    assert quarters == pytest.approx([-math.pi / 2.0, 0.0, math.pi / 2.0, math.pi])
    # ...and whatever the count, the spokes are evenly spaced round the whole turn
    fifths = [radar_angle(index, 5) for index in range(5)]
    gaps = [b - a for a, b in zip(fifths, fifths[1:], strict=False)]
    assert gaps == pytest.approx([math.tau / 5.0] * 4)
    assert math.cos(fifths[0]) == pytest.approx(0.0, abs=1e-9) and math.sin(fifths[0]) < 0


def test_a_radar_maps_its_values_linearly_from_the_centre_to_the_rim() -> None:
    points = radar_points([0.0, 5.0, 10.0, 2.5], 10.0, (100.0, 100.0), 80.0, 4)
    reach = [math.dist((100.0, 100.0), point) for point in points]
    assert reach == pytest.approx([0.0, 40.0, 80.0, 20.0])  # 0, half, the rim, a quarter
    assert points[0] == pytest.approx((100.0, 100.0))
    assert points[2] == pytest.approx((100.0, 180.0))  # the third of four spokes points down


def test_pinning_the_rim_is_what_moves_the_same_number_up_the_spoke() -> None:
    at_data_max = radar_points([5.0], 5.0, (0.0, 0.0), 100.0, 4)
    pinned = radar_points([5.0], 10.0, (0.0, 0.0), 100.0, 4)
    assert math.dist((0.0, 0.0), at_data_max[0]) == pytest.approx(100.0)  # the data max IS the rim
    assert math.dist((0.0, 0.0), pinned[0]) == pytest.approx(50.0)  # half of a rim pinned at ten


def test_the_rings_are_round_steps_and_the_rim_is_always_one_of_them() -> None:
    assert radar_rings(73.0, 4) == pytest.approx([20.0, 40.0, 60.0, 73.0])
    # a rim that lands ON the ladder is not drawn twice, a hair inside itself
    assert radar_rings(100.0, 4) == pytest.approx([50.0, 100.0])
    # ...and the count is a TARGET the ladder rounds off, exactly as an axis's `ticks` is: ask
    # for more rings over the same span and the ladder drops to a step that can supply them
    assert radar_rings(100.0, 5) == pytest.approx([20.0, 40.0, 60.0, 80.0, 100.0])
    assert radar_rings(10.0, 5) == pytest.approx([2.0, 4.0, 6.0, 8.0, 10.0])


def test_a_spoke_label_hangs_off_the_end_of_itself_that_faces_the_wheel() -> None:
    assert radar_anchor(radar_angle(0, 4)) == "middle"  # 12 o'clock
    assert radar_anchor(radar_angle(1, 4)) == "start"  # 3 o'clock, reading outward
    assert radar_anchor(radar_angle(2, 4)) == "middle"  # 6 o'clock
    assert radar_anchor(radar_angle(3, 4)) == "end"  # 9 o'clock, reading back to the rim


def test_a_radar_needs_three_axes_before_it_is_a_shape_at_all() -> None:
    with pytest.raises(ValidationError):
        RadarData(axes=["a", "b"], series=[Series(name="s", values=[1.0, 2.0])])


def test_a_radar_series_carries_one_value_per_axis() -> None:
    with pytest.raises(ValidationError) as caught:
        RadarData(axes=["a", "b", "c"], series=[Series(name="s", values=[1.0, 2.0])])
    assert "one value per axis" in str(caught.value)


def test_a_negative_radius_is_refused_and_the_axis_it_is_on_is_named() -> None:
    with pytest.raises(ValidationError) as caught:
        RadarData(axes=["a", "b", "c"], series=[Series(name="s", values=[1.0, -2.0, 3.0])])
    message = str(caught.value)
    assert "'b'" in message and "cannot be negative" in message


def _radar(
    doc: Document, axes: list[str] | None = None, **kwargs: object
) -> ops.PlacedChart:
    names = axes if axes is not None else ["n", "e", "s", "w"]
    return ops.add_chart(
        doc,
        kind="radar",
        data=RadarData(
            axes=names,
            series=[
                Series(name="a", values=[10.0] * len(names)),
                Series(name="b", values=[2.0] * len(names)),
            ],
            **kwargs,  # type: ignore[arg-type]
        ),
        x=0,
        y=0,
        width=240,
        height=240,
    )


def _wheel(doc: Document, chart_id: str) -> tuple[tuple[float, float], float]:
    """The centre and the radius, read back off the spokes the chart drew."""
    spokes = _wearing(doc, chart_id, "default-axis")
    centre = (float(str(spokes[0].get("x1"))), float(str(spokes[0].get("y1"))))
    return centre, max(
        math.dist(centre, (float(str(spoke.get("x2"))), float(str(spoke.get("y2")))))
        for spoke in spokes
    )


def _polygons(doc: Document, chart_id: str, class_name: str) -> list[BaseElement]:
    return [
        node
        for node in _wearing(doc, chart_id, class_name)
        if str(node.tag).endswith("polygon")
    ]


def _corners(node: BaseElement) -> list[tuple[float, float]]:
    pairs = str(node.get("points") or "").split()
    return [(float(pair.split(",")[0]), float(pair.split(",")[1])) for pair in pairs]


def test_a_radar_draws_a_spoke_per_axis_and_an_n_gon_per_ring() -> None:
    doc = _doc()
    placed = _radar(doc, axes=["a", "b", "c", "d", "e"])
    assert len(_wearing(doc, placed.ref.id, "default-axis")) == 5
    rings = _polygons(doc, placed.ref.id, "default-gridline")
    assert len(rings) == 2  # 0..10 off the 1/2/5 ladder is 5, plus the rim
    centre, radius = _wheel(doc, placed.ref.id)
    for ring in rings:
        corners = _corners(ring)
        assert len(corners) == 5  # an n-gon through the ring, not a circle
        reach = [math.dist(centre, corner) for corner in corners]
        assert reach == pytest.approx([reach[0]] * 5, abs=0.01)
    assert max(math.dist(centre, corner) for corner in _corners(rings[-1])) == pytest.approx(
        radius, abs=0.01
    )


def _passes(doc: Document, chart_id: str) -> list[str]:
    """What each series group of a radar is, in the order the chart drew them."""
    order: list[str] = []
    for child in _children(doc, chart_id):
        if not str(child.get("class") or "").startswith("default-series"):
            continue
        polygons = [node for node in child if str(node.tag).endswith("polygon")]
        if not polygons:
            order.append("mark")
        elif "fill-opacity" in str(polygons[0].get("style")):
            order.append("wash")
        else:
            order.append("outline")
    return order


def test_every_wash_is_drawn_before_every_outline_and_the_marks_come_last() -> None:
    doc = _doc()
    placed = _radar(doc, marker="circle")
    assert _passes(doc, placed.ref.id) == [
        "wash",
        "wash",
        "outline",
        "outline",
        "mark",
        "mark",
    ]


def test_a_radar_that_asks_for_no_wash_draws_only_the_outlines() -> None:
    doc = _doc()
    placed = _radar(doc, fill=False)
    assert _passes(doc, placed.ref.id) == ["outline", "outline"]
    outlines = _polygons(doc, placed.ref.id, "default-series-1")
    assert len(outlines) == 0  # the GROUP wears the series class; the polygon is fill:none


def test_a_profile_is_a_closed_polygon_of_one_corner_per_axis() -> None:
    doc = _doc()
    placed = _radar(doc, axes=["a", "b", "c", "d", "e", "f"])
    outlines = [
        node
        for child in _children(doc, placed.ref.id)
        if str(child.get("class") or "").startswith("default-series")
        for node in child
        if str(node.tag).endswith("polygon") and "fill:none" in str(node.get("style"))
    ]
    assert len(outlines) == 2
    centre, radius = _wheel(doc, placed.ref.id)
    first = _corners(outlines[0])
    assert len(first) == 6
    assert [math.dist(centre, corner) for corner in first] == pytest.approx(
        [radius] * 6, abs=0.01
    )
    # the second series is a fifth of the first, so its polygon is a fifth of the way out
    assert [math.dist(centre, corner) for corner in _corners(outlines[1])] == pytest.approx(
        [radius / 5.0] * 6, abs=0.01
    )


@pytest.mark.parametrize(("shape", "tag"), [("square", "rect"), ("star", "path")])
def test_a_vertex_mark_is_the_glyph_it_was_asked_for(shape: str, tag: str) -> None:
    doc = _doc()
    placed = _radar(doc, marker=shape)
    assert _tags_under(doc, placed.ref.id).count(tag) == 8  # two series, four axes


def test_open_vertex_marks_are_hollow_and_wear_their_own_series() -> None:
    doc = _doc()
    placed = _radar(doc, marker="circle", open=True)
    marks = _circles(doc, placed.ref.id)
    assert len(marks) == 8
    for node in marks:
        assert "fill:none" in str(node.get("style"))
    assert {str(node.get("class")) for node in marks} == {
        "default-series-1",
        "default-series-2",
    }


def test_the_ruler_is_written_once_up_the_twelve_o_clock_spoke() -> None:
    doc = _doc()
    placed = _radar(doc)
    centre, radius = _wheel(doc, placed.ref.id)
    ticks = _wearing(doc, placed.ref.id, "default-tick-label")
    ruler = {
        str(node.text): float(str(node.get("y")))
        for node in ticks
        if float(str(node.get("x"))) < centre[0] and str(node.get("y")) is not None
        and abs(float(str(node.get("x"))) - centre[0]) < 5.0
    }
    assert sorted(ruler) == ["10", "5"]
    assert ruler["10"] == pytest.approx(centre[1] - radius, abs=0.01)  # the rim's own value
    assert ruler["5"] == pytest.approx(centre[1] - radius / 2.0, abs=0.01)  # half of ten
    assert all(str(node.get("text-anchor") or "") == "" for node in ticks)


def test_ring_labels_can_be_turned_off_and_the_frame_stays() -> None:
    doc = _doc()
    placed = _radar(doc, ring_labels=False)
    assert sorted(_texts(doc, placed.ref.id, "default-tick-label")) == ["e", "n", "s", "w"]
    assert len(_polygons(doc, placed.ref.id, "default-gridline")) == 2


def test_a_spoke_label_sits_outside_the_rim_anchored_by_its_octant() -> None:
    doc = _doc()
    placed = _radar(doc)
    centre, radius = _wheel(doc, placed.ref.id)
    placed_labels = {
        str(node.text): node for node in _wearing(doc, placed.ref.id, "default-tick-label")
    }
    for name in ("n", "e", "s", "w"):
        node = placed_labels[name]
        at = (float(str(node.get("x"))), float(str(node.get("y"))))
        assert math.dist(centre, at) > radius  # outside the rim it names
    assert "text-anchor:middle" in str(placed_labels["n"].get("style"))
    assert "text-anchor:middle" in str(placed_labels["s"].get("style"))
    assert "text-anchor:start" in str(placed_labels["e"].get("style"))
    assert "text-anchor:end" in str(placed_labels["w"].get("style"))


def test_the_widest_spoke_label_is_what_the_wheel_gives_up_room_for() -> None:
    doc = _doc()
    narrow = _radar(doc, axes=["n", "e", "s", "w"])
    wide = _radar(doc, axes=["n", "e", "s", "a very long axis name indeed"])
    assert _wheel(doc, wide.ref.id)[1] < _wheel(doc, narrow.ref.id)[1]


def test_pinning_the_rim_of_a_drawn_radar_shrinks_what_the_data_reaches() -> None:
    doc = _doc()
    loose = _radar(doc)
    pinned = _radar(doc, r_max=20.0)
    centre, radius = _wheel(doc, pinned.ref.id)
    outline = next(
        node
        for child in _children(doc, pinned.ref.id)
        if str(child.get("class") or "") == "default-series-1"
        for node in child
        if str(node.tag).endswith("polygon") and "fill:none" in str(node.get("style"))
    )
    assert [math.dist(centre, corner) for corner in _corners(outline)] == pytest.approx(
        [radius / 2.0] * 4, abs=0.01  # ten of a rim pinned at twenty
    )
    assert _wheel(doc, loose.ref.id)[1] == pytest.approx(radius)  # the same wheel, either way


def test_a_bar_and_a_radar_are_told_apart_by_what_they_name_their_columns() -> None:
    doc = _doc()
    bars = ops.add_chart(
        doc,
        kind="bar",
        data=BarData(categories=["a", "b"], series=[Series(name="s", values=[1.0, 2.0])]),
    )
    spec = read_chart_spec(doc.resolve(bars.ref.id))
    assert spec is not None and isinstance(spec.data, BarData)
    # ...and neither payload can be pushed through the other's kind in silence
    with pytest.raises(InvalidArgument):
        ops.add_chart(
            doc,
            kind="radar",
            data=BarData(
                categories=["a", "b", "c"], series=[Series(name="s", values=[1.0, 2.0, 3.0])]
            ),
        )
    with pytest.raises(InvalidArgument):
        ops.add_chart(
            doc,
            kind="bar",
            data=RadarData(
                axes=["a", "b", "c"], series=[Series(name="s", values=[1.0, 2.0, 3.0])]
            ),
        )


def test_a_radar_payload_stays_a_radar_through_the_fence() -> None:
    doc = _doc()
    placed = ops.add_chart(
        doc,
        kind="radar",
        data=RadarData(axes=["a", "b", "c"], series=[Series(name="s", values=[1.0, 2.0, 3.0])]),
    )
    spec = read_chart_spec(doc.resolve(placed.ref.id))
    assert spec is not None and isinstance(spec.data, RadarData)


def test_a_datum_callout_lands_on_the_vertex_it_names() -> None:
    doc = _doc()
    placed = ops.add_chart(
        doc,
        kind="radar",
        data=RadarData(
            axes=["a", "b", "c", "d"],
            series=[
                Series(name="one", values=[10.0, 4.0, 6.0, 2.0]),
                Series(name="two", values=[3.0, 9.0, 1.0, 7.0]),
            ],
        ),
        x=40,
        y=30,
        width=240,
        height=240,
    )
    outline = next(
        node
        for child in _children(doc, placed.ref.id)
        if str(child.get("class") or "") == "default-series-2"
        for node in child
        if str(node.tag).endswith("polygon") and "fill:none" in str(node.get("style"))
    )
    corner = _corners(outline)[1]  # series "two", axis "b"
    anchor = datum_anchor(doc, doc.resolve(placed.ref.id), ops.ChartDatum(series="two", index=1))
    assert anchor is not None
    assert anchor == pytest.approx((corner[0] + 40.0, corner[1] + 30.0), abs=1.0)
    assert datum_anchor(doc, doc.resolve(placed.ref.id), ops.ChartDatum(index=9)) is None


def test_get_params_hands_back_the_radar_a_chart_was_built_from() -> None:
    doc = _doc()
    data = RadarData(
        axes=["a", "b", "c"],
        series=[Series(name="s", values=[1.0, 2.0, 3.0])],
        fill=False,
        marker="diamond",
        open=True,
        rings=3,
        r_max=4.0,
        ring_labels=False,
        value_format=TickFormat(style="fixed", decimals=1),
    )
    placed = ops.add_chart(doc, kind="radar", data=data, x=0, y=0, title="R")
    params = get_params(doc, placed.ref.id)["params"]
    assert isinstance(params, dict)
    assert params["kind"] == "radar"
    assert params["data"] == data.model_dump()


def test_a_two_series_radar_renders_its_wash_inside_the_rim_and_nothing_outside() -> None:
    doc = _doc()
    placed = ops.add_chart(
        doc,
        kind="radar",
        data=RadarData(
            axes=["n", "e", "s", "w"],
            series=[
                Series(name="big", values=[10.0, 10.0, 10.0, 10.0]),
                Series(name="small", values=[2.0, 2.0, 2.0, 2.0]),
            ],
        ),
        x=0,
        y=0,
        width=240,
        height=240,
    )
    image = _render(doc)
    centre, radius = _wheel(doc, placed.ref.id)
    # A point at 45°, four fifths of the way out: inside the first series' diamond, outside the
    # second's, and off every ring and spoke — so the only paint there is series 1's own wash.
    step = radius * 0.4
    inside = image.getpixel((int(centre[0] + step), int(centre[1] - step)))  # type: ignore[attr-defined]
    assert isinstance(inside, tuple)
    # ...the channels within a rounding step of --series-1: a 0.15 wash is stored premultiplied,
    # so unpremultiplying it back out cannot land on the exact byte it went in as.
    assert inside[:3] == pytest.approx((0x44, 0x77, 0xAA), abs=3)
    assert 0 < inside[3] < 128  # ...washed in, not painted flat
    # ...and the corner beyond the rim is bare canvas, which a document has none of.
    corner = radius * 0.9
    outside = image.getpixel((int(centre[0] + corner), int(centre[1] - corner)))  # type: ignore[attr-defined]
    assert isinstance(outside, tuple)
    assert outside[3] == 0
