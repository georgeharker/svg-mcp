"""Chart facades: a data spec in, a laid-out, themed plot out.

A chart is a ``<g>`` that draws itself from the spec stored on it, exactly like a diagram node —
the difference is that the spec carries DATA, not just a label, and that the group is placed with
a ``translate`` so everything inside it is authored in a local ``0..w`` × ``0..h`` frame. That is
what makes :func:`edit_chart` honest: it throws the children away and re-derives them, and the
group keeps its id, its classes and its position because none of those live in the children.

The scales are deliberately small and pure — :func:`nice_ticks` and :func:`bar_bands` and
:func:`donut_angles` take numbers and return numbers, so the arithmetic that decides whether a
chart lies can be tested without a document in sight. Linear only: a log scale is a different
promise about what the picture means, and this module does not make it.

Paint comes from the theme. Every part carries the class its role asks for (``-axis``,
``-gridline``, ``-tick-label``, ``-series-N`` …) and no colour is written inline, so a variant
switch or an ``apply_theme`` moves a chart the same way it moves a diagram. The inline styles
here are structural — ``fill: none`` on a line, ``stroke: none`` on a mark that is a fill, the
area wash's opacity, text anchoring — with exactly one exception, called out where it happens: a
sparkline in a document whose theme offers no series colour strokes itself with the ink token,
because a sparkline is ONE element and an uninherited stroke would render it as nothing at all.
"""

from __future__ import annotations

import json
import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

import inkex
from inkex import BaseElement
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from ..model.document import Document
from ..model.errors import InvalidArgument
from ..model.handles import NodeRef, names_node
from ..theme.model import Category
from .construct import _CATEGORY_ATTR, _PRIM_ATTR, _place_and_style, _points_str
from .diagram import (
    _CHART_ATTR,
    _facade_body,
    _label_font,
    _num,
    _stack_below,
    _token,
    auto_origin,
    measure_label,
)
from .paint import resolve_paint_refs as _resolve_paint_refs
from .themes import ServingTheme, attach_dressing, resolve_dressing, serving_theme, worn_theme

Style = dict[str, str]
Point = tuple[float, float]

ChartKind = Literal["bar", "line", "donut", "scatter", "sparkline"]
"""The five plots this module draws. Closed — each one is a layout, not a configuration."""

_DEFAULT_W, _DEFAULT_H = 320.0, 200.0
_SPARK_W, _SPARK_H = 120.0, 32.0
_DEFAULT_GAP = 24.0

# Type sizes the margin arithmetic assumes. They MUST match the sizes the bundled default gives
# .tick-label / .axis-label / .chart-title, or the plot leaves the wrong amount of room; a theme
# that re-sizes them trades a little slack for its own look, which is a fair trade to allow.
_TICK_SIZE = 10.0
_AXIS_LABEL_SIZE = 11.0
_TITLE_SIZE = 13.0
# The clear air between a plot and the labels around it.
_GAP = 8.0

_MARKER_R = 2.5
_SCATTER_R = 3.0
_AREA_OPACITY = "0.15"
_DONUT_HOLE = 0.6
# What a sparkline strokes itself with when no theme offers it a series colour.
_DEFAULT_INK = "#1a1d21"
# How much of a category's band the bars fill; the rest is the gap that separates the bands.
_BAND_FILL = 0.7

# The 1/2/5 ladder every "nice" step is drawn from, plus the decade rollover.
_LADDER: tuple[float, ...] = (1.0, 2.0, 5.0, 10.0)
# Eight categorical series before the palette repeats — see the default theme's --series-N.
_SERIES_COUNT = 8

_EPS = 1e-9


# --- data schemas ------------------------------------------------------------


class Series(BaseModel):
    """One bar series: a name and one value per category."""

    model_config = ConfigDict(extra="forbid")

    name: str
    values: list[float]


class PointSeries(BaseModel):
    """One line/scatter series: a name and its (x, y) points, in the order they are drawn."""

    model_config = ConfigDict(extra="forbid")

    name: str
    points: list[tuple[float, float]]


class Slice(BaseModel):
    """One donut slice. A non-positive slice has no angle to occupy, so it is rejected."""

    model_config = ConfigDict(extra="forbid")

    label: str
    value: float = Field(gt=0)


class BarData(BaseModel):
    """Categorical bars. More than one series draws them side by side within each category."""

    model_config = ConfigDict(extra="forbid")

    categories: list[str] = Field(min_length=1)
    series: list[Series] = Field(min_length=1)

    @model_validator(mode="after")
    def _values_match_categories(self) -> BarData:
        for entry in self.series:
            if len(entry.values) != len(self.categories):
                raise ValueError(
                    f"series {entry.name!r} has {len(entry.values)} values but there are "
                    f"{len(self.categories)} categories — a bar chart needs one per category"
                )
        return self


