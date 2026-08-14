"""Chart facades: a data spec in, a laid-out, themed plot out.

A chart is a ``<g>`` that draws itself from the spec stored on it, exactly like a diagram node —
the difference is that the spec carries DATA, not just a label, and that the group is placed with
a ``translate`` so everything inside it is authored in a local ``0..w`` × ``0..h`` frame. That is
what makes :func:`edit_chart` honest: it throws the children away and re-derives them, and the
group keeps its id, its classes and its position because none of those live in the children.

The scales are deliberately small and pure — :func:`nice_ticks`, :func:`log_ticks`,
:func:`format_tick`, :func:`resolve_axis`, :func:`bar_bands` and :func:`donut_angles` take
numbers and return numbers, so the arithmetic that decides whether a chart lies can be tested
without a document in sight. An :class:`AxesSpec` on the chart's own spec is what turns those
knobs — pinned limits, a tick count or an explicit tick list, a closed formatting vocabulary, a
log scale — and its ABSENCE is a contract: a chart built without one draws exactly what this
module drew before axes existed, down to the byte.

Paint comes from the theme. Every part carries the class its role asks for (``-axis``,
``-gridline``, ``-tick-label``, ``-series-N`` …) and no colour is written inline, so a variant
switch or an ``apply_theme`` moves a chart the same way it moves a diagram. The inline styles
here are structural — ``fill: none`` on a line, ``stroke: none`` on a mark that is a fill, the
area wash's opacity, text anchoring — with exactly one exception, called out where it happens: a
sparkline in a document whose theme offers no series colour strokes itself with the ink token,
because a sparkline is ONE element and an uninherited stroke would render it as nothing at all.
A hatched mark is not a second exception: the ``fill: url(#…)`` it carries names a pattern, and
the line INSIDE that pattern wears the series class, so the colour still comes from the theme.
"""

from __future__ import annotations

import json
import math
from bisect import bisect_right
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

import inkex
from inkex import BaseElement
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from ..model.document import Document
from ..model.errors import InvalidArgument
from ..model.handles import NodeRef, names_node
from ..query.outline import _to_world_point
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
from .resources import define_clip, define_pattern
from .themes import ServingTheme, attach_dressing, resolve_dressing, serving_theme, worn_theme

Style = dict[str, str]
Point = tuple[float, float]

ChartKind = Literal["bar", "line", "donut", "scatter", "histogram", "sparkline", "radar"]
"""The seven plots this module draws. Closed — each one is a layout, not a configuration."""

_DEFAULT_W, _DEFAULT_H = 320.0, 200.0
_SPARK_W, _SPARK_H = 120.0, 32.0
_DEFAULT_GAP = 24.0

# Type sizes the margin arithmetic assumes when the serving theme names none. The theme's own
# --chart-*-size tokens override every one of them (see :func:`chart_sizes`), which is what keeps
# the size the margins are MEASURED at and the size the CSS RENDERS at from drifting apart; these
# are the no-theme fallbacks, and they match what the bundled default sets.
_TICK_SIZE = 10.0
_AXIS_LABEL_SIZE = 11.0
_TITLE_SIZE = 13.0
_VALUE_LABEL_SIZE = 10.0
_REFERENCE_LABEL_SIZE = 10.0
_DONUT_CENTER_SIZE = 20.0
_DONUT_SUBTEXT_SIZE = 10.0
# The clear air between a plot and the labels around it.
_GAP = 8.0

_MARKER_R = 2.5
_SCATTER_R = 3.0
_SPARK_DOT_R = 2.0
_AREA_OPACITY = "0.15"
_DONUT_HOLE = 0.6
# The smallest ring slice labels are allowed to squeeze a donut down to.
_MIN_DONUT_R = 20.0
# A radar's ring count when nobody names one, and the smallest wheel its labels may squeeze it to.
_RADAR_RINGS = 4
_MIN_RADAR_R = 20.0
# How far a ring's value sits to the left of the 12 o'clock spoke it is written on.
_RING_LABEL_PAD = 3.0
# The hatch tile: a 6-unit square of one stroke, turned 45° by the pattern's own transform.
_HATCH_TILE = 6.0
_HATCH_WIDTH = "2"
# What a sparkline strokes itself with when no theme offers it a series colour.
_DEFAULT_INK = "#1a1d21"
# How much of a category's band the bars fill; the rest is the gap that separates the bands.
_BAND_FILL = 0.7

# How far a reference's label sits off the line it names — a nudge so the two do not touch.
_REF_LABEL_PAD = 2.0
# The radial stub a thin donut slice's label hangs off, and the sweep below which it earns one.
_SLICE_LEADER = 6.0
_THIN_SLICE = math.radians(18.0)

# The 1/2/5 ladder every "nice" step is drawn from, plus the decade rollover.
_LADDER: tuple[float, ...] = (1.0, 2.0, 5.0, 10.0)
# The same ladder inside one decade, which is what a log axis subdivides by.
_LOG_LADDER: tuple[float, ...] = (1.0, 2.0, 5.0)
# Eight categorical series before the palette repeats — see the default theme's --series-N.
_SERIES_COUNT = 8
# How many ticks an axis aims for when the caller names no number.
_TICK_TARGET = 5
# A log axis subdivides its decades only while it spans few enough of them to have room.
_LOG_SUBDIVIDE_DECADES = 2
# The precision a "plain" label is written at before its trailing zeros are stripped.
_PLAIN_DECIMALS = 6

_EPS = 1e-9
# The floor a log map clamps to, so a stray non-positive value is a squashed mark and not a crash.
_TINY = 1e-300

_CLIP_OWNER = "data-chart-owner"
"""Stamped on every ``<defs>`` resource a chart builds, so a rebuild can collect its own litter.

A chart's children are thrown away and re-derived on every edit, but a clipPath or a hatch pattern
lives in ``<defs>`` where that sweep cannot reach it. Owning them by the chart's id is what stops
ten edits leaving ten generations of orphaned resources behind.
"""


# --- data schemas ------------------------------------------------------------


class TickFormat(BaseModel):
    """How a tick's number is written. A CLOSED vocabulary, deliberately.

    An arbitrary format string is a footgun on an axis: it lets a caller write ``{:.0f}`` over
    data that needed two decimals and produce an axis of five identical labels. These five styles
    cover what a tick label is actually for, and each one knows what its own default precision is.

    It is declared here rather than with the axes because the DATA reaches for it too: a value
    label written on a mark is the same number as the tick beside it, and one formatter is what
    keeps the two from disagreeing.
    """

    model_config = ConfigDict(extra="forbid", use_attribute_docstrings=True)

    style: Literal["plain", "percent", "currency", "si", "fixed"] = "plain"
    decimals: int | None = Field(default=None, ge=0, le=10)
    """Precision. ``fixed`` is the style that exists for it (default 0) and ``si`` defaults to 1;
    ``plain``, ``currency`` and ``percent`` default to "as many as the number needs, trailing
    zeros stripped", and take a number here as the precision to round to."""
    prefix: str = ""
    """Written before the number. ``currency`` takes this as its symbol and falls back to ``$``."""
    suffix: str = ""
    """Written after the number — after ``%`` and after an SI unit, which are part of the number."""
    thousands: bool = False
    """Group the integer part: ``1204`` → ``1,204``."""


Marker = Literal[
    "circle", "square", "diamond", "triangle", "tri_down", "plus", "cross", "star", "none"
]
"""The mark a line or scatter puts at a point. Closed, and deliberately modest.

Eight shapes plus ``none`` — not the forty a plotting library offers, because past about eight a
reader is decoding a catalogue rather than reading a chart. ``plus`` and ``cross`` are the two
with no area to fill: they are drawn as STROKED paths wearing the series class, which is exactly
why every ``--series-N`` rule in a theme has to set a stroke as well as a fill.
"""

_STROKED_MARKERS: frozenset[str] = frozenset({"plus", "cross"})
"""The marks that are line, not area. They take the series' STROKE and no fill at all."""

_OPEN_STROKE = "1.5"
"""What an unfilled mark is drawn with. Structural: it is what makes the ring read as a ring."""

# A five-pointed star's inner radius, as a fraction of its outer one — the pentagram ratio, which
# is the only one that puts the inner corners on the lines the outer points already make.
_STAR_INNER = 0.382


class Series(BaseModel):
    """One bar series: a name and one value per category."""

    model_config = ConfigDict(extra="forbid", use_attribute_docstrings=True)

    name: str
    values: list[float]


class PointSeries(BaseModel):
    """One line/scatter series: a name and its (x, y) points, in the order they are drawn."""

    model_config = ConfigDict(extra="forbid", use_attribute_docstrings=True)

    name: str
    points: list[tuple[float, float]]
    sizes: list[float] | None = None
    """A size per point — the bubble channel, one number per mark and never a different count.

    What the numbers MEAN depends on the data model's ``marker_scale``. With one, these are raw
    quantities: they are mapped across the whole chart's observed range so that a mark's AREA
    carries the value (the radii go as the square root, which is the only mapping in which twice
    the number looks like twice the quantity). Without one they are radii, in user units, already
    decided by the caller.
    """

    @model_validator(mode="after")
    def _sizes_match_points(self) -> PointSeries:
        if self.sizes is None:
            return self
        if len(self.sizes) != len(self.points):
            raise ValueError(
                f"series {self.name!r} has {len(self.sizes)} sizes but {len(self.points)} "
                "points — a bubble needs one size per point"
            )
        for size in self.sizes:
            if not math.isfinite(size) or size < 0:
                raise ValueError(
                    f"series {self.name!r} carries a size of {size!r}; a mark cannot be smaller "
                    "than nothing"
                )
        return self


class Slice(BaseModel):
    """One donut slice. A non-positive slice has no angle to occupy, so it is rejected."""

    model_config = ConfigDict(extra="forbid", use_attribute_docstrings=True)

    label: str
    value: float = Field(gt=0)


CategoryOrder = Literal["given", "value_desc", "value_asc", "label"] | list[str]
"""How the categories are sorted before anything is laid out. A list is an explicit order.

``value_desc``/``value_asc`` rank by the FIRST series — the one the eye reads as the subject —
except on a stack, where the only honest ranking is the whole stack's total.
"""

Orientation = Literal["vertical", "horizontal"]
"""Which way a bar grows. ``horizontal`` swaps the two axes, category names into the left margin."""


class BarData(BaseModel):
    """Categorical bars. More than one series draws them side by side within each category."""

    model_config = ConfigDict(extra="forbid", use_attribute_docstrings=True)

    categories: list[str] = Field(min_length=1)
    series: list[Series] = Field(min_length=1)
    hatch: bool = False
    """Fill each series with diagonal hatching in its own colour, so print and greyscale keep
    the series apart when the hues collapse into one another."""
    orientation: Orientation = "vertical"
    """``horizontal`` runs the value axis along x and the categories down the left margin, which
    is what makes long category names readable — the margin is measured from them."""
    stacked: bool = False
    """Sum the series within each category instead of setting them side by side. Positives stack
    up from zero and negatives stack DOWN from zero, each from its own running total, so a series
    that crosses zero never draws over the one below it. The value axis is scaled to the TOTALS."""
    value_labels: bool = False
    """Write each bar's value at its end (in a stack: in the centre of each segment)."""
    stack_total_labels: bool = False
    """Write each stack's total just beyond the end of the stack. Ignored unless ``stacked``."""
    value_format: TickFormat | None = None
    """How the value labels are written. Omit and they follow the axis's own ``tick_format``."""
    order: CategoryOrder = "given"
    """The order the categories are drawn in — see :data:`CategoryOrder`."""
    waterfall: bool = False
    """Float each bar from the running total of the ones before it — how a number got from where
    it started to where it ended. Positives climb, negatives descend, and a dashed connector runs
    from each bar's end to the next bar's start so the eye can follow the ledger. ONE series only
    (a waterfall is a single account being walked), and never stacked.

    The running total follows the DRAWN order, so an ``order`` that moves the categories moves the
    arithmetic with them — which is usually not what a ledger wants."""
    total_label: str | None = None
    """Append a final bar running from zero to the net total, captioned with this text. It wears
    the SECOND series colour, because it is a different kind of statement from the steps that
    produced it. Ignored unless ``waterfall``."""
    normalized: bool = False
    """Scale every category's stack to 100, so the categories compare by SHARE rather than by
    size. Requires ``stacked``. Each value becomes its share of the category's total magnitude
    (the sum of the absolute values, so a stack that crosses zero still normalizes to a span of
    100). It pairs naturally with ``tick_format={"style": "percent"}`` — but the values really are
    0..100 rather than 0..1, so the format to write them with is ``fixed``/``plain`` plus a ``%``
    suffix. Nothing is set for you: a chart that silently reformatted its own axis would be
    deciding what the numbers mean."""

    @model_validator(mode="after")
    def _values_match_categories(self) -> BarData:
        for entry in self.series:
            if len(entry.values) != len(self.categories):
                raise ValueError(
                    f"series {entry.name!r} has {len(entry.values)} values but there are "
                    f"{len(self.categories)} categories — a bar chart needs one per category"
                )
        if self.waterfall:
            if len(self.series) != 1:
                raise ValueError(
                    f"a waterfall walks ONE running total, but this chart has "
                    f"{len(self.series)} series; drop the rest or drop `waterfall`"
                )
            if self.stacked:
                raise ValueError(
                    "a waterfall is already a stack laid end to end; `stacked` on top of it has "
                    "nothing left to sum"
                )
        if self.normalized and not self.stacked:
            raise ValueError(
                "`normalized` scales each STACK to 100, so it needs `stacked` to have a stack "
                "to scale"
            )
        return self


class SeriesBand(BaseModel):
    """A filled region between two of the chart's own series — a range, not a reading.

    ``between`` names them: a confidence interval's two edges, a min and a max, this year and
    last. Both names have to belong to series ON THIS CHART, and in v1 the two series must share
    an identical x sequence — a band between two differently-sampled lines is an interpolation,
    and an interpolation a caller did not ask for is a lie about what was measured.
    """

    model_config = ConfigDict(extra="forbid", use_attribute_docstrings=True)

    between: tuple[str, str]
    label: str | None = None
    """Written inside the band, where it is at its widest. Omit for an unlabelled region."""


class LineData(BaseModel):
    """Numeric-x lines, optionally with a marker at every point and a wash down to zero."""

    model_config = ConfigDict(extra="forbid", use_attribute_docstrings=True)

    series: list[PointSeries] = Field(min_length=1)
    points: bool = False
    area: bool = False
    marker: Marker = "circle"
    """What ``points`` draws at each datum. ``none`` suppresses the marks however it is set."""
    marker_size: float | None = Field(default=None, gt=0)
    """Half the mark's extent, in px — the radius of a circle. Defaults to 2.5 on a line."""
    marker_scale: tuple[float, float] | None = None
    """``(min_r, max_r)`` — what a series' ``sizes`` are mapped ONTO, making the marks a bubble
    channel. The mapping is by area (radii go as the square root) across the range the whole
    chart's sizes actually span, so two series' bubbles are comparable. Leave it out and a
    ``sizes`` list is taken as radii in user units, already decided."""
    open: bool = False
    """Draw the marks unfilled: the series' stroke, a hollow middle. The second encoding a chart
    needs to survive greyscale — shape and fill together tell two series apart where one hue
    against another does not. ``plus``/``cross`` are stroked whatever this says."""
    markevery: int | None = Field(default=None, ge=1)
    """Put a mark on every nth point, counting from the first. What keeps a 400-point series
    readable: the LINE stays complete, only the marks thin out. None (or 1) marks every point."""
    hatch: bool = False
    """Hatch the area wash instead of washing it flat. Ignored when ``area`` is false."""
    step: Literal["none", "pre", "post", "mid"] = "none"
    """Draw the series as a staircase rather than a slope — what a value that HOLDS between
    readings actually looks like. ``post`` holds the old value and rises at the next x; ``pre``
    rises first and then holds; ``mid`` changes halfway between the two, which is what to draw
    when the reading is a period's value and neither end of it is the moment it changed. The area
    wash follows the same outline."""
    bands: list[SeriesBand] | None = None
    """Filled regions between named pairs of these series — see :class:`SeriesBand`. Drawn BEHIND
    every line, in the first named series' colour at a light fill-opacity, so the band reads as
    that series' range rather than as a third series."""
    value_labels: bool = False
    """Write each point's y value above its mark."""
    value_format: TickFormat | None = None
    """How those labels are written. Omit and they follow the axis's own ``tick_format``."""

    @model_validator(mode="after")
    def _bands_name_series_that_line_up(self) -> LineData:
        _validate_bands(self.bands, self.series)
        return self


class ScatterData(BaseModel):
    """Numeric-x points, one mark per datum and no line joining them."""

    model_config = ConfigDict(extra="forbid", use_attribute_docstrings=True)

    series: list[PointSeries] = Field(min_length=1)
    marker: Marker = "circle"
    """The mark each datum is drawn as. ``none`` leaves a scatter with nothing to show."""
    marker_size: float | None = Field(default=None, gt=0)
    """Half the mark's extent, in px. Defaults to 3 on a scatter — a mark is all it has."""
    marker_scale: tuple[float, float] | None = None
    """``(min_r, max_r)`` for a series' ``sizes`` — see :attr:`LineData.marker_scale`. This is
    what turns a scatter into a bubble chart."""
    open: bool = False
    """Draw the marks unfilled — see :attr:`LineData.open`. On a crowded scatter it is also what
    lets overlapping marks stay countable."""
    value_labels: bool = False
    """Write each point's y value above its mark."""
    value_format: TickFormat | None = None
    """How those labels are written. Omit and they follow the axis's own ``tick_format``."""


def _validate_bands(bands: Sequence[SeriesBand] | None, series: Sequence[PointSeries]) -> None:
    """Both ends of every band must be a series here, and the two must be sampled alike. Pure."""
    if not bands:
        return
    by_name = {entry.name: entry for entry in series}
    for band in bands:
        for name in band.between:
            if name not in by_name:
                have = ", ".join(repr(entry.name) for entry in series)
                raise ValueError(
                    f"a band names {name!r}, which is not a series on this chart; it has {have}"
                )
        low, high = (by_name[name] for name in band.between)
        xs_low = [point[0] for point in low.points]
        xs_high = [point[0] for point in high.points]
        if xs_low != xs_high:
            raise ValueError(
                f"a band between {band.between[0]!r} and {band.between[1]!r} needs the two "
                "series sampled at the SAME x values; filling between two differently-sampled "
                "lines would interpolate readings nobody took"
            )


class DonutData(BaseModel):
    """Parts of a whole, as an annulus. The hole is ``--donut-hole`` × the outer radius."""

    model_config = ConfigDict(extra="forbid", use_attribute_docstrings=True)

    slices: list[Slice] = Field(min_length=1)
    hatch: bool = False
    """Hatch each wedge in its own colour — the same print/greyscale insurance the bars take."""
    slice_labels: bool = False
    """Label each slice outside the ring with its name and its value. A slice too thin to be
    labelled at the rim gets a short radial stub to hang its label off."""
    value_format: TickFormat | None = None
    """How a slice's value is written. Omit for the plain number."""
    center_text: str = ""
    """The KPI in the hole — the one number the ring is there to qualify."""
    center_subtext: str = ""
    """The caption under it."""
    start_angle: float = -90.0
    """Degrees from 3 o'clock, clockwise, where the first slice begins. -90 is 12 o'clock."""
    order: CategoryOrder = "given"
    """The order the slices are drawn in; a list names them by label. See :data:`CategoryOrder`."""


class HistogramData(BaseModel):
    """Raw observations, binned and counted — the one chart here that computes its own y.

    That is a deliberate crossing of the "no statistical transforms" fence, and a narrow one:
    binning is ARITHMETIC (which half-open interval does this number fall in, how many landed
    there) with one answer, where a density estimate or a kernel has parameters that change the
    shape of the picture. Density, cumulative counts and KDE stay out.
    """

    model_config = ConfigDict(extra="forbid", use_attribute_docstrings=True)

    values: list[float] = Field(min_length=1)
    """The observations themselves — not counts. The chart does the counting."""
    bins: int | list[float] = 10
    """A COUNT of equal-width bins across the data's range, or the exact bin EDGES (ascending,
    at least two — n edges make n-1 bins). A degenerate range (every value the same) becomes one
    bin the width the axes use for a flat series: ``max(1, |v| × 0.1)``, centred on the value."""
    hatch: bool = False
    """Hatch the bars, for the same print/greyscale reason a bar chart hatches."""
    value_labels: bool = False
    """Write each bin's count above its bar."""
    value_format: TickFormat | None = None
    """How those counts are written. Omit and they follow the axis's own ``tick_format``."""

    @model_validator(mode="after")
    def _bins_are_a_count_or_an_ascending_ladder(self) -> HistogramData:
        if isinstance(self.bins, int):
            if self.bins < 1:
                raise ValueError(f"a histogram needs at least one bin; {self.bins} is not a count")
            return self
        if len(self.bins) < 2:
            raise ValueError(
                "explicit bins are the EDGES, so there have to be at least two of them to make "
                "one bin"
            )
        if any(b <= a for a, b in zip(self.bins, self.bins[1:], strict=False)):
            raise ValueError(f"bin edges have to ascend; {self.bins!r} does not")
        return self


class SparklineData(BaseModel):
    """A bare shape-of-the-trend line: no axes, no ticks, no title, height-normalized."""

    model_config = ConfigDict(extra="forbid", use_attribute_docstrings=True)

    values: list[float] = Field(min_length=2)
    last_point: bool = False
    """Put a dot on the final value — where it ended up, which is what a sparkline is read for."""
    extremes: bool = False
    """Put a dot on the lowest and the highest value."""
    baseline: float | None = None
    """Draw a reference line across the line at this value — a target, a budget, last year."""


class RadarData(BaseModel):
    """A profile per series around a wheel of named axes — a SHAPE, compared to other shapes.

    The polar frame is what the radar buys and what it costs. It buys a closed outline the eye
    reads as one thing, which is why a radar answers "how are these two profiles different"
    better than six bar charts side by side. It costs the zero-crossing story: a radius runs from
    the centre outward and there is no other direction for it to run, so a NEGATIVE value has
    nowhere to be drawn and is refused rather than folded, mirrored or clamped into a lie. Data
    that crosses zero wants a bar chart, where zero is a line and both sides of it exist.

    Every axis is drawn at the SAME radial scale, so the spokes have to be commensurable — the
    reader is being invited to compare across them by eye. Five scores out of ten radar honestly;
    a revenue, a headcount and a latency on one wheel do not, whatever the picture suggests.
    """

    model_config = ConfigDict(extra="forbid", use_attribute_docstrings=True)

    axes: list[str] = Field(min_length=3)
    """The spokes, in clock order from 12 o'clock. Three is the floor: two spokes are a line, and
    a "polygon" of two vertices is a picture of nothing."""
    series: list[Series] = Field(min_length=1)
    """One value per axis per series — the same :class:`Series` a bar chart carries, because a
    profile IS a row of values against named columns."""
    fill: bool = True
    """Wash each polygon in its own colour under all the outlines, so a profile reads as an area
    rather than as a loop of line. Structural opacity, so two overlapping profiles stay legible."""
    marker: Marker = "none"
    """A mark at each vertex — the full vocabulary. ``none`` by default: the vertices are already
    the polygon's corners, and a mark on each is only worth its ink when the spokes are crowded
    or two profiles run close enough to need telling apart at a reading rather than by hue."""
    open: bool = False
    """Draw those marks hollow — see :attr:`LineData.open`."""
    rings: int | None = Field(default=None, ge=1)
    """How many concentric rings to aim for, as a TARGET the 1/2/5 ladder rounds off (exactly
    like an axis's ``ticks``), not an exact count. None asks for about four."""
    r_max: float | None = Field(default=None, gt=0)
    """Pin the rim's value, so two radars can be laid side by side and compared. None takes the
    largest value on the chart. A reading BEYOND a pinned rim is drawn beyond the rim rather than
    clipped to it: on a cartesian plot the axis line is a boundary a reader knows means "off the
    scale", but a radar's rim is its outermost gridline, so a clipped polygon would read as a
    datum sitting exactly at the maximum — a silent lie where an overflow is an obvious one."""
    ring_labels: bool = True
    """Write each ring's value on the 12 o'clock spoke — the one place a radial ruler can be read
    without a second set of numbers cluttering every other spoke."""
    value_format: TickFormat | None = None
    """How those ring values are written. Omit for the plain number."""

    @model_validator(mode="after")
    def _one_value_per_axis_and_never_a_negative_radius(self) -> RadarData:
        for entry in self.series:
            if len(entry.values) != len(self.axes):
                raise ValueError(
                    f"series {entry.name!r} has {len(entry.values)} values but there are "
                    f"{len(self.axes)} axes — a radar needs one value per axis"
                )
            for position, value in enumerate(entry.values):
                if value < 0:
                    raise ValueError(
                        f"series {entry.name!r} carries {value!r} on axis "
                        f"{self.axes[position]!r}; a radius cannot be negative, and a radar has "
                        "no zero line to cross — plot data that goes below zero as bars"
                    )
        return self


ChartData = (
    BarData | LineData | ScatterData | DonutData | HistogramData | SparklineData | RadarData
)
"""What a chart is drawn FROM. Which member is valid is decided by the chart's ``kind``."""

ChartDataModel = (
    type[BarData]
    | type[LineData]
    | type[ScatterData]
    | type[DonutData]
    | type[HistogramData]
    | type[SparklineData]
    | type[RadarData]
)

_MODELS: dict[ChartKind, ChartDataModel] = {
    "bar": BarData,
    "line": LineData,
    "scatter": ScatterData,
    "donut": DonutData,
    "histogram": HistogramData,
    "sparkline": SparklineData,
    "radar": RadarData,
}


def parse_chart_data(kind: ChartKind, data: ChartData) -> ChartData:
    """Re-validate ``data`` against the schema ``kind`` actually requires.

    Line and scatter payloads are structurally identical, so a caller (or a JSON decoder) can
    hand over the wrong one of the two in perfect good faith. Re-validating from the fields that
    were explicitly SET — defaults excluded — moves a bare series list between them silently and
    rejects a genuine mismatch (a scatter carrying ``area=True``) loudly.

    A bar and a radar share their ``series`` and nothing else, and that is exactly what keeps
    them apart in BOTH directions: a bar names its columns with ``categories`` and a radar names
    its spokes with ``axes``, so a bar payload offered as radar data has no ``axes`` (and carries
    a ``categories`` this model forbids), and a radar payload offered as bar data has no
    ``categories`` (and carries an ``axes``). Neither can be mistaken for the other in silence.
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


# --- the axes model ----------------------------------------------------------


Gridlines = Literal["x", "y", "both", "none"]
"""Which sets of gridlines a frame draws. Read in SCREEN terms — ``y`` is the horizontal set."""


class ReferenceLine(BaseModel):
    """A threshold, a target, a tolerance band — furniture the data is READ AGAINST.

    It is axis-level rather than per-kind because that is what it is: a statement about where a
    value falls on the scale, which every plot drawn against that scale can carry.
    """

    model_config = ConfigDict(extra="forbid", use_attribute_docstrings=True)

    value: float
    axis: Literal["y", "x"] = "y"
    """Which DATA axis the value is on — ``y`` is the value axis however the bars are turned,
    ``x`` the numeric x of a line or scatter. An ``x`` reference on a categorical x is dropped."""
    label: str = ""
    """Written at the far end of the line, right-aligned to it."""
    to: float | None = None
    """Set it and the reference is a BAND from ``value`` to here, not a line."""
    kind: str = "reference"
    """The role class the part wears: ``{theme}-{kind}``. Name your own to paint one differently."""


class AxesSpec(BaseModel):
    """Everything a caller can say about the axes, as one optional argument.

    Absent (the default) means today's behaviour EXACTLY: limits from the data, five-ish ticks on
    the 1/2/5 ladder, plain labels, horizontal gridlines, no tick marks. Every field below is a
    departure from that, and each one is honoured on its own — pinning ``y_max`` leaves ``y_min``
    derived from the data, naming ``ticks`` leaves the limits alone.
    """

    model_config = ConfigDict(extra="forbid", use_attribute_docstrings=True)

    y_min: float | None = None
    """Pin the bottom of the value axis — what makes two charts comparable side by side."""
    y_max: float | None = None
    """Pin the top of the value axis. Data outside a pinned range is CLIPPED to the plot rect."""
    x_min: float | None = None
    """Pin the left of a NUMERIC x axis (line/scatter). A bar's x is categorical; ignored there."""
    x_max: float | None = None
    """Pin the right of a numeric x axis."""
    scale: Literal["linear", "log"] = "linear"
    """The value (y) axis's scale. ``log`` needs strictly positive data AND limits, and draws
    bars from the axis minimum rather than from zero — there is no zero on a log axis."""
    ticks: int | list[float] | None = None
    """A target COUNT of value-axis ticks, or the exact values to tick at (those outside the
    range are dropped rather than refused). Omit for the standard five-ish."""
    x_ticks: int | list[float] | None = None
    """The same for a numeric x axis."""
    tick_format: TickFormat | None = None
    """How the value axis's labels are written."""
    x_tick_format: TickFormat | None = None
    """How a numeric x axis's labels are written. A bar's category names are never reformatted."""
    gridlines: Gridlines = "y"
    """Which sets of gridlines to draw. ``y`` (horizontal, at the value ticks) is the default."""
    minor: int | None = Field(default=None, ge=1)
    """How many minor ticks to put BETWEEN each pair of major ticks — ``4`` cuts every major
    interval into five. They are positions, not decoration: nothing is drawn for them unless
    ``tick_marks`` (minor marks, at half the major length) or ``minor_gridlines`` asks for it.

    On a LOG scale the count is ignored and the minor positions are the classic 2..9 mantissa
    points inside each decade — the only subdivision that is evenly spaced in what the axis
    actually measures, and the one that makes a log axis readable at a glance."""
    minor_gridlines: bool = False
    """Draw gridlines at the minor positions too, fainter than the major ones. Follows the same
    ``gridlines`` selection: a chart drawing only horizontal gridlines gets only horizontal
    minor ones."""
    tick_marks: float | None = None
    """Length in px of a tick mark outside the plot rect. ``0``/absent draws no marks at all."""
    tick_direction: Literal["out", "in", "inout"] = "out"
    """Which side of the axis the tick marks stand on. ``in`` puts them inside the plot rect,
    ``inout`` straddles it with ``tick_marks`` px each way. The MARGINS only ever reserve room
    for the outward part — an inward tick costs the labels nothing, because it is drawn in space
    the plot already owns."""
    x_tick_rotate: float = 0.0
    """Degrees to turn the BOTTOM axis's tick labels by, about their own anchor. Negative reads
    bottom-left to top-right, which is the usual way to fit long category names — and on
    horizontal bars the bottom axis is the value axis, so that is what turns."""
    y_tick_rotate: float = 0.0
    """Degrees to turn the LEFT axis's tick labels by, about their own anchor. The left margin
    grows to the turned label's real horizontal extent, exactly as the bottom margin does for
    ``x_tick_rotate`` — so turning long category names on horizontal bars narrows the indent
    instead of clipping them."""
    invert_x: bool = False
    """Run the numeric x axis right-to-left. Ignored where x is categorical (a bar's categories
    are ordered by ``order``, which says what it means; a flipped scale would not)."""
    invert_y: bool = False
    """Run the value axis top-to-bottom — depth below a surface, a golf score, a rank where 1 is
    best. It lives inside the SCALE, so everything measured through it turns with it: bars still
    grow from their baseline, and the baseline is now at the top of the plot."""
    zero_spine: bool = False
    """Draw the category axis LINE at value 0 rather than along the bottom of the plot, when zero
    is inside the range. The tick labels and the tick marks STAY at the plot's edge.

    That last half is a deliberate divergence from what matplotlib does with a spine moved to
    zero, which drags the labels into the middle of the data with it. A reader scans a chart's
    labels down one fixed edge; moving them to wherever zero happens to fall means re-finding the
    ruler on every chart, and means the labels collide with the marks they are supposed to
    measure. Here the SPINE moves — that is the statement, "these bars hang from zero" — and the
    reading edge does not."""
    reference_lines: list[ReferenceLine] = Field(default_factory=list)
    """Thresholds and bands drawn across the plot. A line sits ABOVE the data (a threshold has to
    read over what it judges) and a band BEHIND it. A line whose value is off the axis is dropped
    silently; a band is clamped to the axis instead. See :class:`ReferenceLine`."""


_DEFAULT_AXES = AxesSpec()
"""What every chart is laid out against when the caller named no axes — the compatibility path."""


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


def tick_text(value: float, decimals: int) -> str:
    """A tick's label: fixed to the step's precision, then stripped of the zeros that adds."""
    text = f"{value:.{decimals}f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in ("-0", "", "-") else text


# --- log scale ---------------------------------------------------------------


def log_ticks(lo: float, hi: float, target: int = _TICK_TARGET) -> list[float]:
    """Decade ticks spanning ``[lo, hi]``, subdivided 1/2/5 while the span is short enough.

    Under about two decades there is room for the 1/2/5 rungs inside each decade, and without them
    a chart of 3..80 gets two ticks and no sense of where anything sits. Past that the decades are
    the only readable ticking, and ``target`` thins them further (every second, every third …) so
    a span of eight decades does not draw nine labels down a 200px axis.

    Both ends must be strictly positive: a log axis has no zero to reach and no negative half to
    reach into, so a non-positive limit is an error naming the value rather than a silent nudge.
    """
    if hi < lo:
        lo, hi = hi, lo
    for value in (lo, hi):
        if not math.isfinite(value) or value <= 0:
            raise InvalidArgument(
                f"a log scale needs strictly positive limits; {value!r} is not one"
            )
    if hi / lo < 1.0 + 1e-9:  # a flat series: give it a decade either side to sit in
        lo, hi = lo / 10.0, hi * 10.0
    lo_exp = math.floor(math.log10(lo) + 1e-9)
    hi_exp = math.ceil(math.log10(hi) - 1e-9)
    if hi_exp - lo_exp <= _LOG_SUBDIVIDE_DECADES:
        rungs = [
            _round_decade(mantissa, exponent)
            for exponent in range(lo_exp, hi_exp + 1)
            for mantissa in _LOG_LADDER
        ]
        first = max((rung for rung in rungs if rung <= lo * (1.0 + 1e-9)), default=rungs[0])
        last = min((rung for rung in rungs if rung >= hi * (1.0 - 1e-9)), default=rungs[-1])
        return [rung for rung in rungs if first <= rung <= last]
    step = max(1, math.ceil((hi_exp - lo_exp) / max(1, target - 1)))
    # Round the top out to a whole number of steps, so the last gap is as wide as all the others.
    hi_exp = lo_exp + step * math.ceil((hi_exp - lo_exp) / step)
    return [_round_decade(1.0, exponent) for exponent in range(lo_exp, hi_exp + 1, step)]


def _round_decade(mantissa: float, exponent: int) -> float:
    """``mantissa × 10ᵉ``, rounded to the precision that exponent needs — no float dust."""
    return round(mantissa * 10.0**exponent, max(0, -exponent + 1))