class LineData(BaseModel):
    """Numeric-x lines, optionally with a marker at every point and a wash down to zero."""

    model_config = ConfigDict(extra="forbid")

    series: list[PointSeries] = Field(min_length=1)
    points: bool = False
    area: bool = False


class ScatterData(BaseModel):
    """Numeric-x points, one mark per datum and no line joining them."""

    model_config = ConfigDict(extra="forbid")

    series: list[PointSeries] = Field(min_length=1)


class DonutData(BaseModel):
    """Parts of a whole, as an annulus. The hole is ``--donut-hole`` × the outer radius."""

    model_config = ConfigDict(extra="forbid")

    slices: list[Slice] = Field(min_length=1)


class SparklineData(BaseModel):
    """A bare shape-of-the-trend line: no axes, no ticks, no title, height-normalized."""

    model_config = ConfigDict(extra="forbid")

    values: list[float] = Field(min_length=2)


ChartData = BarData | LineData | ScatterData | DonutData | SparklineData
"""What a chart is drawn FROM. Which member is valid is decided by the chart's ``kind``."""

ChartDataModel = (
    type[BarData] | type[LineData] | type[ScatterData] | type[DonutData] | type[SparklineData]
)

_MODELS: dict[ChartKind, ChartDataModel] = {
    "bar": BarData,
    "line": LineData,
    "scatter": ScatterData,
    "donut": DonutData,
    "sparkline": SparklineData,
}


def parse_chart_data(kind: ChartKind, data: ChartData) -> ChartData:
    """Re-validate ``data`` against the schema ``kind`` actually requires.

    Line and scatter payloads are structurally identical, so a caller (or a JSON decoder) can
    hand over the wrong one of the two in perfect good faith. Re-validating from the fields that
    were explicitly SET — defaults excluded — moves a bare series list between them silently and
    rejects a genuine mismatch (a scatter carrying ``area=True``) loudly.
    """
    model = _MODELS[kind]
    if isinstance(data, model):
        return data
    try:
        return model.model_validate(data.model_dump(exclude_defaults=True))
    except ValidationError as exc:
        raise InvalidArgument(
            f"this data does not fit a {kind} chart: {exc}"
        ) from exc


# --- scales ------------------------------------------------------------------


def nice_step(raw: float) -> float:
    """The smallest 1/2/5×10ᵏ value that is at least ``raw`` — the ladder every tick sits on."""
    if raw <= 0 or not math.isfinite(raw):
        return 1.0
    exponent = math.floor(math.log10(raw))
    base = 10.0**exponent
    fraction = raw / base
    for candidate in _LADDER:
        if fraction <= candidate + 1e-12:
            return candidate * base
    return 10.0 * base


def tick_decimals(step: float) -> int:
    """How many decimals a step needs to be written (and rounded) without float dust."""
    if step <= 0:
        return 0
    return max(0, -math.floor(math.log10(step) + 1e-12))


def nice_ticks(lo: float, hi: float, target: int = 5) -> list[float]:
    """Round tick values spanning ``[lo, hi]``, about ``target`` of them, on the 1/2/5 ladder.

    The returned range is always at least as wide as the data: it runs from the last round step
    at or below ``lo`` to the first at or above ``hi``, so nothing plots outside the axis. A
    degenerate span (a single repeated value) is padded rather than divided by zero — a flat
    series is a real answer and it deserves an axis around it, not an error.
    """
    if hi < lo:
        lo, hi = hi, lo
    if hi - lo <= _EPS:
        pad = max(1.0, abs(lo) * 0.1)
        lo, hi = lo - pad, hi + pad
    step = nice_step((hi - lo) / max(1, target))
    decimals = tick_decimals(step)
    start = math.floor(lo / step + 1e-9) * step
    count = max(1, int(math.ceil(hi / step - 1e-9) - math.floor(lo / step + 1e-9)))
    return [round(start + index * step, decimals) for index in range(count + 1)]


def include_zero(lo: float, hi: float) -> tuple[float, float]:
    """Widen a span to touch zero — what a bar's length and an area's wash are measured FROM."""
    return (min(lo, 0.0), max(hi, 0.0))


def scale(lo: float, hi: float, *, zero: bool = False) -> tuple[list[float], int]:
    """The ticks for a span and the precision to write them at, in one answer."""
    if zero:
        lo, hi = include_zero(lo, hi)
    ticks = nice_ticks(lo, hi)
    return ticks, tick_decimals(ticks[1] - ticks[0] if len(ticks) > 1 else 1.0)


def tick_text(value: float, decimals: int) -> str:
    """A tick's label: fixed to the step's precision, then stripped of the zeros that adds."""
    text = f"{value:.{decimals}f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in ("-0", "", "-") else text


@dataclass(frozen=True, slots=True)
class Band:
    """One bar's slot: where it starts along the category axis and how wide it is."""

    x: float
    w: float


def bar_bands(width: float, categories: int, series: int) -> list[list[Band]]:
    """Split a plot's width into one band per category, and each band into its series' bars.

    Bars fill ``_BAND_FILL`` of their band and the remaining gap is split evenly on both sides,
    so the white space between two categories is the same whether there is one series or six.
    Returns ``[category][series]``.
    """
    if categories <= 0 or series <= 0:
        return []
    band = width / categories
    usable = band * _BAND_FILL
    inset = (band - usable) / 2.0
    bar = usable / series
    return [
        [Band(x=index * band + inset + slot * bar, w=bar) for slot in range(series)]
        for index in range(categories)
    ]


def donut_angles(values: Sequence[float]) -> list[tuple[float, float]]:
    """Each slice's ``(start, end)`` angle in radians, starting at 12 o'clock and going clockwise.

    The angles tile the full turn exactly: the last slice ENDS on the start angle plus 2π rather
    than accumulating its own share, so rounding can never leave a hairline wedge of canvas.
    """
    total = sum(values)
    if total <= 0:
        raise InvalidArgument("a donut needs a positive total to divide into slices")
    start = -math.pi / 2.0
    angles: list[tuple[float, float]] = []
    running = 0.0
    for index, value in enumerate(values):
        first = start + math.tau * (running / total)
        running += value
        final = index == len(values) - 1
        angles.append((first, start + math.tau if final else start + math.tau * (running / total)))
    return angles


# --- plot geometry -----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Margins:
    """The room a plot leaves around itself for the labels that describe it."""

    left: float
    top: float
    right: float
    bottom: float


def plot_margins(
    y_tick_labels: Sequence[str],
    *,
    font: str,
    title: bool,
    x_label: bool,
    y_label: bool,
) -> Margins:
    """How much room the labels need — MEASURED, so a six-digit axis is not clipped or crowded.

    The left margin is the widest y tick label plus a gap, which is why a chart of thousands
    indents further than a chart of tens. Axis titles add their own line on the side they sit.
    """
    widest = max(
        (measure_label(text, font, _TICK_SIZE)[0] for text in y_tick_labels),
        default=0.0,
    )
    tick_height = measure_label("0", font, _TICK_SIZE)[1]
    axis_allowance = _AXIS_LABEL_SIZE + 4.0
    return Margins(
        left=widest + _GAP + (axis_allowance if y_label else 0.0),
        top=(_TITLE_SIZE + _GAP) if title else 4.0,
        right=_GAP,
        bottom=tick_height + _GAP + (axis_allowance if x_label else 0.0),
    )


@dataclass(frozen=True, slots=True)
class Plot:
    """The rectangle the data is drawn in, in the chart group's own local frame."""

    x: float
    y: float
    w: float
    h: float

    def map_x(self, value: float, lo: float, hi: float) -> float:
        span = hi - lo
        return self.x if abs(span) <= _EPS else self.x + (value - lo) / span * self.w

    def map_y(self, value: float, lo: float, hi: float) -> float:
        span = hi - lo
        if abs(span) <= _EPS:
            return self.y + self.h
        return self.y + self.h - (value - lo) / span * self.h


def _plot_rect(w: float, h: float, margins: Margins) -> Plot:
    """The plot rect, never allowed to invert however much the labels ask for."""
    return Plot(
        x=margins.left,
        y=margins.top,
        w=max(1.0, w - margins.left - margins.right),
        h=max(1.0, h - margins.top - margins.bottom),
    )


# --- the spec ----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ChartSpec:
    """What a chart is, as stored on its group. Position lives in the group's transform."""

    kind: ChartKind
    data: ChartData
    title: str
    x_label: str
    y_label: str
    w: float
    h: float
    auto: bool


@dataclass(frozen=True, slots=True)
class PlacedChart:
    """A new chart: its handle, where it landed, and the box it took."""

    ref: NodeRef
    x: float
    y: float
    w: float
    h: float


@dataclass(frozen=True, slots=True)
class ChartEdit:
    """A patched chart: its handle and how many children the rebuild produced."""

    ref: NodeRef
    children: int


def _kind_of(value: str) -> ChartKind | None:
    return value if value in _MODELS else None