# The mantissas a log decade is subdivided at. Not a count anybody chooses: they are where the
# numbers between one decade and the next actually are.
_LOG_MANTISSAS: tuple[float, ...] = (2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0)


def minor_ticks(majors: Sequence[float], count: int, *, log: bool = False) -> list[float]:
    """Where the minor ticks fall between the majors, in data units. Pure, and majors excluded.

    Linear: ``count`` of them in every gap, evenly — ``count=4`` cuts each major interval into
    five. Log: the count is ignored and the answer is the 2..9 mantissa points of every decade the
    majors span, which is the only subdivision that is even in what a log axis measures.

    Either way the positions stay INSIDE the majors' own span: minor ticks past the last major
    would be a subdivision of an interval the axis never drew.
    """
    if len(majors) < 2 or count < 1:
        return []
    lo, hi = min(majors), max(majors)
    if log:
        if lo <= 0:
            return []
        found = [
            _round_decade(mantissa, exponent)
            for exponent in range(
                math.floor(math.log10(lo) + 1e-9), math.floor(math.log10(hi) + 1e-9) + 1
            )
            for mantissa in _LOG_MANTISSAS
        ]
    else:
        ordered = sorted(majors)
        found = [
            low + (high - low) * (index + 1) / (count + 1)
            for low, high in zip(ordered, ordered[1:], strict=False)
            for index in range(count)
        ]
    tolerance = max(abs(lo), abs(hi), 1.0) * 1e-9
    kept = [
        value
        for value in found
        if lo - tolerance <= value <= hi + tolerance
        and not any(abs(value - major) <= tolerance for major in majors)
    ]
    return sorted(kept)


# --- tick labels -------------------------------------------------------------

_SI_STEPS: tuple[tuple[float, str], ...] = (
    (1e12, "T"),
    (1e9, "G"),
    (1e6, "M"),
    (1e3, "k"),
    (1.0, ""),
    (1e-3, "m"),
    (1e-6, "µ"),
    (1e-9, "n"),
)


def _trim(text: str) -> str:
    """Drop the zeros a fixed-point render adds, and the minus sign a negative zero keeps."""
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    if text.startswith("-") and not any(digit in text for digit in "123456789"):
        text = text[1:]
    return text or "0"


def _group_thousands(text: str) -> str:
    """``-1204.5`` → ``-1,204.5``. Grouping is a property of the integer part alone."""
    sign, body = ("-", text[1:]) if text.startswith("-") else ("", text)
    whole, dot, fraction = body.partition(".")
    if not whole.isdigit():
        return text
    return f"{sign}{int(whole):,}{dot}{fraction}"


def _si_parts(value: float, decimals: int) -> tuple[str, str]:
    """An SI-scaled number and its unit prefix. Zero has no magnitude, so it takes no prefix."""
    if value == 0.0 or not math.isfinite(value):
        return _trim(f"{value:.{decimals}f}"), ""
    magnitude = abs(value)
    for factor, unit in _SI_STEPS:
        if magnitude >= factor * (1.0 - 1e-12):
            return _trim(f"{value / factor:.{decimals}f}"), unit
    factor, unit = _SI_STEPS[-1]
    return _trim(f"{value / factor:.{decimals}f}"), unit


def format_tick(value: float, fmt: TickFormat | None = None) -> str:
    """Write one tick's number the way ``fmt`` asks. Pure, and the only formatter a chart uses.

    With no ``fmt`` a value is written plainly: enough decimals to be itself, none of the zeros
    that adds. The five styles then differ in what they do to the NUMBER — ``percent`` scales it
    by 100 and owns the ``%``, ``si`` scales it by a decade and owns the k/M/G/T (m/µ/n below 1),
    ``fixed`` pins the decimals, ``currency`` and ``plain`` leave it alone — and the caller's own
    ``prefix``/``suffix`` wrap whatever came out. Only ``fixed`` and ``percent`` keep trailing
    zeros: they were asked for a precision, where the rest were asked for a number.
    """
    if fmt is None:
        return _trim(f"{value:.{_PLAIN_DECIMALS}f}")
    unit = ""
    if fmt.style == "percent":
        text = f"{value * 100.0:.{_PLAIN_DECIMALS if fmt.decimals is None else fmt.decimals}f}"
        text = _trim(text) if fmt.decimals is None else text
        unit = "%"
    elif fmt.style == "si":
        text, unit = _si_parts(value, 1 if fmt.decimals is None else fmt.decimals)
    elif fmt.style == "fixed":
        text = f"{value:.{0 if fmt.decimals is None else fmt.decimals}f}"
    else:  # plain and currency: the number as it is, to the precision it needs
        text = _trim(f"{value:.{_PLAIN_DECIMALS if fmt.decimals is None else fmt.decimals}f}")
    if fmt.thousands:
        text = _group_thousands(text)
    if text.startswith("-") and not any(digit in text for digit in "123456789"):
        text = text[1:]  # a rounded-to-zero negative is zero
    prefix = (fmt.prefix or "$") if fmt.style == "currency" else fmt.prefix
    # Money is written -$5, never $-5: the sign belongs to the amount, outside its symbol.
    sign, text = ("-", text[1:]) if fmt.style == "currency" and text.startswith("-") else ("", text)
    return f"{sign}{prefix}{text}{unit}{fmt.suffix}"


# --- the resolved axis -------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Scale:
    """The span one direction maps through, and whether it does it logarithmically.

    Deliberately unclamped: :meth:`unit` will happily answer 1.4 for a datum above a pinned top,
    because the plot's job is to CLIP that mark to the rect, not to quietly move it inside.

    ``invert`` is here rather than in the drawing for the same reason: turning the axis round is
    one flip in ONE function, and every bar, wash, reference line and clip that ever asked this
    scale where a number goes turns with it, with nothing to remember and nothing to miss.
    """

    lo: float
    hi: float
    log: bool = False
    invert: bool = False

    def unit(self, value: float) -> float:
        """``value`` as a fraction of the span — 0 at ``lo``, 1 at ``hi``, outside for outside.

        Inverted, it is the same fraction counted from the other end: 1 at ``lo``, 0 at ``hi``.
        """
        if self.log:
            low = math.log10(max(self.lo, _TINY))
            high = math.log10(max(self.hi, _TINY))
            at = math.log10(max(value, _TINY))
        else:
            low, high, at = self.lo, self.hi, value
        span = high - low
        fraction = 0.0 if abs(span) <= _EPS else (at - low) / span
        return 1.0 - fraction if self.invert else fraction


@dataclass(frozen=True, slots=True)
class Axis:
    """One axis, fully resolved: the span to map through, where the ticks are, what they read.

    ``pinned`` is the fact the DRAWING needs from it — a caller who fixed an end is asking for a
    window onto the data, so whatever falls outside has to be clipped rather than drawn over the
    axis it was supposed to be measured against.
    """

    scale: Scale
    ticks: tuple[float, ...]
    labels: tuple[str, ...]
    pinned: bool = False

    def pairs(self) -> list[tuple[float, str]]:
        """The ticks and their labels together, which is how every drawing routine wants them."""
        return list(zip(self.ticks, self.labels, strict=True))


def _positive_or_die(values: Sequence[float], axis: str) -> None:
    for value in values:
        if not math.isfinite(value) or value <= 0:
            raise InvalidArgument(
                f"a log {axis} axis cannot plot {value!r}: a log scale has no zero and no "
                "negative half, so every value and every pinned limit must be above zero"
            )


def _tick_target(ticks: int | Sequence[float] | None) -> int:
    if not isinstance(ticks, int):
        return _TICK_TARGET
    if ticks < 1:
        raise InvalidArgument(f"an axis needs at least one tick; {ticks} is not a count")
    return ticks


def resolve_axis(
    values: Sequence[float],
    *,
    lo_pin: float | None = None,
    hi_pin: float | None = None,
    zero: bool = False,
    log: bool = False,
    ticks: int | Sequence[float] | None = None,
    fmt: TickFormat | None = None,
    invert: bool = False,
    axis: str = "y",
) -> Axis:
    """Turn data plus a caller's wishes into a span, a tick list and the labels for it. Pure.

    The order matters and is the whole contract. The data sets the range; ``zero`` widens it to
    touch zero, but only at an end the caller did NOT pin — pinning is the stronger statement, and
    a bar chart asked to start at 20 must start at 20. The ticks are then chosen inside that range
    (or taken verbatim, out-of-range values dropped), and finally an UNPINNED end is stretched out
    to its nearest tick, which is what has always given a chart its round-numbered axis.
    """
    data_lo, data_hi = (min(values), max(values)) if values else (0.0, 1.0)
    if log:
        _positive_or_die([*values, *(pin for pin in (lo_pin, hi_pin) if pin is not None)], axis)
    elif zero:
        data_lo = data_lo if lo_pin is not None else min(data_lo, 0.0)
        data_hi = data_hi if hi_pin is not None else max(data_hi, 0.0)
    lo = data_lo if lo_pin is None else lo_pin
    hi = data_hi if hi_pin is None else hi_pin
    if hi <= lo:
        if lo_pin is not None and hi_pin is not None:
            raise InvalidArgument(
                f"the {axis} axis was pinned to an empty range: {lo_pin} to {hi_pin}"
            )
        lo, hi = _widen(lo, hi, log=log, low_fixed=lo_pin is not None)

    explicit = None if ticks is None or isinstance(ticks, int) else [float(t) for t in ticks]
    if explicit is None:
        target = _tick_target(ticks)
        chosen = log_ticks(lo, hi, target) if log else nice_ticks(lo, hi, target)
        lo = lo if lo_pin is not None else min(lo, chosen[0])
        hi = hi if hi_pin is not None else max(hi, chosen[-1])
    else:
        chosen = sorted(set(explicit))
    tolerance = max(abs(lo), abs(hi), 1.0) * 1e-9
    kept = [tick for tick in chosen if lo - tolerance <= tick <= hi + tolerance]
    labels = _tick_labels(kept, fmt=fmt, exact=explicit is None and not log)
    return Axis(
        scale=Scale(lo=lo, hi=hi, log=log, invert=invert),
        ticks=tuple(kept),
        labels=tuple(labels),
        pinned=lo_pin is not None or hi_pin is not None,
    )


def _widen(lo: float, hi: float, *, log: bool, low_fixed: bool) -> tuple[float, float]:
    """Open up a range that closed on itself, moving whichever end the caller did not pin."""
    if log:
        return (lo, lo * 10.0) if low_fixed else (hi / 10.0, hi)
    pad = max(1.0, abs(lo if low_fixed else hi) * 0.1)
    return (lo, lo + pad) if low_fixed else (hi - pad, hi)


def _tick_labels(ticks: Sequence[float], *, fmt: TickFormat | None, exact: bool) -> list[str]:
    """Label a tick list — at the STEP's precision for a ladder, at the value's for anything else.

    A ladder of 1/2/5 steps has one precision that suits every rung, and writing them all at it is
    what keeps ``0``/``0.5``/``1`` from reading as ``0``/``0.5``/``1.0``. An explicit list or a
    log axis has no single step, so each value is written at whatever precision it needs.
    """
    if fmt is not None:
        return [format_tick(tick, fmt) for tick in ticks]
    if exact:
        step = ticks[1] - ticks[0] if len(ticks) > 1 else 1.0
        decimals = tick_decimals(step)
        return [tick_text(tick, decimals) for tick in ticks]
    return [format_tick(tick, None) for tick in ticks]


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


def donut_angles(
    values: Sequence[float], start_degrees: float = -90.0
) -> list[tuple[float, float]]:
    """Each slice's ``(start, end)`` angle in radians, from ``start_degrees`` and going clockwise.

    The default is -90°, which is 12 o'clock — where a donut has always started here.

    The angles tile the full turn exactly: the last slice ENDS on the start angle plus 2π rather
    than accumulating its own share, so rounding can never leave a hairline wedge of canvas.
    """
    total = sum(values)
    if total <= 0:
        raise InvalidArgument("a donut needs a positive total to divide into slices")
    start = math.radians(start_degrees)
    angles: list[tuple[float, float]] = []
    running = 0.0
    for index, value in enumerate(values):
        first = start + math.tau * (running / total)
        running += value
        final = index == len(values) - 1
        angles.append((first, start + math.tau if final else start + math.tau * (running / total)))
    return angles


# --- the radar's wheel, pure --------------------------------------------------


def radar_angle(index: int, count: int) -> float:
    """Which way spoke ``index`` of ``count`` points, in radians. Pure.

    12 o'clock first and then CLOCKWISE, which is the order a reader takes a list of axis names
    in — the same convention the donut's default ``start_angle`` picks, for the same reason.
    """
    return -math.pi / 2.0 + index * math.tau / max(1, count)


def radar_points(
    values: Sequence[float], r_max: float, center: Point, radius: float, n: int
) -> list[Point]:
    """Where one series' values land on an ``n``-spoke wheel. Pure — the whole radial mapping.

    A value is mapped LINEARLY: 0 at the centre, ``r_max`` at the rim. Not by area, however
    tempting a polygon whose size should carry the quantity sounds — a reader takes a radar's
    vertex off the ruler written up the 12 o'clock spoke, and a vertex that did not sit where
    that ruler says it does would make the picture disagree with its own axis.
    """
    cx, cy = center
    scale = radius / r_max if r_max > 0 else 0.0
    placed: list[Point] = []
    for index, value in enumerate(values):
        angle = radar_angle(index, n)
        reach = value * scale
        placed.append((cx + reach * math.cos(angle), cy + reach * math.sin(angle)))
    return placed


def radar_rings(r_max: float, count: int = _RADAR_RINGS) -> list[float]:
    """The values the concentric rings sit at, innermost first, the rim last. Pure.

    The interior rings come off the same 1/2/5 ladder every axis is ticked on, so the numbers up
    the spoke are round ones — and the RIM is always a ring whatever the ladder says, because it
    is the frame's own boundary and a wheel drawn without one has no edge for the eye to close
    the profiles against. A ladder step landing on (or past) the rim is the rim, not a second
    ring a hairline inside it.
    """
    ladder = [tick for tick in nice_ticks(0.0, r_max, count) if _EPS < tick < r_max - _EPS]
    return [*ladder, r_max]


def radar_anchor(angle: float) -> str:
    """Which end of a spoke label sits on its spoke, by the octant the spoke points into. Pure.

    A label at the top or the bottom is centred over its spoke; one out to either side hangs off
    the near end of itself, so no label ever reaches back across the wheel it names.
    """
    cos = math.cos(angle)
    if abs(cos) < math.cos(math.radians(67.5)):
        return "middle"
    return "start" if cos > 0 else "end"


# --- ordering, stacking, steps and the furniture ------------------------------


def order_indices(
    labels: Sequence[str], keys: Sequence[float], order: CategoryOrder
) -> list[int]:
    """The permutation ``order`` asks for, as indices into ``labels``. Pure.

    ``value_*`` sorts by ``keys`` (stably, so equal values keep their given order) and ``label``
    alphabetically. An explicit list names the LEADING order: anything it does not mention keeps
    its given place after the named ones, and a name that is in the list but not in the data is
    an error naming it — that is a typo every time, and quietly dropping it would silently order
    the chart by something other than what was asked for.
    """
    count = len(labels)
    if isinstance(order, list):
        pools: dict[str, list[int]] = {}
        for index, label in enumerate(labels):
            pools.setdefault(label, []).append(index)
        unknown = [name for name in dict.fromkeys(order) if name not in pools]
        if unknown:
            named = ", ".join(repr(name) for name in unknown)
            have = ", ".join(repr(label) for label in labels)
            raise InvalidArgument(
                f"this chart has nothing named {named} to order by; it has {have}"
            )
        taken = [pools[name].pop(0) for name in order if pools[name]]
        return [*taken, *sorted(index for pool in pools.values() for index in pool)]
    if order == "label":
        return sorted(range(count), key=lambda index: labels[index])
    if order == "value_desc":
        return sorted(range(count), key=lambda index: keys[index], reverse=True)
    if order == "value_asc":
        return sorted(range(count), key=lambda index: keys[index])
    return list(range(count))


@dataclass(frozen=True, slots=True)
class Segment:
    """One stacked segment: the two values along the axis that its ends sit at."""

    lo: float
    hi: float

    @property
    def extent(self) -> float:
        return self.hi - self.lo

    @property
    def middle(self) -> float:
        return (self.lo + self.hi) / 2.0


def stack_segments(values: Sequence[float]) -> list[Segment]:
    """Where each series' segment sits in ONE stacked category, in data units. Pure.

    Positives accumulate upward from zero and negatives downward from zero, on two separate
    running totals. That is the standard treatment, and it is the only one in which a series that
    turns negative does not draw straight over the series beneath it. A zero contributes a
    zero-extent segment at the positive total rather than being skipped, so the stacking order is
    the series order however the numbers come out.
    """
    up = down = 0.0
    segments: list[Segment] = []
    for value in values:
        if value < 0:
            segments.append(Segment(down + value, down))
            down += value
        else:
            segments.append(Segment(up, up + value))
            up += value
    return segments


def waterfall_segments(values: Sequence[float], total: bool = False) -> list[Segment]:
    """Where each bar of a waterfall floats, in data units. Pure.

    Each bar starts at the running total of everything before it and ends at the running total
    including itself, so a positive climbs and a negative descends from wherever the previous one
    left off. With ``total``, one more segment is appended running from zero to the net total —
    the whole walk restated as a single bar from the ground.
    """
    segments: list[Segment] = []
    running = 0.0
    for value in values:
        segments.append(Segment(min(running, running + value), max(running, running + value)))
        running += value
    if total:
        segments.append(Segment(min(0.0, running), max(0.0, running)))
    return segments