def read_chart_spec(element: BaseElement) -> ChartSpec | None:
    """The chart spec stored on ``element``, or None if it is not a chart (or is corrupt)."""
    raw = element.get(_CHART_ATTR)
    if raw is None:
        return None
    try:
        spec = json.loads(raw)
        kind = _kind_of(str(spec["kind"]))
        if kind is None:
            return None
        return ChartSpec(
            kind=kind,
            data=_MODELS[kind].model_validate(spec["data"]),
            title=str(spec.get("title", "")),
            x_label=str(spec.get("x_label", "")),
            y_label=str(spec.get("y_label", "")),
            w=float(spec["w"]),
            h=float(spec["h"]),
            auto=bool(spec.get("auto", False)),
        )
    except (ValueError, TypeError, KeyError, ValidationError):
        return None


def _write_chart_spec(element: BaseElement, spec: ChartSpec) -> None:
    element.set(
        _CHART_ATTR,
        json.dumps(
            {
                "kind": spec.kind,
                "data": spec.data.model_dump(),
                "title": spec.title,
                "x_label": spec.x_label,
                "y_label": spec.y_label,
                "w": spec.w,
                "h": spec.h,
                "auto": spec.auto,
            },
            separators=(",", ":"),
        ),
    )


# --- part construction -------------------------------------------------------


def _hooks(doc: Document, theme: str | None, suffix: str) -> list[str]:
    """The one class a part wears, if the theme dressing this chart actually defines it."""
    if theme is None:
        return []
    meta = doc.theme_meta.get(theme)
    name = f"{theme}-{suffix}"
    return [name] if meta is not None and name in meta.class_names else []


def _part(
    doc: Document,
    element: BaseElement,
    *,
    prefix: str,
    category: Category,
    parent: str,
    style: Style | None,
    classes: Sequence[str],
) -> BaseElement:
    """Add one piece of a chart: unthemed by category, wearing exactly the classes it was given.

    Every part goes through here rather than through ``add_line``/``add_polyline``, because those
    default a black ``stroke`` onto anything linear — an inline colour, which would beat the
    series class the part is supposed to be painted by.
    """
    _place_and_style(
        doc,
        element,
        prefix=prefix,
        parent=parent,
        name=None,
        style=style,
        transform=None,
        category=category,
        styles=list(classes),
        themed=False,
    )
    return element


def _group(doc: Document, parent: str) -> BaseElement:
    """A bare structural group inside the chart — not themable, so it stamps no category."""
    group = inkex.Group()
    _place_and_style(
        doc, group, prefix="chart-part", parent=parent, name=None, style=None, transform=None
    )
    return group


def _text(
    doc: Document,
    parent: str,
    content: str,
    at: Point,
    *,
    anchor: str,
    baseline: str,
    classes: Sequence[str],
    rotate: float | None = None,
) -> None:
    if not content:
        return
    element = inkex.TextElement()
    element.set("x", _num(at[0]))
    element.set("y", _num(at[1]))
    element.text = content
    style: Style = {"text-anchor": anchor, "dominant-baseline": baseline}
    _part(
        doc, element, prefix="text", category="text", parent=parent, style=style, classes=classes
    )
    if rotate is not None:
        element.set("transform", f"rotate({_num(rotate)} {_num(at[0])} {_num(at[1])})")


def _line(doc: Document, parent: str, a: Point, b: Point, classes: Sequence[str]) -> None:
    _part(
        doc,
        inkex.Line.new(a, b),
        prefix="line",
        category="connector",
        parent=parent,
        style=None,
        classes=classes,
    )


def _polyline(doc: Document, parent: str, points: list[Point], classes: Sequence[str]) -> None:
    _part(
        doc,
        inkex.Polyline.new(_points_str(points)),
        prefix="polyline",
        category="connector",
        parent=parent,
        style={"fill": "none"},
        classes=classes,
    )


# --- frame -------------------------------------------------------------------


def _series_class(doc: Document, theme: str | None, index: int) -> list[str]:
    return _hooks(doc, theme, f"series-{index % _SERIES_COUNT + 1}")