def normalize_stacks(values: Sequence[Sequence[float]]) -> list[list[float]]:
    """``[series][category]`` rescaled so every category's stack spans 100. Pure.

    The denominator is the sum of the ABSOLUTE values, which is the only choice that leaves a
    stack crossing zero occupying 100 of axis rather than blowing up as its parts cancel. A
    category of all zeros has no total to take shares of and is left alone.
    """
    if not values:
        return []
    count = len(values[0])
    scales = [
        (100.0 / total if (total := sum(abs(row[index]) for row in values)) > 0 else 0.0)
        for index in range(count)
    ]
    return [[value * scales[index] for index, value in enumerate(row)] for row in values]


def histogram_edges(values: Sequence[float], bins: int | Sequence[float]) -> list[float]:
    """The bin boundaries a histogram counts into. Pure, and ascending by construction.

    A COUNT spreads equal-width bins across the data's own range. A range of nothing — every
    value the same — is padded the way a flat axis is padded (``max(1, |v| × 0.1)``) into ONE bin
    centred on the value, because a bin of zero width counts nothing however many of them there
    are. An explicit list is taken as the edges themselves, verbatim.
    """
    if not isinstance(bins, int):
        return [float(edge) for edge in bins]
    lo, hi = min(values), max(values)
    if hi - lo <= _EPS:
        width = max(1.0, abs(lo) * 0.1)
        return [lo - width / 2.0, lo + width / 2.0]
    step = (hi - lo) / bins
    return [lo + step * index for index in range(bins + 1)]


def histogram_counts(values: Sequence[float], edges: Sequence[float]) -> list[int]:
    """How many observations land in each bin. Pure.

    Every bin is half-open ``[lo, hi)`` — a value sitting exactly on an edge belongs to the bin
    ABOVE it — except the last, which closes at the top so the largest observation is counted
    rather than falling off the end. Anything outside the edges is not counted at all, which only
    happens when the caller named the edges: a derived ladder always covers its own data.
    """
    counts = [0] * (len(edges) - 1)
    for value in values:
        index = bisect_right(edges, value) - 1
        if index == len(counts) and value == edges[-1]:
            index -= 1  # the top edge closes the last bin rather than opening one past it
        if 0 <= index < len(counts):
            counts[index] += 1
    return counts


def widest_gap(low: Sequence[Point], high: Sequence[Point]) -> int:
    """Which pair of points a band is widest between — where its label has room. Pure."""
    if not low:
        return 0
    gaps = [abs(a[1] - b[1]) for a, b in zip(low, high, strict=True)]
    return gaps.index(max(gaps))


def stack_extents(values: Sequence[float]) -> tuple[float, float]:
    """How far down and how far up one stacked category reaches — what its axis must cover."""
    segments = stack_segments(values)
    return (
        min((piece.lo for piece in segments), default=0.0),
        max((piece.hi for piece in segments), default=0.0),
    )


def step_points(points: Sequence[Point], mode: Literal["pre", "post", "mid"]) -> list[Point]:
    """A staircase through ``points``. Pure, and agnostic about the units it is handed.

    ``post`` HOLDS each value along to the next x and rises there — the shape of a reading that
    stays put until it is next taken. ``pre`` rises at the current x and then holds. ``mid``
    changes halfway between the two readings, which is what to draw when the number describes a
    PERIOD and neither of its ends is the moment anything happened.
    """
    if len(points) < 2:
        return list(points)
    stepped: list[Point] = [points[0]]
    for (x0, y0), (x1, y1) in zip(points, points[1:], strict=False):
        if mode == "mid":
            middle = (x0 + x1) / 2.0
            stepped.extend(((middle, y0), (middle, y1)))
        else:
            stepped.append((x1, y0) if mode == "post" else (x0, y1))
        stepped.append((x1, y1))
    return stepped


def label_centre(
    end: float, outward: float, *, gap: float, extent: float, lo: float, hi: float
) -> float:
    """Where a value label's centre goes along the value direction, in SCREEN units. Pure.

    ``outward`` is +1 when leaving the mark means increasing the coordinate and -1 when it means
    decreasing it, so one routine serves a bar that grows up, one that grows down, and one that
    grows to the right. The label sits ``gap`` clear of the mark's end; when the label would then
    reach past ``lo``/``hi`` — the plot's own edges — it flips to the INSIDE of the mark instead,
    because a number half over the frame is worse than a number over its own bar.
    """
    outside = end + outward * (gap + extent / 2.0)
    if lo <= outside - extent / 2.0 and outside + extent / 2.0 <= hi:
        return outside
    return end - outward * (gap + extent / 2.0)


def reference_extent(
    value: float, to: float | None, scale: Scale
) -> tuple[float, float] | None:
    """What a reference occupies on ``scale``, in data units, or None if it draws nothing. Pure.

    A LINE outside the axis is DROPPED — it is furniture, and furniture jammed against the frame
    reads as part of the frame rather than as a threshold. A BAND is CLAMPED to the axis instead:
    the part of it that is in range is still true, and half a band beats no band.
    """
    lo, hi = min(scale.lo, scale.hi), max(scale.lo, scale.hi)
    tolerance = max(abs(lo), abs(hi), 1.0) * 1e-9
    if to is None:
        return (value, value) if lo - tolerance <= value <= hi + tolerance else None
    low, high = min(value, to), max(value, to)
    if high < lo - tolerance or low > hi + tolerance:
        return None
    return (max(low, lo), min(high, hi))


# --- plot geometry -----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Margins:
    """The room a plot leaves around itself for the labels that describe it."""

    left: float
    top: float
    right: float
    bottom: float


@dataclass(frozen=True, slots=True)
class Sizes:
    """The type sizes and the gap a chart lays out against — the theme's numbers, not guesses.

    These used to be module constants that ``plot_margins`` measured with while the CSS set the
    sizes the text was actually DRAWN at, so a theme restyling ``.tick-label`` to 16px got either
    clipped labels or a channel of slack. The theme's ``--chart-*`` tokens now feed both halves.
    """

    tick: float = _TICK_SIZE
    axis_label: float = _AXIS_LABEL_SIZE
    title: float = _TITLE_SIZE
    gap: float = _GAP
    value_label: float = _VALUE_LABEL_SIZE
    reference_label: float = _REFERENCE_LABEL_SIZE
    donut_center: float = _DONUT_CENTER_SIZE
    donut_subtext: float = _DONUT_SUBTEXT_SIZE


_DEFAULT_SIZES = Sizes()


def chart_sizes(theme: ServingTheme) -> Sizes:
    """Read a chart's layout sizes off the serving theme, falling back to the bundled values."""
    return Sizes(
        tick=_token(theme, "--chart-tick-size", _TICK_SIZE),
        axis_label=_token(theme, "--chart-axis-label-size", _AXIS_LABEL_SIZE),
        title=_token(theme, "--chart-title-size", _TITLE_SIZE),
        gap=_token(theme, "--chart-gap", _GAP),
        value_label=_token(theme, "--chart-value-label-size", _VALUE_LABEL_SIZE),
        reference_label=_token(theme, "--chart-reference-label-size", _REFERENCE_LABEL_SIZE),
        donut_center=_token(theme, "--chart-donut-center-size", _DONUT_CENTER_SIZE),
        donut_subtext=_token(theme, "--chart-donut-subtext-size", _DONUT_SUBTEXT_SIZE),
    )


def rotated_extent(width: float, height: float, degrees: float) -> float:
    """How much vertical room a label of this size needs once it is turned by ``degrees``."""
    radians = math.radians(degrees)
    return abs(width * math.sin(radians)) + abs(height * math.cos(radians))


def rotated_width(width: float, height: float, degrees: float) -> float:
    """The same for the HORIZONTAL room — what a turned y tick label costs the left margin."""
    radians = math.radians(degrees)
    return abs(width * math.cos(radians)) + abs(height * math.sin(radians))


def tick_reach(length: float, direction: Literal["out", "in", "inout"]) -> tuple[float, float]:
    """How far a tick mark of this length stands (outward, inward) from its axis. Pure.

    Only the OUTWARD half is ever charged to the margins: an inward tick is drawn across the plot
    rect, which is space the chart already owns.
    """
    reach = max(0.0, length)
    if direction == "in":
        return (0.0, reach)
    return (reach, reach if direction == "inout" else 0.0)


def plot_margins(
    y_tick_labels: Sequence[str],
    *,
    font: str,
    title: bool,
    x_label: bool,
    y_label: bool,
    sizes: Sizes = _DEFAULT_SIZES,
    tick_marks: float = 0.0,
    x_tick_labels: Sequence[str] = (),
    x_tick_rotate: float = 0.0,
    y_tick_rotate: float = 0.0,
) -> Margins:
    """How much room the labels need — MEASURED, so a six-digit axis is not clipped or crowded.

    The left margin is the widest y tick label plus a gap, which is why a chart of thousands
    indents further than a chart of tens. Axis titles add their own line on the side they sit,
    tick marks push the labels out by their own length (the OUTWARD length — see
    :func:`tick_reach`), and a turned label on either axis claims the room its turned bounding box
    actually occupies rather than one line's worth.
    """
    widest = max(
        (
            rotated_width(*measure_label(text, font, sizes.tick), y_tick_rotate)
            if y_tick_rotate
            else measure_label(text, font, sizes.tick)[0]
            for text in y_tick_labels
        ),
        default=0.0,
    )
    tick_height = measure_label("0", font, sizes.tick)[1]
    if x_tick_rotate and x_tick_labels:
        tick_height = max(
            rotated_extent(*measure_label(text, font, sizes.tick), x_tick_rotate)
            for text in x_tick_labels
        )
    axis_allowance = sizes.axis_label + 4.0
    return Margins(
        left=widest + sizes.gap + tick_marks + (axis_allowance if y_label else 0.0),
        top=(sizes.title + sizes.gap) if title else 4.0,
        right=sizes.gap,
        bottom=tick_height + sizes.gap + tick_marks + (axis_allowance if x_label else 0.0),
    )


@dataclass(frozen=True, slots=True)
class Plot:
    """The rectangle the data is drawn in, in the chart group's own local frame."""

    x: float
    y: float
    w: float
    h: float

    def map_x(self, value: float, scale: Scale) -> float:
        return self.x + scale.unit(value) * self.w

    def map_y(self, value: float, scale: Scale) -> float:
        return self.y + self.h - scale.unit(value) * self.h


def _plot_rect(w: float, h: float, margins: Margins) -> Plot:
    """The plot rect, never allowed to invert however much the labels ask for."""
    return Plot(
        x=margins.left,
        y=margins.top,
        w=max(1.0, w - margins.left - margins.right),
        h=max(1.0, h - margins.top - margins.bottom),
    )


# --- the spec ----------------------------------------------------------------


class ChartDatum(BaseModel):
    """One datum in a chart, named the way a caller can actually name it.

    ``series`` is a name or an index into the series list; ``index`` is the position WITHIN the
    data as it was given — a bar's category, a point's place in its series, a donut's slice — so
    it still names the same number after an ``order`` has moved it somewhere else on the page.
    A donut has one series, so only ``index`` means anything there.
    """

    model_config = ConfigDict(extra="forbid", use_attribute_docstrings=True)

    series: str | int = 0
    index: int = 0


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
    axes: AxesSpec | None = None


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
        axes = spec.get("axes")
        return ChartSpec(
            kind=kind,
            data=_MODELS[kind].model_validate(spec["data"]),
            title=str(spec.get("title", "")),
            x_label=str(spec.get("x_label", "")),
            y_label=str(spec.get("y_label", "")),
            w=float(spec["w"]),
            h=float(spec["h"]),
            auto=bool(spec.get("auto", False)),
            axes=None if axes is None else AxesSpec.model_validate(axes),
        )
    except (ValueError, TypeError, KeyError, ValidationError):
        return None


def _write_chart_spec(element: BaseElement, spec: ChartSpec) -> None:
    """Store the spec. An absent ``axes`` writes no key at all — the compatibility contract is
    that a chart nobody gave axes to is byte-for-byte the chart this module always wrote."""
    stored: dict[str, object] = {
        "kind": spec.kind,
        "data": spec.data.model_dump(),
        "title": spec.title,
        "x_label": spec.x_label,
        "y_label": spec.y_label,
        "w": spec.w,
        "h": spec.h,
        "auto": spec.auto,
    }
    if spec.axes is not None:
        stored["axes"] = spec.axes.model_dump()
    element.set(_CHART_ATTR, json.dumps(stored, separators=(",", ":")))


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


def _rotated_anchor(degrees: float) -> tuple[str, str]:
    """Where a turned x tick label hangs from its tick: by its end going up-left, start going up-
    right. An unturned label is centred under the tick, which is the only case that reads level."""
    if not degrees:
        return "middle", "alphabetic"
    return ("end" if degrees < 0 else "start"), "central"


def _draw_frame(
    doc: Document,
    parent: str,
    theme: str | None,
    *,
    plot: Plot,
    spec: ChartSpec,
    y_ticks: Sequence[tuple[float, str]],
    x_ticks: Sequence[tuple[float, str]],
    font: str,
    axes: AxesSpec,
    sizes: Sizes,
    y_minor: Sequence[float] = (),
    x_minor: Sequence[float] = (),
    bottom_at: float | None = None,
    left_at: float | None = None,
) -> None:
    """The scenery: gridlines, the two axes, the tick marks, the tick labels, the axis titles.

    Both tick lists arrive as POSITIONS in the chart's own frame, already mapped, paired with the
    text to write there. That is what lets horizontal bars swap the two over — the value ticks go
    along the bottom and the category names down the side — without a second copy of this routine.
    ``axes.gridlines`` is read in the same screen terms: ``y`` is the set of horizontal lines, and
    the minor positions (also already mapped) follow whichever sets it names.

    Gridlines are drawn FIRST so the data is never hidden behind them — minor before major, so a
    major reads over its own subdivisions — and the axes after, so the baseline reads as a line
    rather than as the last gridline. Tick marks sit outside the rect unless
    ``axes.tick_direction`` says otherwise, which is why the margins make room for their outward
    half alone.

    ``bottom_at``/``left_at`` move an axis LINE off the edge of the plot (the zero spine); the
    ticks and their labels stay where they were, which is the whole of that feature's argument.
    """
    frame = _group(doc, parent)
    frame_id = str(frame.get_id())
    grid = _hooks(doc, theme, "gridline")
    minor_grid = [*grid, *_hooks(doc, theme, "gridline-minor")]
    across = axes.gridlines in ("y", "both")
    down = axes.gridlines in ("x", "both")
    if axes.minor_gridlines:
        for at in y_minor if across else ():
            _line(doc, frame_id, (plot.x, at), (plot.x + plot.w, at), minor_grid)
        for at in x_minor if down else ():
            _line(doc, frame_id, (at, plot.y), (at, plot.y + plot.h), minor_grid)
    if across:
        for at, _ in y_ticks:
            _line(doc, frame_id, (plot.x, at), (plot.x + plot.w, at), grid)
    if down:
        for at, _ in x_ticks:
            _line(doc, frame_id, (at, plot.y), (at, plot.y + plot.h), grid)
    axis = _hooks(doc, theme, "axis")
    spine_x = plot.x if left_at is None else left_at
    spine_y = (plot.y + plot.h) if bottom_at is None else bottom_at
    _line(doc, frame_id, (spine_x, plot.y), (spine_x, plot.y + plot.h), axis)
    _line(doc, frame_id, (plot.x, spine_y), (plot.x + plot.w, spine_y), axis)

    out, into = tick_reach(axes.tick_marks or 0.0, axes.tick_direction)
    marks = max(out, into)
    if marks > 0:
        mark_class = _hooks(doc, theme, "tick")
        minor_class = [*mark_class, *_hooks(doc, theme, "tick-minor")]
        base = plot.y + plot.h
        for at, _ in y_ticks:
            _line(doc, frame_id, (plot.x - out, at), (plot.x + into, at), mark_class)
        for at, _ in x_ticks:
            _line(doc, frame_id, (at, base - into), (at, base + out), mark_class)
        # Half length, so the ruler's two orders of subdivision are told apart by size before
        # anyone reads a number off either.
        for at in y_minor:
            _line(doc, frame_id, (plot.x - out / 2.0, at), (plot.x + into / 2.0, at), minor_class)
        for at in x_minor:
            _line(doc, frame_id, (at, base - into / 2.0), (at, base + out / 2.0), minor_class)

    tick_class = _hooks(doc, theme, "tick-label")
    for at, text in y_ticks:
        _text(
            doc,
            frame_id,
            text,
            (plot.x - 4.0 - out, at),
            anchor="end",
            baseline="central",
            classes=tick_class,
            rotate=axes.y_tick_rotate or None,
        )
    tick_height = measure_label("0", font, sizes.tick)[1]
    anchor, baseline = _rotated_anchor(axes.x_tick_rotate)
    drop = out + (tick_height * 0.85 if not axes.x_tick_rotate else tick_height * 0.5)
    for at, text in x_ticks:
        _text(
            doc,
            frame_id,
            text,
            (at, plot.y + plot.h + drop),
            anchor=anchor,
            baseline=baseline,
            classes=tick_class,
            rotate=axes.x_tick_rotate or None,
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
        (sizes.axis_label, plot.y + plot.h / 2.0),
        anchor="middle",
        baseline="alphabetic",
        classes=label_class,
        rotate=-90.0,
    )


def _draw_title(
    doc: Document,
    parent: str,
    theme: str | None,
    plot: Plot,
    title: str,
    sizes: Sizes = _DEFAULT_SIZES,
) -> None:
    _text(
        doc,
        parent,
        title,
        (plot.x + plot.w / 2.0, sizes.title),
        anchor="middle",
        baseline="alphabetic",
        classes=_hooks(doc, theme, "chart-title"),
    )


# --- marks, patterns, and the window the data is cut to ------------------------


def marker_points(shape: Marker, cx: float, cy: float, size: float) -> list[Point]:
    """The corners of a polygonal mark, from the top clockwise. Round, stroked and absent marks
    have none: a circle has no corners, and ``plus``/``cross`` are :func:`marker_strokes`.

    ``size`` is the half-extent — the radius of the circle the mark would replace — so swapping a
    shape never changes how much ink a point carries.
    """
    if shape == "square":
        return [
            (cx - size, cy - size),
            (cx + size, cy - size),
            (cx + size, cy + size),
            (cx - size, cy + size),
        ]
    if shape == "diamond":
        return [(cx, cy - size), (cx + size, cy), (cx, cy + size), (cx - size, cy)]
    if shape in ("triangle", "tri_down"):
        turn = math.pi / 2.0 if shape == "tri_down" else -math.pi / 2.0
        return [
            (cx + size * math.cos(turn + step), cy + size * math.sin(turn + step))
            for step in (0.0, math.tau / 3.0, 2.0 * math.tau / 3.0)
        ]
    if shape == "star":
        # Ten corners, alternating out and in on the pentagram ratio: the inner ones have to sit
        # where the outer points' own edges already cross, or the star reads as a lumpy flower.
        corners: list[Point] = []
        for index in range(10):
            angle = -math.pi / 2.0 + index * math.tau / 10.0
            radius = size if index % 2 == 0 else size * _STAR_INNER
            corners.append((cx + radius * math.cos(angle), cy + radius * math.sin(angle)))
        return corners
    return []


def marker_strokes(shape: Marker, cx: float, cy: float, size: float) -> list[tuple[Point, Point]]:
    """The two strokes of a mark that is LINE rather than area, or nothing for the rest. Pure.

    ``plus`` and ``cross`` have no inside to fill, which is the point of them: on a chart printed
    in one colour they are the two marks that stay legible on top of another series. ``cross``
    reaches the same distance as the others — its arms are the diagonals of the square inscribed
    in the circle of radius ``size``, not that square's sides.
    """
    if shape == "plus":
        return [((cx, cy - size), (cx, cy + size)), ((cx - size, cy), (cx + size, cy))]
    if shape == "cross":
        arm = size * math.sqrt(0.5)
        return [
            ((cx - arm, cy - arm), (cx + arm, cy + arm)),
            ((cx - arm, cy + arm), (cx + arm, cy - arm)),
        ]
    return []


def marker_radii(
    sizes: Sequence[float], scale: tuple[float, float] | None, observed: tuple[float, float]
) -> list[float]:
    """One radius per datum, from the caller's numbers. Pure — the bubble channel's whole rule.

    Without a ``scale`` the numbers ARE radii and come back untouched. With one they are
    quantities, and what has to carry the quantity is the mark's AREA: a bubble twice the radius
    is four times the ink, so a linear radius mapping would quadruple what the reader sees for a
    doubled number. Interpolating the SQUARE of the radius across ``observed`` — which is the
    range the whole chart's sizes span, not one series' — is what makes two bubbles' areas stand
    in the same ratio as the two numbers' distances up that range.

    Every value the same leaves no range to map across; those all come out at ``max_r``, because a
    size that carries no information should at least be visible.
    """
    if scale is None:
        return [float(size) for size in sizes]
    min_r, max_r = scale
    low, high = observed
    span = high - low
    return [
        math.sqrt(
            min_r**2
            + (1.0 if abs(span) <= _EPS else (size - low) / span) * (max_r**2 - min_r**2)
        )
        for size in sizes
    ]


def _draw_mark(
    doc: Document,
    parent: str,
    shape: Marker,
    at: Point,
    size: float,
    *,
    unfilled: bool = False,
    classes: Sequence[str] = (),
) -> None:
    """One data point's mark.

    A FILLED mark carries no class: the series group above it paints it, and it takes a structural
    ``stroke: none`` so the fill is the whole of the number. A mark that is drawn by its stroke
    instead — ``plus``/``cross``, which have no area, or any shape asked to draw ``open`` — inverts
    that, and wears the series class itself, because reading the markup should show at a glance
    which half of the series' paint is doing the work.
    """
    cx, cy = at
    if shape == "none":
        return
    strokes = marker_strokes(shape, cx, cy, size)
    if strokes:
        steps = " ".join(
            f"M {_num(a[0])} {_num(a[1])} L {_num(b[0])} {_num(b[1])}" for a, b in strokes
        )
        _part(
            doc,
            inkex.PathElement.new(steps),
            prefix="path",
            category="shape",
            parent=parent,
            style={"fill": "none"},
            classes=classes,
        )
        return
    if shape == "circle":
        element: BaseElement = inkex.Circle.new((cx, cy), size)
        prefix = "circle"
    elif shape == "square":
        element = inkex.Rectangle.new(cx - size, cy - size, size * 2.0, size * 2.0)
        prefix = "rect"
    else:
        corners = marker_points(shape, cx, cy, size)
        steps = " L ".join(f"{_num(x)} {_num(y)}" for x, y in corners)
        element = inkex.PathElement.new(f"M {steps} Z")
        prefix = "path"
    _part(
        doc,
        element,
        prefix=prefix,
        category="shape",
        parent=parent,
        style=(
            {"fill": "none", "stroke-width": _OPEN_STROKE}
            if unfilled
            else {"stroke": "none"}  # a mark is a fill (see the bars)
        ),
        classes=classes if unfilled else (),
    )


def _stage(doc: Document, parent: str, element: BaseElement, prefix: str) -> str:
    """Put a node in the tree so a ``define_*`` can move it into ``<defs>``; returns its id."""
    doc.resolve(parent).add(element)
    element.set_id(doc.new_id(prefix))
    return str(element.get_id())


def _own(doc: Document, resource: str, owner: str) -> str:
    """Stamp a ``<defs>`` resource with the chart that built it. See :data:`_CLIP_OWNER`."""
    doc.resolve(resource).set(_CLIP_OWNER, owner)
    return resource


def _purge_owned_defs(doc: Document, owner: str) -> None:
    """Delete the ``<defs>`` resources a previous build of this chart left behind."""
    for node in list(doc.svg.iter()):
        if isinstance(node.tag, str) and node.get(_CLIP_OWNER) == owner:
            node.delete()


def _clip_to_plot(doc: Document, group: str, plot: Plot) -> str:
    """A clipPath the shape of the plot rect, in the chart's own local frame; returns its id.

    Pinning a limit turns the plot into a WINDOW onto the data, and a window that lets a bar hang
    past the axis it is measured against is worse than no window at all.
    """
    rect = inkex.Rectangle.new(plot.x, plot.y, plot.w, plot.h)
    clip = define_clip(doc, content=[_stage(doc, group, rect, "chart-clip")])
    return _own(doc, clip, group)


def _hatch(doc: Document, group: str, classes: Sequence[str]) -> str | None:
    """A diagonal hatch pattern painted in one series' colour; returns its id, or None.

    The stroke is NOT written here: the line inside the pattern wears the series class and is
    painted by the same rule that paints the bars, so a variant switch or an ``apply_theme``
    moves the hatching with everything else. That also means a chart wearing no dressing has
    nothing to hatch WITH — such a chart keeps its flat fill rather than turning invisible.
    """
    if not classes:
        return None
    line = inkex.Line.new((0.0, 0.0), (0.0, _HATCH_TILE))
    line.set("class", " ".join(classes))
    line.set("style", f"fill:none;stroke-width:{_HATCH_WIDTH}")
    pattern = define_pattern(
        doc,
        content=[_stage(doc, group, line, "chart-hatch")],
        width=_HATCH_TILE,
        height=_HATCH_TILE,
        units="userSpaceOnUse",
        pattern_transform="rotate(45)",
    )
    return _own(doc, pattern, group)


def _fill_of(pattern: str | None) -> Style:
    return {"fill": f"url(#{pattern})"} if pattern is not None else {}


# --- laid-out geometry --------------------------------------------------------
#
# Each plot's ARITHMETIC is separated from its drawing, because two callers need it: the routine
# that draws the chart, and :func:`datum_anchor`, which has to find one bar, point or slice of a
# chart already on the page. Deriving both from one function is what keeps a callout's leader on
# the right mark after an ``edit_chart`` has moved every mark somewhere else.


def _swapped_grid(which: Gridlines) -> Gridlines:
    """Turn a set of gridlines through 90° — what horizontal bars ask of the frame."""
    if which == "x":
        return "y"
    if which == "y":
        return "x"
    return which


@dataclass(frozen=True, slots=True)
class BarLayout:
    """Everything a bar chart decides before a single rectangle is written.

    ``values`` and ``categories`` are in DRAWING order (``order`` maps a drawn position back to
    the caller's own index), and every coordinate question is asked along one of two directions:
    the VALUE direction, which is y on a vertical chart and x on a horizontal one, and the
    CATEGORY direction, which is the other one.
    """

    plot: Plot
    axis: Axis
    frame_axes: AxesSpec
    horizontal: bool
    stacked: bool
    origin: float
    """The value a bar is measured FROM — zero, or the axis floor on a log scale."""
    categories: tuple[str, ...]
    order: tuple[int, ...]
    values: tuple[tuple[float, ...], ...]
    """``[series][category]``, in drawing order."""
    segments: tuple[tuple[Segment, ...], ...]
    """``[category][series]`` — where each series sits in that category's stack, or where that
    category's single bar floats when this is a waterfall."""
    bands: tuple[tuple[Band, ...], ...]
    y_ticks: tuple[tuple[float, str], ...]
    x_ticks: tuple[tuple[float, str], ...]
    waterfall: bool = False
    """Each bar floats from the running total rather than growing from the origin."""
    y_minor: tuple[float, ...] = ()
    x_minor: tuple[float, ...] = ()
    bottom_at: float | None = None
    """Where the bottom axis LINE goes, if not along the bottom of the plot (the zero spine)."""
    left_at: float | None = None
    """The same for the left axis line — which is the one that moves on horizontal bars."""

    @property
    def floating(self) -> bool:
        """Whether a bar's two ends come from its segment rather than from the origin."""
        return self.stacked or self.waterfall

    def band(self, category: int, series: int) -> Band:
        """The slot one bar occupies. A stack's series all share the category's single band."""
        return self.bands[category][0 if self.floating else series]

    def span(self, category: int, series: int) -> tuple[float, float]:
        """The two VALUES one bar runs between — its segment's ends when it floats."""
        if self.floating:
            piece = self.segments[category][series]
            return (piece.lo, piece.hi)
        value = self.values[series][category]
        return (min(self.origin, value), max(self.origin, value))

    def map_value(self, value: float) -> float:
        """A value's screen coordinate along the value direction."""
        if self.horizontal:
            return self.plot.map_x(value, self.axis.scale)
        return self.plot.map_y(value, self.axis.scale)

    def centre(self, band: Band) -> float:
        """The middle of a band along the category direction, in screen units."""
        return (self.plot.y if self.horizontal else self.plot.x) + band.x + band.w / 2.0

    def rect(self, category: int, series: int) -> tuple[float, float, float, float]:
        """One bar as ``(x, y, w, h)`` in the chart's own frame."""
        band = self.band(category, series)
        start = (self.plot.y if self.horizontal else self.plot.x) + band.x
        low, high = self.span(category, series)
        near, far = sorted((self.map_value(low), self.map_value(high)))
        if self.horizontal:
            return (near, start, far - near, band.w)
        return (start, near, band.w, far - near)

    def outward(self, value: float) -> float:
        """Which way is OUT of a bar carrying this value, as a sign on the screen coordinate."""
        if self.horizontal:
            return 1.0 if value >= self.origin else -1.0
        return -1.0 if value >= self.origin else 1.0

    def bounds(self) -> tuple[float, float]:
        """The plot's two edges along the value direction — what a label has to stay inside."""
        if self.horizontal:
            return (self.plot.x, self.plot.x + self.plot.w)
        return (self.plot.y, self.plot.y + self.plot.h)

    def across(self, at: float, along: float) -> Point:
        """A point from its value coordinate and its category coordinate, whichever way round."""
        return (at, along) if self.horizontal else (along, at)

    def datum_point(self, category: int, series: int) -> Point:
        """Where a leader lands on one bar: the middle of the end it grows to (a stacked
        segment has no free end, so it takes its own centre instead)."""
        along = self.centre(self.band(category, series))
        if self.stacked:
            return self.across(self.map_value(self.segments[category][series].middle), along)
        low, high = self.span(category, series)
        value = self.values[series][category]
        return self.across(self.map_value(high if value >= self.origin else low), along)


def _bar_layout(spec: ChartSpec, *, font: str, sizes: Sizes) -> BarLayout:
    """Resolve a bar chart's order, axis, margins, plot rect and bands. No document needed."""
    data = spec.data
    if not isinstance(data, BarData):
        raise InvalidArgument("a bar chart needs bar data")
    axes = spec.axes or _DEFAULT_AXES
    log = axes.scale == "log"
    if data.stacked and log:
        raise InvalidArgument(
            "a stacked bar chart cannot be drawn on a log scale: a stack is a sum measured from "
            "zero, and a log axis has no zero to measure it from"
        )
    rows = [entry.values for entry in data.series]
    count = len(data.categories)
    # What `value_*` ranks by: the first series is the one the eye reads as the subject, except
    # on a stack, where no single series is the subject and only the total is honest.
    keys = (
        [sum(row[index] for row in rows) for index in range(count)]
        if data.stacked
        else list(rows[0])
    )
    order = order_indices(data.categories, keys, data.order)
    categories = [data.categories[index] for index in order]
    values = [[row[index] for index in order] for row in rows]
    if data.normalized:
        values = normalize_stacks(values)
    segments: list[tuple[Segment, ...]]
    if data.waterfall:
        segments = [
            (piece,)
            for piece in waterfall_segments(values[0], total=data.total_label is not None)
        ]
        if data.total_label is not None:
            # The net total is one more bar, and one more addressable datum: giving it the index
            # it would have had as a category is what lets a callout point at "the total" at all.
            categories = [*categories, data.total_label]
            values = [[*values[0], sum(values[0])]]
            order = [*order, count]
    else:
        segments = [
            tuple(stack_segments([row[index] for row in values]))
            for index in range(len(categories))
        ]
    # A stack's AXIS is scaled to the totals, not to the parts: the tallest thing on the page is
    # the tallest stack, and an axis measured from the parts would let it out of the plot. A
    # waterfall's bars float, so its axis is scaled to where they float BETWEEN.
    flat = (
        [value for column in segments for piece in column for value in (piece.lo, piece.hi)]
        if data.stacked or data.waterfall
        else [value for row in values for value in row]
    )
    y_axis = resolve_axis(
        flat,
        lo_pin=axes.y_min,
        hi_pin=axes.y_max,
        zero=not log,
        log=log,
        ticks=axes.ticks,
        fmt=axes.tick_format,
        invert=axes.invert_y,
    )
    horizontal = data.orientation == "horizontal"
    # Horizontal bars put the CATEGORY names down the side, so they are what the left margin is
    # measured from — which is the whole reason to turn a bar chart on its side.
    side_labels = categories if horizontal else list(y_axis.labels)
    foot_labels = list(y_axis.labels) if horizontal else categories
    margins = plot_margins(
        side_labels,
        font=font,
        title=bool(spec.title),
        x_label=bool(spec.x_label),
        y_label=bool(spec.y_label),
        sizes=sizes,
        tick_marks=tick_reach(axes.tick_marks or 0.0, axes.tick_direction)[0],
        x_tick_labels=foot_labels,
        x_tick_rotate=axes.x_tick_rotate,
        y_tick_rotate=axes.y_tick_rotate,
    )
    plot = _plot_rect(spec.w, spec.h, margins)
    bands = bar_bands(
        plot.h if horizontal else plot.w,
        len(categories),
        1 if data.stacked or data.waterfall else len(values),
    )

    def _map(value: float) -> float:
        return plot.map_x(value, y_axis.scale) if horizontal else plot.map_y(value, y_axis.scale)

    value_ticks = [(_map(tick), text) for tick, text in y_axis.pairs()]
    value_minor = [_map(tick) for tick in _minor_of(axes, y_axis)]
    along = plot.y if horizontal else plot.x
    category_ticks = [
        (along + (row[0].x + row[-1].x + row[-1].w) / 2.0, name)
        for row, name in zip(bands, categories, strict=True)
    ]
    spine = _map(0.0) if _spine_at_zero(axes, y_axis) else None
    return BarLayout(
        plot=plot,
        axis=y_axis,
        frame_axes=(
            axes.model_copy(update={"gridlines": _swapped_grid(axes.gridlines)})
            if horizontal
            else axes
        ),
        horizontal=horizontal,
        stacked=data.stacked,
        waterfall=data.waterfall,
        # A bar measures from zero, because that is what makes its LENGTH the number. On a log
        # axis there is no zero to measure from, so it measures from the bottom of the axis.
        origin=y_axis.scale.lo if log else 0.0,
        categories=tuple(categories),
        order=tuple(order),
        values=tuple(tuple(row) for row in values),
        segments=tuple(segments),
        bands=tuple(tuple(row) for row in bands),
        y_ticks=tuple(category_ticks if horizontal else value_ticks),
        x_ticks=tuple(value_ticks if horizontal else category_ticks),
        y_minor=() if horizontal else tuple(value_minor),
        x_minor=tuple(value_minor) if horizontal else (),
        # The value axis runs along x when the bars are turned, so the spine that stands at zero
        # is the LEFT one there and the bottom one otherwise.
        bottom_at=None if horizontal else spine,
        left_at=spine if horizontal else None,
    )