def _draw_frame(
    doc: Document,
    parent: str,
    theme: str | None,
    *,
    plot: Plot,
    spec: ChartSpec,
    y_ticks: Sequence[float],
    y_decimals: int,
    x_ticks: Sequence[tuple[float, str]],
    font: str,
) -> None:
    """The scenery: gridlines, the two axes, the tick labels, and the axis titles.

    Gridlines are drawn FIRST so the data is never hidden behind them, and the axes after, so
    the baseline reads as a line rather than as the last gridline.
    """
    frame = _group(doc, parent)
    frame_id = str(frame.get_id())
    lo, hi = y_ticks[0], y_ticks[-1]
    for tick in y_ticks:
        at = plot.map_y(tick, lo, hi)
        _line(
            doc,
            frame_id,
            (plot.x, at),
            (plot.x + plot.w, at),
            _hooks(doc, theme, "gridline"),
        )
    axis = _hooks(doc, theme, "axis")
    _line(doc, frame_id, (plot.x, plot.y), (plot.x, plot.y + plot.h), axis)
    _line(
        doc, frame_id, (plot.x, plot.y + plot.h), (plot.x + plot.w, plot.y + plot.h), axis
    )

    tick_class = _hooks(doc, theme, "tick-label")
    for tick in y_ticks:
        _text(
            doc,
            frame_id,
            tick_text(tick, y_decimals),
            (plot.x - 4.0, plot.map_y(tick, lo, hi)),
            anchor="end",
            baseline="central",
            classes=tick_class,
        )
    tick_height = measure_label("0", font, _TICK_SIZE)[1]
    for at, text in x_ticks:
        _text(
            doc,
            frame_id,
            text,
            (at, plot.y + plot.h + tick_height * 0.85),
            anchor="middle",
            baseline="alphabetic",
            classes=tick_class,
        )

    label_class = _hooks(doc, theme, "axis-label")
    _text(
        doc,
        frame_id,
        spec.x_label,
        (plot.x + plot.w / 2.0, spec.h - 2.0),
        anchor="middle",
        baseline="alphabetic",
        classes=label_class,
    )
    _text(
        doc,
        frame_id,
        spec.y_label,
        (_AXIS_LABEL_SIZE, plot.y + plot.h / 2.0),
        anchor="middle",
        baseline="alphabetic",
        classes=label_class,
        rotate=-90.0,
    )


def _draw_title(doc: Document, parent: str, theme: str | None, plot: Plot, title: str) -> None:
    _text(
        doc,
        parent,
        title,
        (plot.x + plot.w / 2.0, _TITLE_SIZE),
        anchor="middle",
        baseline="alphabetic",
        classes=_hooks(doc, theme, "chart-title"),
    )


# --- the five plots ----------------------------------------------------------


def _y_span(values: Sequence[float], *, zero: bool) -> tuple[float, float]:
    lo, hi = (min(values), max(values)) if values else (0.0, 1.0)
    return include_zero(lo, hi) if zero else (lo, hi)


def _draw_bar(doc: Document, group: str, theme: str | None, spec: ChartSpec, font: str) -> Plot:
    data = spec.data
    if not isinstance(data, BarData):
        raise InvalidArgument("a bar chart needs bar data")
    flat = [value for entry in data.series for value in entry.values]
    lo, hi = _y_span(flat, zero=True)
    ticks, decimals = scale(lo, hi)
    margins = plot_margins(
        [tick_text(tick, decimals) for tick in ticks],
        font=font,
        title=bool(spec.title),
        x_label=bool(spec.x_label),
        y_label=bool(spec.y_label),
    )
    plot = _plot_rect(spec.w, spec.h, margins)
    bands = bar_bands(plot.w, len(data.categories), len(data.series))
    x_ticks = [
        (plot.x + (bands[index][0].x + bands[index][-1].x + bands[index][-1].w) / 2.0, name)
        for index, name in enumerate(data.categories)
    ]
    _draw_frame(
        doc,
        group,
        theme,
        plot=plot,
        spec=spec,
        y_ticks=ticks,
        y_decimals=decimals,
        x_ticks=x_ticks,
        font=font,
    )

    top, bottom = ticks[-1], ticks[0]
    base = plot.map_y(0.0, bottom, top)
    for index, entry in enumerate(data.series):
        series = _group(doc, group)
        series_id = str(series.get_id())
        for class_name in _series_class(doc, theme, index):
            series.set("class", class_name)
        for category, value in enumerate(entry.values):
            band = bands[category][index]
            at = plot.map_y(value, bottom, top)
            _part(
                doc,
                inkex.Rectangle.new(plot.x + band.x, min(at, base), band.w, abs(base - at)),
                prefix="rect",
                category="shape",
                parent=series_id,
                # A mark that is a FILL takes no stroke: the series class sets both, and the
                # stroke would make every bar a hairline wider than the number it stands for.
                style={"stroke": "none"},
                classes=(),
            )
    return plot


def _point_span(series: Sequence[PointSeries]) -> tuple[tuple[float, float], tuple[float, float]]:
    xs = [point[0] for entry in series for point in entry.points]
    ys = [point[1] for entry in series for point in entry.points]
    if not xs:
        raise InvalidArgument("this chart's series carry no points to plot")
    return (min(xs), max(xs)), (min(ys), max(ys))


def _draw_points(
    doc: Document, group: str, theme: str | None, spec: ChartSpec, font: str
) -> Plot:
    """Line and scatter — the same axes, differing only in what each series draws."""
    data = spec.data
    if not isinstance(data, LineData | ScatterData):
        raise InvalidArgument("a line or scatter chart needs point-series data")
    area = isinstance(data, LineData) and data.area
    markers = isinstance(data, ScatterData) or data.points
    (x_lo, x_hi), (y_lo, y_hi) = _point_span(data.series)
    y_ticks, y_decimals = scale(y_lo, y_hi, zero=area)
    x_ticks_at, x_decimals = scale(x_lo, x_hi)
    margins = plot_margins(
        [tick_text(tick, y_decimals) for tick in y_ticks],
        font=font,
        title=bool(spec.title),
        x_label=bool(spec.x_label),
        y_label=bool(spec.y_label),
    )
    plot = _plot_rect(spec.w, spec.h, margins)
    x_left, x_right = x_ticks_at[0], x_ticks_at[-1]
    y_bottom, y_top = y_ticks[0], y_ticks[-1]
    _draw_frame(
        doc,
        group,
        theme,
        plot=plot,
        spec=spec,
        y_ticks=y_ticks,
        y_decimals=y_decimals,
        x_ticks=[
            (plot.map_x(tick, x_left, x_right), tick_text(tick, x_decimals))
            for tick in x_ticks_at
        ],
        font=font,
    )

    for index, entry in enumerate(data.series):
        series = _group(doc, group)
        series_id = str(series.get_id())
        for class_name in _series_class(doc, theme, index):
            series.set("class", class_name)
        placed = [
            (plot.map_x(px, x_left, x_right), plot.map_y(py, y_bottom, y_top))
            for px, py in entry.points
        ]
        if area and placed:
            base = plot.map_y(0.0, y_bottom, y_top)
            steps = " ".join(f"L {_num(px)} {_num(py)}" for px, py in placed)
            _part(
                doc,
                inkex.PathElement.new(
                    f"M {_num(placed[0][0])} {_num(base)} {steps} "
                    f"L {_num(placed[-1][0])} {_num(base)} Z"
                ),
                prefix="path",
                category="shape",
                parent=series_id,
                style={"stroke": "none", "fill-opacity": _AREA_OPACITY},
                classes=(),
            )
        if isinstance(data, LineData) and len(placed) > 1:
            _polyline(doc, series_id, placed, ())
        if markers:
            radius = _SCATTER_R if isinstance(data, ScatterData) else _MARKER_R
            for px, py in placed:
                _part(
                    doc,
                    inkex.Circle.new((px, py), radius),
                    prefix="circle",
                    category="shape",
                    parent=series_id,
                    style={"stroke": "none"},  # a dot is a fill (see the bars)
                    classes=(),
                )
    return plot


def _polar(cx: float, cy: float, radius: float, angle: float) -> str:
    return f"{_num(cx + radius * math.cos(angle))} {_num(cy + radius * math.sin(angle))}"


def annular_sector(
    cx: float, cy: float, outer: float, inner: float, start: float, end: float
) -> str:
    """One donut wedge: out along the start angle, round, in, and back round the hole.

    A lone slice covers the whole turn, where a single elliptical arc is degenerate (its two
    endpoints coincide), so that case is drawn as two half-turns instead of being fudged into
    an almost-closed ring with a visible seam.
    """
    if end - start >= math.tau - 1e-9:
        middle = start + math.pi
        return (
            f"M {_polar(cx, cy, outer, start)} "
            f"A {_num(outer)} {_num(outer)} 0 0 1 {_polar(cx, cy, outer, middle)} "
            f"A {_num(outer)} {_num(outer)} 0 0 1 {_polar(cx, cy, outer, end)} "
            f"L {_polar(cx, cy, inner, end)} "
            f"A {_num(inner)} {_num(inner)} 0 0 0 {_polar(cx, cy, inner, middle)} "
            f"A {_num(inner)} {_num(inner)} 0 0 0 {_polar(cx, cy, inner, start)} Z"
        )
    large = 1 if end - start > math.pi else 0
    return (
        f"M {_polar(cx, cy, outer, start)} "
        f"A {_num(outer)} {_num(outer)} 0 {large} 1 {_polar(cx, cy, outer, end)} "
        f"L {_polar(cx, cy, inner, end)} "
        f"A {_num(inner)} {_num(inner)} 0 {large} 0 {_polar(cx, cy, inner, start)} Z"
    )