@dataclass(frozen=True, slots=True)
class PointLayout:
    """A line or scatter chart's arithmetic: both axes, and where every datum landed."""

    plot: Plot
    x_axis: Axis
    y_axis: Axis
    placed: tuple[tuple[Point, ...], ...]
    """``[series][point]`` — the marks' screen positions, before any stepping."""
    y_ticks: tuple[tuple[float, str], ...]
    x_ticks: tuple[tuple[float, str], ...]
    radii: tuple[tuple[float, ...], ...] = ()
    """``[series][point]`` — each mark's radius, once the bubble channel has had its say."""
    y_minor: tuple[float, ...] = ()
    x_minor: tuple[float, ...] = ()
    bottom_at: float | None = None

    def radius(self, series: int, index: int) -> float:
        """One mark's radius, or 0 where this chart draws no marks at all."""
        if series >= len(self.radii) or index >= len(self.radii[series]):
            return 0.0
        return self.radii[series][index]


def _sizes_span(series: Sequence[PointSeries]) -> tuple[float, float]:
    """The range the whole chart's ``sizes`` cover — one range, so two series' bubbles compare."""
    found = [size for entry in series if entry.sizes for size in entry.sizes]
    return (min(found), max(found)) if found else (0.0, 1.0)


def _points_layout(spec: ChartSpec, *, font: str, sizes: Sizes) -> PointLayout:
    """Resolve a line/scatter chart's two axes, its margins and its mapped points."""
    data = spec.data
    if not isinstance(data, LineData | ScatterData):
        raise InvalidArgument("a line or scatter chart needs point-series data")
    axes = spec.axes or _DEFAULT_AXES
    log = axes.scale == "log"
    area = isinstance(data, LineData) and data.area
    (x_lo, x_hi), (y_lo, y_hi) = _point_span(data.series)
    y_axis = resolve_axis(
        [y_lo, y_hi],
        lo_pin=axes.y_min,
        hi_pin=axes.y_max,
        zero=area and not log,
        log=log,
        ticks=axes.ticks,
        fmt=axes.tick_format,
        invert=axes.invert_y,
    )
    x_axis = resolve_axis(
        [x_lo, x_hi],
        lo_pin=axes.x_min,
        hi_pin=axes.x_max,
        ticks=axes.x_ticks,
        fmt=axes.x_tick_format,
        invert=axes.invert_x,
        axis="x",
    )
    margins = plot_margins(
        y_axis.labels,
        font=font,
        title=bool(spec.title),
        x_label=bool(spec.x_label),
        y_label=bool(spec.y_label),
        sizes=sizes,
        tick_marks=tick_reach(axes.tick_marks or 0.0, axes.tick_direction)[0],
        x_tick_labels=x_axis.labels,
        x_tick_rotate=axes.x_tick_rotate,
        y_tick_rotate=axes.y_tick_rotate,
    )
    plot = _plot_rect(spec.w, spec.h, margins)
    flat = data.marker_size or (_SCATTER_R if isinstance(data, ScatterData) else _MARKER_R)
    observed = _sizes_span(data.series)
    return PointLayout(
        plot=plot,
        x_axis=x_axis,
        y_axis=y_axis,
        placed=tuple(
            tuple(
                (plot.map_x(px, x_axis.scale), plot.map_y(py, y_axis.scale))
                for px, py in entry.points
            )
            for entry in data.series
        ),
        radii=tuple(
            tuple(
                marker_radii(entry.sizes, data.marker_scale, observed)
                if entry.sizes is not None
                else [flat] * len(entry.points)
            )
            for entry in data.series
        ),
        y_ticks=tuple((plot.map_y(tick, y_axis.scale), text) for tick, text in y_axis.pairs()),
        x_ticks=tuple((plot.map_x(tick, x_axis.scale), text) for tick, text in x_axis.pairs()),
        y_minor=tuple(plot.map_y(tick, y_axis.scale) for tick in _minor_of(axes, y_axis)),
        x_minor=tuple(plot.map_x(tick, x_axis.scale) for tick in _minor_of(axes, x_axis)),
        bottom_at=plot.map_y(0.0, y_axis.scale) if _spine_at_zero(axes, y_axis) else None,
    )


@dataclass(frozen=True, slots=True)
class HistogramLayout:
    """A histogram's arithmetic: the bins it counted into, and where each bar landed."""

    plot: Plot
    x_axis: Axis
    y_axis: Axis
    edges: tuple[float, ...]
    counts: tuple[int, ...]
    y_ticks: tuple[tuple[float, str], ...]
    x_ticks: tuple[tuple[float, str], ...]
    y_minor: tuple[float, ...] = ()
    x_minor: tuple[float, ...] = ()
    bottom_at: float | None = None

    def rect(self, index: int) -> tuple[float, float, float, float]:
        """One bin's bar as ``(x, y, w, h)``. Its width is the BIN's, which is why the bars touch:
        a histogram's x is continuous, and a gap between two bars would be a range nothing fell in.
        """
        left, right = sorted(
            (
                self.plot.map_x(self.edges[index], self.x_axis.scale),
                self.plot.map_x(self.edges[index + 1], self.x_axis.scale),
            )
        )
        near, far = sorted(
            (
                self.plot.map_y(0.0, self.y_axis.scale),
                self.plot.map_y(self.counts[index], self.y_axis.scale),
            )
        )
        return (left, near, right - left, far - near)

    def datum_point(self, index: int) -> Point:
        """Where a leader lands on one bin: the middle of the end its bar grows to."""
        x, y, w, h = self.rect(index)
        base = self.plot.map_y(0.0, self.y_axis.scale)
        return (x + w / 2.0, y if abs(y + h - base) < abs(y - base) else y + h)


def _histogram_layout(spec: ChartSpec, *, font: str, sizes: Sizes) -> HistogramLayout:
    """Bin the observations, then resolve the two axes and the plot rect around the counts."""
    data = spec.data
    if not isinstance(data, HistogramData):
        raise InvalidArgument("a histogram needs a list of observations")
    axes = spec.axes or _DEFAULT_AXES
    log = axes.scale == "log"
    edges = histogram_edges(data.values, data.bins)
    counts = histogram_counts(data.values, edges)
    y_axis = resolve_axis(
        [float(count) for count in counts],
        lo_pin=axes.y_min,
        hi_pin=axes.y_max,
        zero=not log,
        log=log,
        ticks=axes.ticks,
        fmt=axes.tick_format,
        invert=axes.invert_y,
    )
    # The x axis is the MEASUREMENT, not the bins: its ticks are round numbers off the usual
    # ladder, which is what lets a reader say where a bar sits rather than only how tall it is.
    x_axis = resolve_axis(
        [edges[0], edges[-1]],
        lo_pin=axes.x_min,
        hi_pin=axes.x_max,
        ticks=axes.x_ticks,
        fmt=axes.x_tick_format,
        invert=axes.invert_x,
        axis="x",
    )
    margins = plot_margins(
        y_axis.labels,
        font=font,
        title=bool(spec.title),
        x_label=bool(spec.x_label),
        y_label=bool(spec.y_label),
        sizes=sizes,
        tick_marks=tick_reach(axes.tick_marks or 0.0, axes.tick_direction)[0],
        x_tick_labels=x_axis.labels,
        x_tick_rotate=axes.x_tick_rotate,
        y_tick_rotate=axes.y_tick_rotate,
    )
    plot = _plot_rect(spec.w, spec.h, margins)
    return HistogramLayout(
        plot=plot,
        x_axis=x_axis,
        y_axis=y_axis,
        edges=tuple(edges),
        counts=tuple(counts),
        y_ticks=tuple((plot.map_y(tick, y_axis.scale), text) for tick, text in y_axis.pairs()),
        x_ticks=tuple((plot.map_x(tick, x_axis.scale), text) for tick, text in x_axis.pairs()),
        y_minor=tuple(plot.map_y(tick, y_axis.scale) for tick in _minor_of(axes, y_axis)),
        x_minor=tuple(plot.map_x(tick, x_axis.scale) for tick in _minor_of(axes, x_axis)),
        bottom_at=plot.map_y(0.0, y_axis.scale) if _spine_at_zero(axes, y_axis) else None,
    )


@dataclass(frozen=True, slots=True)
class DonutLayout:
    """A donut's ring: where it is, how big it is, and the angles its slices occupy."""

    plot: Plot
    cx: float
    cy: float
    outer: float
    inner: float
    labels: tuple[str, ...]
    values: tuple[float, ...]
    order: tuple[int, ...]
    angles: tuple[tuple[float, float], ...]

    def middle(self, index: int) -> float:
        """One slice's mid-angle — the direction everything about it hangs off."""
        start, end = self.angles[index]
        return (start + end) / 2.0

    def datum_point(self, index: int) -> Point:
        """Where a leader lands on one slice: the middle of the ring, along its mid-angle."""
        radius = (self.inner + self.outer) / 2.0
        angle = self.middle(index)
        return (self.cx + radius * math.cos(angle), self.cy + radius * math.sin(angle))


def _donut_layout(spec: ChartSpec, *, hole: float, font: str, sizes: Sizes) -> DonutLayout:
    """Resolve a donut's ring: its box, its two radii, its slice order and its angles."""
    data = spec.data
    if not isinstance(data, DonutData):
        raise InvalidArgument("a donut chart needs slice data")
    top = (sizes.title + sizes.gap) if spec.title else 4.0
    plot = Plot(x=0.0, y=top, w=spec.w, h=max(1.0, spec.h - top - 4.0))
    order = order_indices(
        [piece.label for piece in data.slices],
        [piece.value for piece in data.slices],
        data.order,
    )
    pieces = [data.slices[index] for index in order]
    outer = min(plot.w, plot.h) / 2.0
    if data.slice_labels:
        # The labels live OUTSIDE the ring, so the ring gives up the room they need — measured,
        # like every other margin here, rather than guessed at with a fixed inset.
        texts = [_slice_text(piece.label, piece.value, data.value_format) for piece in pieces]
        widest = max(
            (measure_label(text, font, sizes.value_label)[0] for text in texts), default=0.0
        )
        line = measure_label("0", font, sizes.value_label)[1]
        reserve = widest + _SLICE_LEADER + sizes.gap
        outer = max(
            _MIN_DONUT_R,
            min(plot.w / 2.0 - reserve, plot.h / 2.0 - (line + _SLICE_LEADER + sizes.gap)),
        )
    return DonutLayout(
        plot=plot,
        cx=plot.x + plot.w / 2.0,
        cy=plot.y + plot.h / 2.0,
        outer=outer,
        inner=outer * hole,
        labels=tuple(piece.label for piece in pieces),
        values=tuple(piece.value for piece in pieces),
        order=tuple(order),
        angles=tuple(
            donut_angles([piece.value for piece in pieces], data.start_angle)
        ),
    )


def _slice_text(label: str, value: float, fmt: TickFormat | None) -> str:
    """A slice's outside label: what it is, then how much of the whole it is."""
    return f"{label} {format_tick(value, fmt)}".strip()


@dataclass(frozen=True, slots=True)
class RadarLayout:
    """A radar's wheel: where it is, how far it reaches, and where every profile's corners fell."""

    plot: Plot
    cx: float
    cy: float
    radius: float
    r_max: float
    rings: tuple[float, ...]
    labels: tuple[str, ...]
    placed: tuple[tuple[Point, ...], ...]

    @property
    def spokes(self) -> int:
        return len(self.labels)

    def ring(self, value: float) -> list[Point]:
        """One gridline ring, as the n-gon through that value on every spoke.

        A CIRCLE would be the other reading of "concentric ring", and it is the wrong one: the
        data's own polygon has straight edges between spokes, so a circular gridline sits inside
        the profile between the spokes and outside it at them, and the eye reads the gap as a
        reading. An n-gon is parallel to what it is measuring everywhere.
        """
        return radar_points(
            [value] * self.spokes, self.r_max, (self.cx, self.cy), self.radius, self.spokes
        )

    def spoke_end(self, index: int) -> Point:
        """Where one spoke meets the rim."""
        angle = radar_angle(index, self.spokes)
        return (self.cx + self.radius * math.cos(angle), self.cy + self.radius * math.sin(angle))

    def datum_point(self, series: int, index: int) -> Point:
        """Where a leader lands on one reading: that profile's own vertex on that spoke."""
        return self.placed[series][index]


def _radar_layout(spec: ChartSpec, *, font: str, sizes: Sizes) -> RadarLayout:
    """Resolve a radar's wheel: its box, its radius, its radial scale and every vertex.

    The radius is MEASURED down, exactly as the cartesian margins are: the spoke labels live
    outside the rim, so the widest of them (and one line of type, for the ones at the top and
    the bottom) is what the wheel gives up rather than a guessed inset.
    """
    data = spec.data
    if not isinstance(data, RadarData):
        raise InvalidArgument("a radar chart needs one value per named axis")
    top = (sizes.title + sizes.gap) if spec.title else 4.0
    plot = Plot(x=0.0, y=top, w=spec.w, h=max(1.0, spec.h - top - 4.0))
    observed = max((max(entry.values) for entry in data.series), default=0.0)
    r_max = data.r_max if data.r_max is not None else observed
    if r_max <= 0:
        # Every reading zero. A wheel of nothing is still a wheel, and dividing by the nothing
        # would be the only way to fail at drawing it.
        r_max = 1.0
    widest = max((measure_label(text, font, sizes.tick)[0] for text in data.axes), default=0.0)
    line = measure_label("0", font, sizes.tick)[1]
    radius = max(
        _MIN_RADAR_R,
        min(plot.w / 2.0 - (widest + sizes.gap), plot.h / 2.0 - (line + sizes.gap)),
    )
    cx, cy = plot.x + plot.w / 2.0, plot.y + plot.h / 2.0
    return RadarLayout(
        plot=plot,
        cx=cx,
        cy=cy,
        radius=radius,
        r_max=r_max,
        rings=tuple(radar_rings(r_max, data.rings or _RADAR_RINGS)),
        labels=tuple(data.axes),
        placed=tuple(
            tuple(radar_points(entry.values, r_max, (cx, cy), radius, len(data.axes)))
            for entry in data.series
        ),
    )


# --- value labels and reference furniture -------------------------------------


def _draw_references(
    doc: Document,
    group: str,
    theme: str | None,
    *,
    plot: Plot,
    axes: AxesSpec,
    value: Scale,
    value_along_x: bool,
    numeric_x: Scale | None,
    font: str,
    sizes: Sizes,
    bands: bool,
) -> None:
    """Draw the reference lines (``bands=False``) or the reference bands (``bands=True``).

    They are two passes because they belong on two sides of the data: a BAND is a region the data
    is read inside, so it sits behind the marks, and a LINE is a threshold the data is judged
    against, so it has to read over them.
    """
    for reference in axes.reference_lines:
        if (reference.to is not None) != bands:
            continue
        scale = value if reference.axis == "y" else numeric_x
        # A reference on the x axis of a chart whose x is CATEGORICAL has no scale to sit on.
        # It is furniture; furniture nobody can place is left out, not complained about.
        if scale is None:
            continue
        along_x = value_along_x if reference.axis == "y" else True
        extent = reference_extent(reference.value, reference.to, scale)
        if extent is None:
            continue
        at = [
            (plot.map_x(edge, scale) if along_x else plot.map_y(edge, scale)) for edge in extent
        ]
        near, far = min(at), max(at)
        classes = _hooks(doc, theme, reference.kind)
        if bands:
            _part(
                doc,
                (
                    inkex.Rectangle.new(near, plot.y, far - near, plot.h)
                    if along_x
                    else inkex.Rectangle.new(plot.x, near, plot.w, far - near)
                ),
                prefix="rect",
                category="shape",
                parent=group,
                style=None,
                classes=[*classes, *_hooks(doc, theme, "reference-band")],
            )
        elif along_x:
            _line(doc, group, (near, plot.y), (near, plot.y + plot.h), classes)
        else:
            _line(doc, group, (plot.x, near), (plot.x + plot.w, near), classes)
        if reference.label:
            # Right-aligned at the far end of the thing it names: a threshold's label belongs
            # where the eye leaves the line, not where it picks it up.
            anchor = (
                (far - _REF_LABEL_PAD, plot.y + sizes.reference_label)
                if along_x
                else (plot.x + plot.w - _REF_LABEL_PAD, near - _REF_LABEL_PAD)
            )
            _text(
                doc,
                group,
                reference.label,
                anchor,
                anchor="end",
                baseline="alphabetic",
                classes=_hooks(doc, theme, "reference-label"),
            )


# --- the five plots ----------------------------------------------------------