def _draw_donut(
    doc: Document,
    group: str,
    theme: str | None,
    spec: ChartSpec,
    hole: float,
) -> None:
    data = spec.data
    if not isinstance(data, DonutData):
        raise InvalidArgument("a donut chart needs slice data")
    top = (_TITLE_SIZE + _GAP) if spec.title else 4.0
    plot = Plot(x=0.0, y=top, w=spec.w, h=max(1.0, spec.h - top - 4.0))
    outer = min(plot.w, plot.h) / 2.0
    inner = outer * hole
    cx, cy = plot.x + plot.w / 2.0, plot.y + plot.h / 2.0
    for index, (piece, (start, end)) in enumerate(
        zip(data.slices, donut_angles([s.value for s in data.slices]), strict=True)
    ):
        wedge = _group(doc, group)
        wedge_id = str(wedge.get_id())
        for class_name in _series_class(doc, theme, index):
            wedge.set("class", class_name)
        wedge.set("data-slice", piece.label)
        _part(
            doc,
            inkex.PathElement.new(annular_sector(cx, cy, outer, inner, start, end)),
            prefix="path",
            category="shape",
            parent=wedge_id,
            style={"stroke": "none"},
            classes=(),
        )
    _draw_title(doc, group, theme, plot, spec.title)


def _draw_sparkline(
    doc: Document, group: str, theme: str | None, spec: ChartSpec, ink: str
) -> None:
    data = spec.data
    if not isinstance(data, SparklineData):
        raise InvalidArgument("a sparkline needs a plain list of values")
    lo, hi = min(data.values), max(data.values)
    inset = 1.0
    span = hi - lo
    height = max(1.0, spec.h - 2 * inset)
    step = spec.w / max(1, len(data.values) - 1)
    points = [
        (
            index * step,
            spec.h / 2.0 if abs(span) <= _EPS else inset + height - (value - lo) / span * height,
        )
        for index, value in enumerate(data.values)
    ]
    classes = _series_class(doc, theme, 0)
    # A sparkline is one line: if the theme offers no series colour there is nothing above it to
    # inherit a stroke from, so it takes the ink rather than rendering as nothing at all.
    style: Style = {"fill": "none"} if classes else {"fill": "none", "stroke": ink}
    _part(
        doc,
        inkex.Polyline.new(_points_str(points)),
        prefix="polyline",
        category="connector",
        parent=group,
        style=style,
        classes=classes,
    )


# --- the facade --------------------------------------------------------------


def _hole(theme: ServingTheme) -> float:
    value = _token(theme, "--donut-hole", _DONUT_HOLE)
    return value if 0.0 <= value < 1.0 else _DONUT_HOLE


def _build(doc: Document, group: BaseElement, spec: ChartSpec) -> None:
    """Draw (or re-draw) every child of a chart group from its spec. Idempotent by construction.

    The theme the marks are painted in is read off the classes the GROUP wears, not off a scan of
    the routing table: the group already carries the hook it was dressed with, and a scan can
    name a different theme once a second one is resident. A group wearing no chart hook was built
    ``themed=False``, and a rebuild that dressed it anyway would overturn that decision.
    """
    for child in list(group):
        if isinstance(child.tag, str):
            child.delete()
    theme = serving_theme(doc, "chart")
    font = _label_font(theme)
    # Both hooks a chart can be wearing, most specific first: a theme is free to define only
    # `.chart--bar` (a look for one plot and nothing for the rest), and reading just the bare
    # `.chart` would call such a chart undressed and rebuild every axis and series unstyled.
    dressing = worn_theme(doc, group, f"chart--{spec.kind}", "chart")
    group_id = str(group.get_id())
    if spec.kind == "sparkline":
        # A sparkline is the line and nothing else, by definition — no frame, no title.
        _draw_sparkline(doc, group_id, dressing, spec, theme.tokens.get("--ink", _DEFAULT_INK))
        return
    if spec.kind == "donut":
        # The donut lays out its own title, sized around the ring rather than above a plot.
        _draw_donut(doc, group_id, dressing, spec, _hole(theme))
        return
    plot = (
        _draw_bar(doc, group_id, dressing, spec, font)
        if spec.kind == "bar"
        else _draw_points(doc, group_id, dressing, spec, font)
    )
    _draw_title(doc, group_id, dressing, plot, spec.title)


@names_node
def _place_chart(
    doc: Document,
    group: BaseElement,
    *,
    kind: ChartKind,
    x: float,
    y: float,
    parent: str | None,
    name: str | None,
    style: Style | None,
    styles: list[str] | None,
    themed: bool,
) -> NodeRef:
    """Attach the chart group: parent, id, name, the what-it-is stamp, its hooks, its position.

    The group is placed with a ``translate``, so every child is authored in a local frame and a
    rebuild never has to know where the chart sits.

    As in ``_place_facade``, everything that can fail is resolved BEFORE the group joins the tree
    — a style name nothing defines used to leave an empty chart group behind.
    """
    parent_element = doc.resolve_parent(parent)
    dressing = resolve_dressing(doc, category="chart", prim=kind, styles=styles, themed=themed)
    resolved = _resolve_paint_refs(doc, style)

    parent_element.add(group)
    group.set_id(doc.new_id("chart"))
    if name is not None:
        group.label = name
    group.set(_CATEGORY_ATTR, "chart")
    group.set(_PRIM_ATTR, kind)
    group.set("transform", f"translate({_num(x)},{_num(y)})")
    attach_dressing(doc, group, dressing)
    if resolved:
        group.style = inkex.Style(resolved)
    return NodeRef(id=str(group.get_id()), tag=str(group.TAG), name=name)