def _draw_bar(
    doc: Document, group: str, theme: str | None, spec: ChartSpec, font: str, sizes: Sizes
) -> Plot:
    data = spec.data
    if not isinstance(data, BarData):
        raise InvalidArgument("a bar chart needs bar data")
    layout = _bar_layout(spec, font=font, sizes=sizes)
    plot, y_axis = layout.plot, layout.axis
    axes = spec.axes or _DEFAULT_AXES
    _draw_frame(
        doc,
        group,
        theme,
        plot=plot,
        spec=spec,
        y_ticks=layout.y_ticks,
        x_ticks=layout.x_ticks,
        font=font,
        axes=layout.frame_axes,
        sizes=sizes,
        y_minor=layout.y_minor,
        x_minor=layout.x_minor,
        bottom_at=layout.bottom_at,
        left_at=layout.left_at,
    )
    _draw_references(
        doc,
        group,
        theme,
        plot=plot,
        axes=axes,
        value=y_axis.scale,
        value_along_x=layout.horizontal,
        numeric_x=None,
        font=font,
        sizes=sizes,
        bands=True,
    )

    clip = _clip_to_plot(doc, group, plot) if y_axis.pinned else None
    # The net-total bar is the last "category" and is NOT one of the steps: it wears the second
    # series colour, because "where this ended up" is a different statement from "what moved it".
    total_slot = len(layout.categories) - 1 if layout.waterfall and data.total_label else None

    def _bars(
        parent: str, *, geom: int, paint: int, only: int | None = None, skip: int | None = None
    ) -> None:
        classes = _series_class(doc, theme, paint)
        fill = _fill_of(_hatch(doc, group, classes) if data.hatch else None)
        for category in range(len(layout.categories)):
            if category == skip or (only is not None and category != only):
                continue
            x, y, w, h = layout.rect(category, geom)
            _part(
                doc,
                inkex.Rectangle.new(x, y, w, h),
                prefix="rect",
                category="shape",
                parent=parent,
                # A mark that is a FILL takes no stroke: the series class sets both, and the
                # stroke would make every bar a hairline wider than the number it stands for.
                style={"stroke": "none", **fill},
                classes=(),
            )

    def _series_group(slot: int) -> str:
        series = _group(doc, group)
        for class_name in _series_class(doc, theme, slot):
            series.set("class", class_name)
        if clip is not None:
            series.set("clip-path", f"url(#{clip})")
        return str(series.get_id())

    for index in range(len(data.series)):
        _bars(_series_group(index), geom=index, paint=index, skip=total_slot)
    if total_slot is not None:
        _bars(_series_group(1), geom=0, paint=1, only=total_slot)
    if layout.waterfall:
        _draw_waterfall_connectors(doc, group, theme, layout, clip=clip)
    _draw_references(
        doc,
        group,
        theme,
        plot=plot,
        axes=axes,
        value=y_axis.scale,
        value_along_x=layout.horizontal,
        numeric_x=None,
        font=font,
        sizes=sizes,
        bands=False,
    )
    if data.value_labels or (data.stacked and data.stack_total_labels):
        _draw_bar_labels(doc, group, theme, data, layout, font=font, sizes=sizes)
    return plot


def _draw_waterfall_connectors(
    doc: Document,
    group: str,
    theme: str | None,
    layout: BarLayout,
    *,
    clip: str | None,
) -> None:
    """The dashed rules joining each bar's end to the next bar's start.

    A waterfall's bars float, so nothing in the picture says that the top of one IS the bottom of
    the next — the connectors are what turn a row of detached rectangles back into one running
    total. They are drawn from the running sum after each step, which is why the last one lands on
    the total bar's far end rather than on its base.
    """
    values = layout.values[0]
    steps = len(layout.categories) - 1
    running = 0.0
    classes = _hooks(doc, theme, "waterfall-connector")
    joined = _group(doc, group)
    joined_id = str(joined.get_id())
    if clip is not None:
        joined.set("clip-path", f"url(#{clip})")
    along = layout.plot.y if layout.horizontal else layout.plot.x
    for index in range(steps):
        running += values[index]
        at = layout.map_value(running)
        band, following = layout.band(index, 0), layout.band(index + 1, 0)
        _line(
            doc,
            joined_id,
            layout.across(at, along + band.x + band.w),
            layout.across(at, along + following.x),
            classes,
        )


def _minor_of(axes: AxesSpec, axis: Axis) -> list[float]:
    """The minor tick positions one axis asks for, in data units — none unless ``minor`` is set."""
    if axes.minor is None:
        return []
    return minor_ticks(axis.ticks, axes.minor, log=axis.scale.log)


def _spine_at_zero(axes: AxesSpec, axis: Axis) -> bool:
    """Whether the category axis line moves to value zero: only if asked, and only if 0 is there.

    A log axis never qualifies — it has no zero — and neither does a range that does not contain
    zero, where the "spine" would be a line drawn outside the plot it belongs to.
    """
    if not axes.zero_spine or axis.scale.log:
        return False
    lo, hi = min(axis.scale.lo, axis.scale.hi), max(axis.scale.lo, axis.scale.hi)
    return lo <= 0.0 <= hi


def _draw_bar_labels(
    doc: Document,
    group: str,
    theme: str | None,
    data: BarData,
    layout: BarLayout,
    *,
    font: str,
    sizes: Sizes,
) -> None:
    """The numbers on the bars: one per bar (or per segment), plus the stack totals.

    A plain bar's label sits just outside the end it grows to and flips inside when the plot's
    edge is too close for it (see :func:`label_centre`). A STACKED segment has no free end, so its
    label goes in the middle of the segment — and is left off entirely when the segment is too
    short to hold the text, because a number overflowing its own segment reads as the next one's.
    """
    fmt = data.value_format or layout.frame_axes.tick_format
    classes = _hooks(doc, theme, "value-label")
    # Its own group, OUTSIDE the clip the series wear: a caption cut in half by the window the
    # data is clipped to is worse than no caption.
    parent = str(_group(doc, group).get_id())
    lo, hi = layout.bounds()
    line = measure_label("0", font, sizes.value_label)[1]

    def _extent(text: str) -> float:
        """How much room the label needs ALONG the value direction."""
        return measure_label(text, font, sizes.value_label)[0] if layout.horizontal else line

    def _write(text: str, at: float, along: float) -> None:
        _text(
            doc,
            parent,
            text,
            layout.across(at, along),
            anchor="middle",
            baseline="central",
            classes=classes,
        )

    for category in range(len(layout.categories)):
        # Where the total will land, worked out BEFORE the segments so a total that flipped
        # inside the stack (its end is against the plot edge) can silence the segment label it
        # would otherwise sit on top of — two numbers in one place read as neither.
        total_at: float | None = None
        total_room = 0.0
        if layout.stacked and data.stack_total_labels:
            column = [row[category] for row in layout.values]
            total_text = format_tick(sum(column), fmt)
            total_room = _extent(total_text)
            down, up = stack_extents(column)
            total_at = label_centre(
                layout.map_value(up if sum(column) >= 0 else down),
                layout.outward(sum(column)),
                gap=sizes.gap,
                extent=total_room,
                lo=lo,
                hi=hi,
            )
        for index in range(len(layout.values)) if data.value_labels else ():
            value = layout.values[index][category]
            text = format_tick(value, fmt)
            along = layout.centre(layout.band(category, index))
            if layout.stacked:
                piece = layout.segments[category][index]
                room = abs(layout.map_value(piece.hi) - layout.map_value(piece.lo))
                if room < _extent(text):
                    continue
                middle = layout.map_value(piece.middle)
                if total_at is not None and abs(middle - total_at) < (
                    _extent(text) + total_room
                ) / 2.0 + sizes.gap / 2.0:
                    continue
                _write(text, middle, along)
                continue
            low, high = layout.span(category, index)
            end = layout.map_value(high if value >= layout.origin else low)
            at = label_centre(
                end, layout.outward(value), gap=sizes.gap, extent=_extent(text), lo=lo, hi=hi
            )
            _write(text, at, along)
        if total_at is None:
            continue
        column = [row[category] for row in layout.values]
        _write(format_tick(sum(column), fmt), total_at, layout.centre(layout.band(category, 0)))


def _point_span(series: Sequence[PointSeries]) -> tuple[tuple[float, float], tuple[float, float]]:
    xs = [point[0] for entry in series for point in entry.points]
    ys = [point[1] for entry in series for point in entry.points]
    if not xs:
        raise InvalidArgument("this chart's series carry no points to plot")
    return (min(xs), max(xs)), (min(ys), max(ys))


def _draw_points(
    doc: Document, group: str, theme: str | None, spec: ChartSpec, font: str, sizes: Sizes
) -> Plot:
    """Line and scatter — the same axes, differing only in what each series draws."""
    data = spec.data
    if not isinstance(data, LineData | ScatterData):
        raise InvalidArgument("a line or scatter chart needs point-series data")
    axes = spec.axes or _DEFAULT_AXES
    log = axes.scale == "log"
    area = isinstance(data, LineData) and data.area
    hatch = isinstance(data, LineData) and data.hatch
    step = data.step if isinstance(data, LineData) else "none"
    markers = (isinstance(data, ScatterData) or data.points) and data.marker != "none"
    layout = _points_layout(spec, font=font, sizes=sizes)
    plot, x_axis, y_axis = layout.plot, layout.x_axis, layout.y_axis
    _draw_frame(
        doc,
        group,
        theme,
        plot=plot,
        spec=spec,
        y_ticks=layout.y_ticks,
        x_ticks=layout.x_ticks,
        font=font,
        axes=axes,
        sizes=sizes,
        y_minor=layout.y_minor,
        x_minor=layout.x_minor,
        bottom_at=layout.bottom_at,
    )
    _draw_references(
        doc,
        group,
        theme,
        plot=plot,
        axes=axes,
        value=y_axis.scale,
        value_along_x=False,
        numeric_x=x_axis.scale,
        font=font,
        sizes=sizes,
        bands=True,
    )

    clip = _clip_to_plot(doc, group, plot) if y_axis.pinned or x_axis.pinned else None
    stride = data.markevery or 1 if isinstance(data, LineData) else 1
    if isinstance(data, LineData) and data.bands:
        _draw_series_bands(doc, group, theme, data, layout, step=step, clip=clip)
    for index in range(len(data.series)):
        series = _group(doc, group)
        series_id = str(series.get_id())
        classes = _series_class(doc, theme, index)
        for class_name in classes:
            series.set("class", class_name)
        if clip is not None:
            series.set("clip-path", f"url(#{clip})")
        placed = list(layout.placed[index])
        # The stepped outline is what the LINE and the wash under it follow; the marks stay on
        # the real data, because the corners of a staircase are not readings.
        outline = placed if step == "none" else step_points(placed, step)
        if area and outline:
            # The wash runs down to zero — or, on a log axis, to the bottom of the axis.
            base = plot.map_y(y_axis.scale.lo if log else 0.0, y_axis.scale)
            steps = " ".join(f"L {_num(px)} {_num(py)}" for px, py in outline)
            fill = _fill_of(_hatch(doc, group, classes) if hatch else None)
            # Hatching IS the texture, so it keeps its full opacity; a flat wash is diluted to
            # stay behind the line it belongs to.
            wash: Style = {} if fill else {"fill-opacity": _AREA_OPACITY}
            _part(
                doc,
                inkex.PathElement.new(
                    f"M {_num(outline[0][0])} {_num(base)} {steps} "
                    f"L {_num(outline[-1][0])} {_num(base)} Z"
                ),
                prefix="path",
                category="shape",
                parent=series_id,
                style={"stroke": "none", **wash, **fill},
                classes=(),
            )
        if isinstance(data, LineData) and len(outline) > 1:
            _polyline(doc, series_id, outline, ())
        if markers:
            for where, at in enumerate(placed):
                if where % stride:
                    continue
                _draw_mark(
                    doc,
                    series_id,
                    data.marker,
                    at,
                    layout.radius(index, where),
                    unfilled=data.open,
                    classes=classes,
                )
    _draw_references(
        doc,
        group,
        theme,
        plot=plot,
        axes=axes,
        value=y_axis.scale,
        value_along_x=False,
        numeric_x=x_axis.scale,
        font=font,
        sizes=sizes,
        bands=False,
    )
    if data.value_labels:
        _draw_point_labels(
            doc,
            group,
            theme,
            data,
            layout,
            fmt=data.value_format or axes.tick_format,
            font=font,
            sizes=sizes,
            # A label clears the mark it names — which on a bubble chart is a different distance
            # at every point, and no distance at all where no mark was drawn.
            radii=tuple(
                tuple(
                    layout.radius(index, where) if markers and where % stride == 0 else 0.0
                    for where in range(len(row))
                )
                for index, row in enumerate(layout.placed)
            ),
        )
    return plot


def _draw_series_bands(
    doc: Document,
    group: str,
    theme: str | None,
    data: LineData,
    layout: PointLayout,
    *,
    step: Literal["none", "pre", "post", "mid"],
    clip: str | None,
) -> None:
    """The filled regions between named pairs of series, drawn BEHIND every line.

    The two series are known to share an x sequence (:func:`_validate_bands` refuses anything
    else), so the region is just the one polyline out and the other back. It wears the FIRST named
    series' class at a light fill-opacity: a band is that series' range, and painting it in a
    colour of its own would put a third series on a chart that has two.

    A label goes at the band's WIDEST point rather than through the annotation module's placement
    scoring: the widest gap is the one place inside a band that is guaranteed to have room for
    text, and it is also the place a reader is already looking.
    """
    slots = {entry.name: index for index, entry in enumerate(data.series)}
    parent = _group(doc, group)
    parent_id = str(parent.get_id())
    if clip is not None:
        parent.set("clip-path", f"url(#{clip})")
    for band in data.bands or ():
        first, second = (slots[name] for name in band.between)
        low = list(layout.placed[first])
        high = list(layout.placed[second])
        if len(low) < 2:
            continue
        out = low if step == "none" else step_points(low, step)
        back = high if step == "none" else step_points(high, step)
        outline = [*out, *reversed(back)]
        steps = " L ".join(f"{_num(px)} {_num(py)}" for px, py in outline)
        _part(
            doc,
            inkex.PathElement.new(f"M {steps} Z"),
            prefix="path",
            category="shape",
            parent=parent_id,
            style={"stroke": "none", "fill-opacity": _AREA_OPACITY},
            classes=_series_class(doc, theme, first),
        )
        if band.label:
            where = widest_gap(low, high)
            _text(
                doc,
                parent_id,
                band.label,
                (low[where][0], (low[where][1] + high[where][1]) / 2.0),
                anchor="middle",
                baseline="central",
                classes=_hooks(doc, theme, "value-label"),
            )


def _draw_point_labels(
    doc: Document,
    group: str,
    theme: str | None,
    data: LineData | ScatterData,
    layout: PointLayout,
    *,
    fmt: TickFormat | None,
    font: str,
    sizes: Sizes,
    radii: Sequence[Sequence[float]] = (),
) -> None:
    """Each point's y value, written above its mark. Outside the clip, like every other caption.

    "Above" flips to "below" for a point sitting at the top of the plot, by the same rule a bar's
    label follows — and the topmost point of an unpinned axis is ALWAYS at the top of the plot,
    so without the flip the one number a reader looks for first is the one written over the title.

    The gap is measured from the EDGE of the mark, not from its centre: on a bubble chart every
    mark is a different size, and a label placed a fixed distance from the centre would sit on top
    of the big ones. A point with no mark drawn on it clears nothing, which is the old behaviour.
    """
    parent = str(_group(doc, group).get_id())
    classes = _hooks(doc, theme, "value-label")
    plot = layout.plot
    line = measure_label("0", font, sizes.value_label)[1]
    for index, entry in enumerate(data.series):
        row = radii[index] if index < len(radii) else ()
        for where, ((_, py), (x, y)) in enumerate(
            zip(entry.points, layout.placed[index], strict=True)
        ):
            _text(
                doc,
                parent,
                format_tick(py, fmt),
                (
                    x,
                    label_centre(
                        y,
                        -1.0,
                        gap=sizes.gap + (row[where] if where < len(row) else 0.0),
                        extent=line,
                        lo=plot.y,
                        hi=plot.y + plot.h,
                    ),
                ),
                anchor="middle",
                baseline="central",
                classes=classes,
            )


def _draw_histogram(
    doc: Document, group: str, theme: str | None, spec: ChartSpec, font: str, sizes: Sizes
) -> Plot:
    """Contiguous bars over a numeric x — the one plot here that counts its own data.

    It borrows the bar chart's paint (one series group, ``stroke: none`` on a mark that is a fill,
    the same hatch) and none of its LAYOUT: a bar chart's bands divide a categorical axis into
    equal slots, where a histogram's bars are as wide as the bins are, edge to edge.
    """
    data = spec.data
    if not isinstance(data, HistogramData):
        raise InvalidArgument("a histogram needs a list of observations")
    axes = spec.axes or _DEFAULT_AXES
    layout = _histogram_layout(spec, font=font, sizes=sizes)
    plot = layout.plot
    _draw_frame(
        doc,
        group,
        theme,
        plot=plot,
        spec=spec,
        y_ticks=layout.y_ticks,
        x_ticks=layout.x_ticks,
        font=font,
        axes=axes,
        sizes=sizes,
        y_minor=layout.y_minor,
        x_minor=layout.x_minor,
        bottom_at=layout.bottom_at,
    )
    _draw_references(
        doc,
        group,
        theme,
        plot=plot,
        axes=axes,
        value=layout.y_axis.scale,
        value_along_x=False,
        numeric_x=layout.x_axis.scale,
        font=font,
        sizes=sizes,
        bands=True,
    )
    series = _group(doc, group)
    series_id = str(series.get_id())
    classes = _series_class(doc, theme, 0)
    for class_name in classes:
        series.set("class", class_name)
    if layout.y_axis.pinned or layout.x_axis.pinned:
        series.set("clip-path", f"url(#{_clip_to_plot(doc, group, plot)})")
    fill = _fill_of(_hatch(doc, group, classes) if data.hatch else None)
    for index in range(len(layout.counts)):
        x, y, w, h = layout.rect(index)
        _part(
            doc,
            inkex.Rectangle.new(x, y, w, h),
            prefix="rect",
            category="shape",
            parent=series_id,
            style={"stroke": "none", **fill},
            classes=(),
        )
    _draw_references(
        doc,
        group,
        theme,
        plot=plot,
        axes=axes,
        value=layout.y_axis.scale,
        value_along_x=False,
        numeric_x=layout.x_axis.scale,
        font=font,
        sizes=sizes,
        bands=False,
    )
    if data.value_labels:
        _draw_histogram_labels(
            doc,
            group,
            theme,
            layout,
            fmt=data.value_format or axes.tick_format,
            font=font,
            sizes=sizes,
        )
    return plot