def add_chart(
    doc: Document,
    *,
    kind: ChartKind,
    data: ChartData,
    x: float | None = None,
    y: float | None = None,
    width: float | None = None,
    height: float | None = None,
    title: str | None = None,
    x_label: str | None = None,
    y_label: str | None = None,
    parent: str | None = None,
    name: str | None = None,
    style: Style | None = None,
    styles: list[str] | None = None,
    themed: bool = True,
) -> PlacedChart:
    """Add a chart — axes, scales, marks and labels derived from the data, as one themed group.

    The DATA is the whole call: ticks are chosen from it on the 1/2/5 ladder, the margins are
    measured from the tick labels those produce, and every mark is painted by the theme's
    ``--series-N`` palette in series order. Omit x/y to stack the chart under the last chart or
    diagram node in the same parent.

    A ``sparkline`` ignores ``title``/``x_label``/``y_label`` and a ``donut`` ignores the two axis
    labels — they have no axes for them to name, and drawing them anyway would be a lie about
    what the picture shows.
    """
    parsed = parse_chart_data(kind, data)
    spark = kind == "sparkline"
    auto = width is None and height is None
    w = width if width is not None else (_SPARK_W if spark else _DEFAULT_W)
    h = height if height is not None else (_SPARK_H if spark else _DEFAULT_H)
    if w <= 0 or h <= 0:
        raise InvalidArgument("a chart needs a positive width and height")

    gap = _token(serving_theme(doc, "chart"), "--gap-node", _DEFAULT_GAP)
    parent_element = doc.resolve_parent(parent)
    stacked = _stack_below(parent_element, gap)
    # The group's translate is read in the PARENT's frame; the stack was measured in world, so
    # it crosses over. An x/y the caller gave is parent-local already (the ``add_rect`` rule).
    auto_x, auto_y = auto_origin(parent_element, stacked)
    at_x = x if x is not None else auto_x
    at_y = y if y is not None else auto_y

    group = inkex.Group()
    ref = _place_chart(
        doc,
        group,
        kind=kind,
        x=at_x,
        y=at_y,
        parent=parent,
        name=name,
        style=style,
        styles=styles,
        themed=themed,
    )
    spec = ChartSpec(
        kind=kind,
        data=parsed,
        title=title or "",
        x_label=x_label or "",
        y_label=y_label or "",
        w=w,
        h=h,
        auto=auto,
    )
    with _facade_body(doc, ref):
        _write_chart_spec(group, spec)
        _build(doc, group, spec)
    return PlacedChart(ref=ref, x=at_x, y=at_y, w=w, h=h)


def edit_chart(
    doc: Document,
    target: str,
    *,
    data: ChartData | None = None,
    title: str | None = None,
    x_label: str | None = None,
    y_label: str | None = None,
    width: float | None = None,
    height: float | None = None,
) -> ChartEdit:
    """Edit a chart by its SPEC — new data, new labels, a new box — and re-derive the picture.

    The children are thrown away and rebuilt rather than patched: a chart's geometry is a pure
    function of its data and its box, deriving it costs nothing, and nothing outside a chart may
    reference its internals (they carry no stable identity — the spec does). The GROUP survives
    untouched, so its id, its classes and its position all mean what they meant before.

    ``data`` is re-validated against the chart's own kind: an edit cannot turn a bar into a donut.
    """
    group = doc.resolve(target)
    current = read_chart_spec(group)
    if current is None:
        raise InvalidArgument(f"{target!r} is not a chart (no {_CHART_ATTR} spec)")
    if (width is not None and width <= 0) or (height is not None and height <= 0):
        raise InvalidArgument("a chart needs a positive width and height")
    spec = ChartSpec(
        kind=current.kind,
        data=parse_chart_data(current.kind, data) if data is not None else current.data,
        title=title if title is not None else current.title,
        x_label=x_label if x_label is not None else current.x_label,
        y_label=y_label if y_label is not None else current.y_label,
        w=width if width is not None else current.w,
        h=height if height is not None else current.h,
        auto=current.auto and width is None and height is None,
    )
    _write_chart_spec(group, spec)
    _build(doc, group, spec)
    return ChartEdit(
        ref=NodeRef(id=str(group.get_id()), tag=str(group.TAG), name=getattr(group, "label", None)),
        children=sum(1 for child in group if isinstance(child.tag, str)),
    )