def _draw_histogram_labels(
    doc: Document,
    group: str,
    theme: str | None,
    layout: HistogramLayout,
    *,
    fmt: TickFormat | None,
    font: str,
    sizes: Sizes,
) -> None:
    """Each bin's count, just beyond the end of its bar — flipping inside at the plot's edge."""
    parent = str(_group(doc, group).get_id())
    classes = _hooks(doc, theme, "value-label")
    line = measure_label("0", font, sizes.value_label)[1]
    base = layout.plot.map_y(0.0, layout.y_axis.scale)
    for index, count in enumerate(layout.counts):
        x, y, w, h = layout.rect(index)
        end = y if abs(y - base) > abs(y + h - base) else y + h
        _text(
            doc,
            parent,
            format_tick(float(count), fmt),
            (
                x + w / 2.0,
                label_centre(
                    end,
                    -1.0 if end <= base else 1.0,
                    gap=sizes.gap,
                    extent=line,
                    lo=layout.plot.y,
                    hi=layout.plot.y + layout.plot.h,
                ),
            ),
            anchor="middle",
            baseline="central",
            classes=classes,
        )


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
    font: str,
    sizes: Sizes,
) -> None:
    data = spec.data
    if not isinstance(data, DonutData):
        raise InvalidArgument("a donut chart needs slice data")
    layout = _donut_layout(spec, hole=hole, font=font, sizes=sizes)
    cx, cy, outer, inner = layout.cx, layout.cy, layout.outer, layout.inner
    for index, (label, value) in enumerate(zip(layout.labels, layout.values, strict=True)):
        start, end = layout.angles[index]
        wedge = _group(doc, group)
        wedge_id = str(wedge.get_id())
        classes = _series_class(doc, theme, index)
        for class_name in classes:
            wedge.set("class", class_name)
        wedge.set("data-slice", label)
        fill = _fill_of(_hatch(doc, group, classes) if data.hatch else None)
        _part(
            doc,
            inkex.PathElement.new(annular_sector(cx, cy, outer, inner, start, end)),
            prefix="path",
            category="shape",
            parent=wedge_id,
            style={"stroke": "none", **fill},
            classes=(),
        )
        if data.slice_labels:
            _draw_slice_label(
                doc, wedge_id, theme, layout, index, value=value, fmt=data.value_format,
                sizes=sizes,
            )
    _draw_donut_centre(doc, group, theme, layout, data, sizes=sizes, font=font)
    _draw_title(doc, group, theme, layout.plot, spec.title, sizes)


def _draw_slice_label(
    doc: Document,
    parent: str,
    theme: str | None,
    layout: DonutLayout,
    index: int,
    *,
    value: float,
    fmt: TickFormat | None,
    sizes: Sizes,
) -> None:
    """One slice's name and value, outside the ring on its own mid-angle.

    A THIN slice gets a short radial stub first: at the rim, two labels a couple of degrees apart
    are indistinguishable, and the stub is what says which of them belongs to which wedge. It is
    a plain radial line rather than the annotate module's leader machinery — that module imports
    THIS one, so reaching back into it would be a cycle for six pixels of ink.
    """
    start, end = layout.angles[index]
    angle = layout.middle(index)
    cos, sin = math.cos(angle), math.sin(angle)
    reach = layout.outer
    if end - start < _THIN_SLICE:
        _line(
            doc,
            parent,
            (layout.cx + cos * reach, layout.cy + sin * reach),
            (layout.cx + cos * (reach + _SLICE_LEADER), layout.cy + sin * (reach + _SLICE_LEADER)),
            _series_class(doc, theme, index),
        )
        reach += _SLICE_LEADER
    reach += sizes.gap / 2.0
    _text(
        doc,
        parent,
        _slice_text(layout.labels[index], value, fmt),
        (layout.cx + cos * reach, layout.cy + sin * reach),
        anchor="start" if cos >= 0 else "end",
        baseline="central",
        classes=_hooks(doc, theme, "value-label"),
    )


def _draw_donut_centre(
    doc: Document,
    group: str,
    theme: str | None,
    layout: DonutLayout,
    data: DonutData,
    *,
    sizes: Sizes,
    font: str,
) -> None:
    """The KPI in the hole: a big number, and a caption under it. Both centred on the ring.

    The two are stacked and the STACK is centred, so one line alone sits on the middle of the
    hole and two lines straddle it — which is what keeps the number optically in the doughnut.
    """
    if not (data.center_text or data.center_subtext):
        return
    big = measure_label(data.center_text, font, sizes.donut_center)[1] if data.center_text else 0.0
    small = (
        measure_label(data.center_subtext, font, sizes.donut_subtext)[1]
        if data.center_subtext
        else 0.0
    )
    top = layout.cy - (big + small) / 2.0
    _text(
        doc,
        group,
        data.center_text,
        (layout.cx, top + big / 2.0),
        anchor="middle",
        baseline="central",
        classes=_hooks(doc, theme, "donut-center"),
    )
    _text(
        doc,
        group,
        data.center_subtext,
        (layout.cx, top + big + small / 2.0),
        anchor="middle",
        baseline="central",
        classes=_hooks(doc, theme, "donut-subtext"),
    )


def _draw_radar_frame(
    doc: Document,
    group: str,
    theme: str | None,
    layout: RadarLayout,
    data: RadarData,
    *,
    font: str,
    sizes: Sizes,
) -> None:
    """The wheel: the rings, the spokes, the ruler up the 12 o'clock spoke, the spoke names.

    Rings first and spokes over them, for the reason ``_draw_frame`` draws its gridlines before
    its axes: a spoke is the axis a reading is taken along, and it has to read as a line rather
    than as wherever the rings happen to cross.
    """
    frame = _group(doc, group)
    frame_id = str(frame.get_id())
    grid = _hooks(doc, theme, "gridline")
    for value in layout.rings:
        _part(
            doc,
            inkex.Polygon.new(_points_str(layout.ring(value))),
            prefix="polygon",
            category="shape",
            parent=frame_id,
            style={"fill": "none"},
            classes=grid,
        )
    axis = _hooks(doc, theme, "axis")
    for index in range(layout.spokes):
        _line(doc, frame_id, (layout.cx, layout.cy), layout.spoke_end(index), axis)

    tick_class = _hooks(doc, theme, "tick-label")
    line = measure_label("0", font, sizes.tick)[1]
    if data.ring_labels:
        # One ruler, on one spoke. A value at every ring on every spoke would be n copies of the
        # same ladder, and the wheel would be unreadable through its own numbers.
        for value in layout.rings:
            _text(
                doc,
                frame_id,
                format_tick(value, data.value_format),
                (layout.cx - _RING_LABEL_PAD, layout.cy - layout.radius * value / layout.r_max),
                anchor="end",
                baseline="central",
                classes=tick_class,
            )
    for index, name in enumerate(layout.labels):
        angle = radar_angle(index, layout.spokes)
        cos, sin = math.cos(angle), math.sin(angle)
        # Half a line further out where the spoke points up or down: the text is centred on its
        # own middle, so that is what keeps a label at 12 o'clock off the rim it labels.
        reach = layout.radius + sizes.gap + abs(sin) * line / 2.0
        _text(
            doc,
            frame_id,
            name,
            (layout.cx + cos * reach, layout.cy + sin * reach),
            anchor=radar_anchor(angle),
            baseline="central",
            classes=tick_class,
        )


def _draw_radar(
    doc: Document, group: str, theme: str | None, spec: ChartSpec, font: str, sizes: Sizes
) -> Plot:
    """One closed polygon per series on a wheel of named axes.

    The three passes are the whole of the drawing order and they are not interchangeable: every
    wash first, then every outline, then every mark. A wash drawn with its own outline would sit
    over the profile drawn before it — translucent, so not hiding it, but tinting it into a
    colour neither series wears, which is exactly the reading a radar is there to support going
    wrong. Marks last for the same reason a level up: a vertex is a reading, and no other
    series' area may wash over it.
    """
    data = spec.data
    if not isinstance(data, RadarData):
        raise InvalidArgument("a radar chart needs one value per named axis")
    layout = _radar_layout(spec, font=font, sizes=sizes)
    _draw_radar_frame(doc, group, theme, layout, data, font=font, sizes=sizes)

    def _profile(index: int, style: Style) -> None:
        holder = _group(doc, group)
        classes = _series_class(doc, theme, index)
        for class_name in classes:
            holder.set("class", class_name)
        _part(
            doc,
            inkex.Polygon.new(_points_str(list(layout.placed[index]))),
            prefix="polygon",
            category="shape",
            parent=str(holder.get_id()),
            style=style,
            classes=(),
        )

    if data.fill:
        for index in range(len(layout.placed)):
            _profile(index, {"stroke": "none", "fill-opacity": _AREA_OPACITY})
    for index in range(len(layout.placed)):
        _profile(index, {"fill": "none"})
    if data.marker != "none":
        for index, points in enumerate(layout.placed):
            marks = _group(doc, group)
            classes = _series_class(doc, theme, index)
            for class_name in classes:
                marks.set("class", class_name)
            for at in points:
                _draw_mark(
                    doc,
                    str(marks.get_id()),
                    data.marker,
                    at,
                    _MARKER_R,
                    unfilled=data.open,
                    classes=classes,
                )
    return layout.plot


def sparkline_y(value: float, lo: float, hi: float, height: float, inset: float = 1.0) -> float:
    """Where one value sits down a sparkline's box — pure, so the line and its dots agree."""
    span = hi - lo
    usable = max(1.0, height - 2 * inset)
    if abs(span) <= _EPS:
        return height / 2.0
    return inset + usable - (value - lo) / span * usable


def _draw_sparkline(
    doc: Document, group: str, theme: str | None, spec: ChartSpec, ink: str
) -> None:
    data = spec.data
    if not isinstance(data, SparklineData):
        raise InvalidArgument("a sparkline needs a plain list of values")
    lo, hi = min(data.values), max(data.values)
    step = spec.w / max(1, len(data.values) - 1)
    points = [
        (index * step, sparkline_y(value, lo, hi, spec.h))
        for index, value in enumerate(data.values)
    ]
    classes = _series_class(doc, theme, 0)
    # A sparkline is one line: if the theme offers no series colour there is nothing above it to
    # inherit a stroke from, so it takes the ink rather than rendering as nothing at all.
    style: Style = {"fill": "none"} if classes else {"fill": "none", "stroke": ink}
    if data.baseline is not None:
        # Drawn FIRST, and as a gridline rather than a series mark: it is the thing the trend is
        # being read against, so it has to sit behind the trend and read as scenery.
        at = sparkline_y(data.baseline, lo, hi, spec.h)
        _line(doc, group, (0.0, at), (spec.w, at), _hooks(doc, theme, "gridline"))
    _part(
        doc,
        inkex.Polyline.new(_points_str(points)),
        prefix="polyline",
        category="connector",
        parent=group,
        style=style,
        classes=classes,
    )
    for index in _flourishes(data):
        dot: Style = {"stroke": "none"} if classes else {"stroke": "none", "fill": ink}
        _part(
            doc,
            inkex.Circle.new(points[index], _SPARK_DOT_R),
            prefix="circle",
            category="shape",
            parent=group,
            style=dot,
            classes=classes,
        )


def _flourishes(data: SparklineData) -> list[int]:
    """Which points get a dot, in drawing order and each one only once."""
    marked: list[int] = []
    if data.extremes:
        marked.extend((data.values.index(min(data.values)), data.values.index(max(data.values))))
    if data.last_point:
        marked.append(len(data.values) - 1)
    return sorted(dict.fromkeys(marked))


# --- the facade --------------------------------------------------------------


def _hole(theme: ServingTheme) -> float:
    value = _token(theme, "--donut-hole", _DONUT_HOLE)
    return value if 0.0 <= value < 1.0 else _DONUT_HOLE


def _series_slot(spec: ChartSpec, datum: ChartDatum) -> int | None:
    """Which series a datum names — by index, or by the name that series carries."""
    data = spec.data
    if isinstance(data, BarData | LineData | ScatterData | RadarData):
        names = [entry.name for entry in data.series]
    else:  # a donut and a sparkline are one series by construction
        names = [""]
    if isinstance(datum.series, int):
        return datum.series if 0 <= datum.series < len(names) else None
    return names.index(datum.series) if datum.series in names else None


def _datum_local(
    spec: ChartSpec, datum: ChartDatum, *, hole: float, font: str, sizes: Sizes
) -> Point | None:
    """A datum's position in the chart group's OWN frame, or None if there is no such datum."""
    slot = _series_slot(spec, datum)
    if slot is None:
        return None
    data = spec.data
    if isinstance(data, BarData):
        bars = _bar_layout(spec, font=font, sizes=sizes)
        if not 0 <= datum.index < len(bars.categories):
            return None
        # `index` names the datum as the CALLER wrote it, so an `order` that moved the category
        # somewhere else on the page is looked through rather than round.
        return bars.datum_point(bars.order.index(datum.index), slot)
    if isinstance(data, LineData | ScatterData):
        points = _points_layout(spec, font=font, sizes=sizes)
        row = points.placed[slot]
        return row[datum.index] if 0 <= datum.index < len(row) else None
    if isinstance(data, DonutData):
        ring = _donut_layout(spec, hole=hole, font=font, sizes=sizes)
        if not 0 <= datum.index < len(ring.labels):
            return None
        return ring.datum_point(ring.order.index(datum.index))
    if isinstance(data, RadarData):
        web = _radar_layout(spec, font=font, sizes=sizes)
        # `index` names the AXIS, and the axes are drawn in the order they were given — a radar
        # has no `order` to look through, because the shape IS the order.
        if not 0 <= datum.index < web.spokes:
            return None
        return web.datum_point(slot, datum.index)
    if isinstance(data, HistogramData):
        bins = _histogram_layout(spec, font=font, sizes=sizes)
        # `index` names the BIN, which is the only thing a histogram has to point at — the
        # observations themselves were counted, not placed.
        if not 0 <= datum.index < len(bins.counts):
            return None
        return bins.datum_point(datum.index)
    if not 0 <= datum.index < len(data.values):
        return None
    step = spec.w / max(1, len(data.values) - 1)
    lo, hi = min(data.values), max(data.values)
    return (datum.index * step, sparkline_y(data.values[datum.index], lo, hi, spec.h))


def datum_anchor(doc: Document, chart: BaseElement, datum: ChartDatum) -> Point | None:
    """Where one datum of an existing chart sits, in WORLD coordinates.

    None means there is no such datum — an index past the end of the series, a series name
    nothing carries, or a group that is not a chart at all. The geometry is RE-DERIVED from the
    chart's spec through the same layout that drew it, never read back off the marks, so an
    annotation anchored here survives an ``edit_chart`` that moves every mark somewhere else.
    """
    spec = read_chart_spec(chart)
    if spec is None:
        return None
    theme = serving_theme(doc, "chart")
    local = _datum_local(
        spec, datum, hole=_hole(theme), font=_label_font(theme), sizes=chart_sizes(theme)
    )
    return None if local is None else _to_world_point(chart, local)


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
    _purge_owned_defs(doc, str(group.get_id()))
    theme = serving_theme(doc, "chart")
    font = _label_font(theme)
    sizes = chart_sizes(theme)
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
        _draw_donut(doc, group_id, dressing, spec, _hole(theme), font, sizes)
        return
    if spec.kind == "bar":
        plot = _draw_bar(doc, group_id, dressing, spec, font, sizes)
    elif spec.kind == "histogram":
        plot = _draw_histogram(doc, group_id, dressing, spec, font, sizes)
    elif spec.kind == "radar":
        # A radar has no cartesian frame, but it does hang a title over its box like the rest.
        plot = _draw_radar(doc, group_id, dressing, spec, font, sizes)
    else:
        plot = _draw_points(doc, group_id, dressing, spec, font, sizes)
    _draw_title(doc, group_id, dressing, plot, spec.title, sizes)


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
    axes: AxesSpec | None = None,
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

    ``axes`` is how a caller overrules any of that — pinned limits, a tick count or an explicit
    tick list, a formatting style, gridlines, tick marks, a log scale. Omitting it draws exactly
    what this module drew before axes existed; see :class:`AxesSpec`.

    A ``sparkline`` ignores ``title``/``x_label``/``y_label``/``axes`` and a ``donut`` ignores
    those too — they have no axes for them to name, and drawing them anyway would be a lie about
    what the picture shows. A ``radar`` keeps its ``title`` and ignores the rest for the same
    reason: its frame is polar, and the two controls it does have (``rings``, ``r_max``) live on
    its own data model, where the donut's hole and the histogram's bins live — a per-chart frame
    choice, not a cartesian axis anyone could pin.
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
        axes=axes,
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
    axes: AxesSpec | None = None,
) -> ChartEdit:
    """Edit a chart by its SPEC — new data, new labels, new axes, a new box — and re-derive it.

    The children are thrown away and rebuilt rather than patched: a chart's geometry is a pure
    function of its data and its box, deriving it costs nothing, and nothing outside a chart may
    reference its internals (they carry no stable identity — the spec does). The GROUP survives
    untouched, so its id, its classes and its position all mean what they meant before.

    ``data`` is re-validated against the chart's own kind: an edit cannot turn a bar into a donut.
    ``axes`` REPLACES the chart's axes wholesale (it is one settled description of the frame, not
    a bag of independent knobs); omitting it keeps whatever the chart already had.
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
        axes=axes if axes is not None else current.axes,
    )
    _write_chart_spec(group, spec)
    _build(doc, group, spec)
    return ChartEdit(
        ref=NodeRef(id=str(group.get_id()), tag=str(group.TAG), name=getattr(group, "label", None)),
        children=sum(1 for child in group if isinstance(child.tag, str)),
    )
