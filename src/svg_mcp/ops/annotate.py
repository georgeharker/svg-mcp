"""Annotation facades: a legend that reads the document, and a callout that points at a node.

Both are facades in exactly the sense :mod:`~svg_mcp.ops.diagram` means it — a ``<g>`` that draws
itself from a spec stored on it in a ``data-*`` attribute — and both exist because the two things
a finished picture always needs are the ones an agent is worst at maintaining by hand: a key that
still matches what is drawn, and a note that still points at the thing it is about.

A LEGEND is GENERATED. Its default mode scans the document's own facade specs — diagram node,
edge and container kinds, then the series names of every chart — and each swatch is drawn wearing
the REAL class that kind or series is painted by. That is the whole design: the legend is not a
picture of the palette, it IS the palette, so a variant switch or a theme swap recolours the key
for free and a legend can never drift from the diagram beside it.

A CALLOUT is a card plus a LEADER, and only the card is a decision. The leader is derived from
where the card and its target are NOW, by the same ``auto_side``/``anchor_point`` machinery an
edge routes with, so ``reflow`` re-anchors it after a layout pass moves the target out from under
it. It ends in a dot rather than an arrowhead: an annotation points at something, it does not
flow into it.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Literal, cast

import inkex
from inkex import BaseElement
from pydantic import BaseModel, ConfigDict

from ..model.document import Document
from ..model.errors import InvalidArgument
from ..model.handles import NodeRef
from ..query.outline import _bbox_xywh
from .chart import (
    _SERIES_COUNT,
    BarData,
    ChartSpec,
    DonutData,
    LineData,
    ScatterData,
    _part,
    read_chart_spec,
)
from .diagram import (
    _CALLOUT_ATTR,
    _CHART_ATTR,
    _CONTAINER_ATTR,
    _DEFAULT_PAD,
    _EDGE_ATTR,
    _LEGEND_ATTR,
    _NODE_ATTR,
    AnchorPref,
    Box,
    Point,
    Side,
    _box_of,
    _label_font,
    _num,
    _place_facade,
    _side_pref,
    _stack_below,
    _swap_role,
    _token,
    anchor_point,
    auto_side,
    measure_label,
    read_container_spec,
    read_edge_spec,
    read_node_spec,
)
from .resources import _class_list
from .themes import _fallback_hook, _hook, serving_theme, serving_theme_name

Style = dict[str, str]

SwatchForm = Literal["rect", "line"]
"""How a legend entry's swatch is drawn: a filled chip, or a length of the line it stands for."""

CalloutSide = Literal["auto", "N", "S", "E", "W"]
"""Which face of the target a callout sits off — ``auto`` picks the roomiest."""

# The type size the legend and callout arithmetic assumes. It MUST match the size the bundled
# default gives `.legend text` and `.callout-text`, or a card is sized for text it does not draw
# — the same contract the chart module's `_TICK_SIZE` holds with `.tick-label`.
_TEXT_SIZE = 11.0

_SWATCH_W, _SWATCH_H = 16.0, 11.0
_SWATCH_LINE = 18.0
# The column a swatch occupies, whichever form it takes: the wider of the two, so a legend mixing
# chips and lines still has ONE left edge for its labels.
_SWATCH_CELL = 18.0
_SWATCH_GAP = 6.0
_COLUMN_GAP = 16.0
# The clear air a legend row adds to the height of the text in it.
_ROW_LEAD = 6.0
# The clear air between two wrapped lines of a callout.
_LINE_LEAD = 3.0
_DOT_R = 2.0

_DEFAULT_CALLOUT_GAP = 40.0
_DEFAULT_MAX_WIDTH = 160.0
_MIN_MAX_WIDTH = 20.0

# Where a facade lands when nothing is stacked above it — the same corner a diagram node takes.
_ORIGIN = 20.0

# What each piece of a callout is, stored where it cannot be guessed at (never on the id).
_PART_ATTR = "data-callout-part"
_PART_CARD = "card"
_PART_TEXT = "text"
_PART_LEADER = "leader"
_PART_DOT = "dot"


# --- results -----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PlacedLegend:
    """A new legend: its handle, the box it took, and the entries it ended up drawing."""

    ref: NodeRef
    x: float
    y: float
    w: float
    h: float
    entries: list[str]
    auto: bool


@dataclass(frozen=True, slots=True)
class LegendEdit:
    """A patched legend: its handle, its entries after the edit, and whether it re-scanned."""

    ref: NodeRef
    entries: list[str]
    regenerated: bool = False


@dataclass(frozen=True, slots=True)
class PlacedCallout:
    """A new callout: its handle, the card's box, the lines it wrapped to, and the side used."""

    ref: NodeRef
    x: float
    y: float
    w: float
    h: float
    lines: int
    side: Side
    auto: bool


@dataclass(frozen=True, slots=True)
class CalloutEdit:
    """A patched callout: its handle, its line count, and whether the card had to move."""

    ref: NodeRef
    lines: int
    replaced: bool = False


# --- legend entries ----------------------------------------------------------


class LegendItem(BaseModel):
    """One entry a caller spells out: what to write, and what to draw the swatch AS.

    ``swatch`` names a diagram kind (``service``, ``data``, ``cluster``, …), a chart series slot
    (``series-3``), or a materialized class outright (``house-service``). ``form`` is normally
    left off — a kind the document uses on an edge draws a line, everything else a chip.
    """

    model_config = ConfigDict(extra="forbid")

    label: str
    swatch: str
    form: SwatchForm | None = None


@dataclass(frozen=True, slots=True)
class LegendEntry:
    """One resolved entry: the text, the swatch it names, and the form that swatch takes."""

    label: str
    swatch: str
    form: SwatchForm


@dataclass(frozen=True, slots=True)
class LegendSpec:
    """What a legend is, as stored on its group. ``auto`` means "generated, and re-generatable"."""

    entries: tuple[LegendEntry, ...]
    title: str
    columns: int
    auto: bool


def _form(value: str) -> SwatchForm:
    return "line" if value == "line" else "rect"


def read_legend_spec(element: BaseElement) -> LegendSpec | None:
    """The legend spec stored on ``element``, or None if it is not a legend (or is corrupt)."""
    raw = element.get(_LEGEND_ATTR)
    if raw is None:
        return None
    try:
        spec = json.loads(raw)
        entries = spec["entries"]
        if not isinstance(entries, list):
            return None
        return LegendSpec(
            entries=tuple(
                LegendEntry(
                    label=str(entry["label"]),
                    swatch=str(entry["swatch"]),
                    form=_form(str(entry.get("form", "rect"))),
                )
                for entry in entries
            ),
            title=str(spec.get("title", "")),
            columns=max(1, int(spec.get("columns", 1))),
            auto=bool(spec.get("auto", False)),
        )
    except (ValueError, TypeError, KeyError):
        return None


def _write_legend_spec(element: BaseElement, spec: LegendSpec) -> None:
    element.set(
        _LEGEND_ATTR,
        json.dumps(
            {
                "entries": [
                    {"label": entry.label, "swatch": entry.swatch, "form": entry.form}
                    for entry in spec.entries
                ],
                "title": spec.title,
                "columns": spec.columns,
                "auto": spec.auto,
            },
            separators=(",", ":"),
        ),
    )


def _facades(doc: Document, attr: str) -> Iterator[BaseElement]:
    """Every element in document order carrying ``attr`` — the one way a facade is recognised."""
    return (
        node for node in doc.svg.iter() if isinstance(node.tag, str) and node.get(attr) is not None
    )


def _chart_series(spec: ChartSpec) -> list[str]:
    """The names a chart hands out its palette to, in the order it hands them out.

    A donut's slices ARE its series — each wedge takes the next ``--series-N`` — so their labels
    count. A sparkline names nothing: it is one line, and a key entry for it would be a caption.
    """
    data = spec.data
    if isinstance(data, BarData | LineData | ScatterData):
        return [entry.name for entry in data.series]
    if isinstance(data, DonutData):
        return [piece.label for piece in data.slices]
    return []


def _edge_kinds(doc: Document) -> list[str]:
    kinds = [spec.kind for node in _facades(doc, _EDGE_ATTR) if (spec := read_edge_spec(node))]
    return list(dict.fromkeys(kinds))


def scan_legend_entries(doc: Document) -> list[LegendEntry]:
    """Generate a legend from what the document ACTUALLY draws, in first-use order.

    Node kinds, then edge kinds, then container kinds, then chart series — the reading order of
    a diagram with a chart beside it. Every group is deduplicated on first use, so a kind used
    forty times gets one row and a kind used none gets none.
    """
    nodes = [found.kind for node in _facades(doc, _NODE_ATTR) if (found := read_node_spec(node))]
    containers = [
        held.kind for node in _facades(doc, _CONTAINER_ATTR) if (held := read_container_spec(node))
    ]
    edges = _edge_kinds(doc)
    series: dict[str, str] = {}
    for node in _facades(doc, _CHART_ATTR):
        chart = read_chart_spec(node)
        if chart is None:
            continue
        for index, name in enumerate(_chart_series(chart)):
            series.setdefault(name, f"series-{index % _SERIES_COUNT + 1}")

    entries = [LegendEntry(label=kind, swatch=kind, form="rect") for kind in dict.fromkeys(nodes)]
    entries += [LegendEntry(label=kind, swatch=kind, form="line") for kind in edges]
    entries += [
        LegendEntry(label=kind, swatch=kind, form="rect")
        for kind in dict.fromkeys(containers)
        if kind not in {entry.swatch for entry in entries}
    ]
    entries += [
        LegendEntry(label=name, swatch=swatch, form="rect") for name, swatch in series.items()
    ]
    return entries


def _resolve_entries(doc: Document, items: Sequence[LegendItem]) -> list[LegendEntry]:
    """Resolve a caller's entries, deciding the form of each one it did not decide itself."""
    edges = set(_edge_kinds(doc))
    return [
        LegendEntry(
            label=item.label,
            swatch=item.swatch,
            form=item.form
            if item.form is not None
            else ("line" if item.swatch in edges else "rect"),
        )
        for item in items
    ]


# --- the classes a swatch wears ----------------------------------------------


def _swatch_class(doc: Document, swatch: str) -> str:
    """The REAL class a swatch is painted by — the same lookup the facades themselves hook with.

    A materialized class name is taken at its word; otherwise the theme serving the role (or, for
    a ``series-N`` slot, the one serving charts) answers, and failing that the bundled default is
    summoned exactly as ``apply_auto_styles`` would summon it. A swatch nothing can paint is an
    error: a legend row with no colour in it is worse than no legend.
    """
    if any(swatch in meta.class_names for meta in doc.theme_meta.values()):
        return swatch
    key = "chart" if swatch.startswith("series-") else swatch
    hook = _hook(doc, serving_theme_name(doc, key), swatch)
    if hook is None:
        hook = _fallback_hook(doc, swatch)
    if hook is None:
        raise InvalidArgument(
            f"no resident theme (nor the bundled default) paints legend swatch {swatch!r}; "
            "name a diagram kind, a 'series-N' slot, or a materialized class"
        )
    return hook


def _part_class(doc: Document, suffix: str) -> list[str]:
    """A part class (``leader``, ``callout-text``) if some theme defines it, else nothing."""
    hook = _hook(doc, serving_theme_name(doc, suffix), suffix)
    if hook is None:
        hook = _fallback_hook(doc, suffix)
    return [] if hook is None else [hook]


# --- legend geometry ---------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LegendLayout:
    """Everything the drawing pass needs, measured once: the box and the per-cell arithmetic."""

    w: float
    h: float
    pad: float
    row: float
    column: float
    title_h: float


def _legend_layout(doc: Document, spec: LegendSpec) -> LegendLayout:
    theme = serving_theme(doc, "legend")
    font = _label_font(theme)
    pad = _token(theme, "--pad-node", _DEFAULT_PAD)
    measured = [measure_label(entry.label, font, _TEXT_SIZE) for entry in spec.entries]
    if spec.title:
        measured.append(measure_label(spec.title, font, _TEXT_SIZE))
    text_h = max((size[1] for size in measured), default=_TEXT_SIZE)
    row = text_h + _ROW_LEAD
    label_w = max(
        (measure_label(entry.label, font, _TEXT_SIZE)[0] for entry in spec.entries), default=0.0
    )
    column = _SWATCH_CELL + _SWATCH_GAP + label_w
    columns = max(1, spec.columns)
    rows = math.ceil(len(spec.entries) / columns) if spec.entries else 0
    title_h = row if spec.title else 0.0
    width = max(
        2 * pad + columns * column + (columns - 1) * _COLUMN_GAP,
        2 * pad + (measure_label(spec.title, font, _TEXT_SIZE)[0] if spec.title else 0.0),
    )
    return LegendLayout(
        w=width, h=2 * pad + title_h + rows * row, pad=pad, row=row, column=column, title_h=title_h
    )


def _text_part(
    doc: Document, parent: str, content: str, at: Point, classes: Sequence[str]
) -> BaseElement:
    """One line of annotation type: anchored at its left, centered on the row it sits in."""
    element = inkex.TextElement()
    element.set("x", _num(at[0]))
    element.set("y", _num(at[1]))
    element.text = content
    return _part(
        doc,
        element,
        prefix="text",
        category="text",
        parent=parent,
        style={"text-anchor": "start", "dominant-baseline": "central"},
        classes=classes,
    )


def _build_legend(
    doc: Document, group: BaseElement, spec: LegendSpec, at: Point, *, themed: bool
) -> LegendLayout:
    """Draw (or re-draw) every child of a legend from its spec. Idempotent by construction."""
    for child in list(group):
        if isinstance(child.tag, str):
            child.delete()
    layout = _legend_layout(doc, spec)
    group_id = str(group.get_id())
    x, y = at
    _part(
        doc,
        inkex.Rectangle.new(x, y, layout.w, layout.h),
        prefix="rect",
        category="shape",
        parent=group_id,
        style=None,
        classes=(),
    )
    # The legend's own text needs no class of its own: the GROUP wears `.legend`, so the theme's
    # `.legend text` part rule reaches every line of it by descent.
    if spec.title:
        _text_part(
            doc, group_id, spec.title, (x + layout.pad, y + layout.pad + layout.row / 2.0), ()
        )
    columns = max(1, spec.columns)
    for index, entry in enumerate(spec.entries):
        row, column = divmod(index, columns)
        cell_x = x + layout.pad + column * (layout.column + _COLUMN_GAP)
        middle = y + layout.pad + layout.title_h + row * layout.row + layout.row / 2.0
        classes = [_swatch_class(doc, entry.swatch)] if themed else []
        if entry.form == "line":
            _part(
                doc,
                inkex.Line.new((cell_x, middle), (cell_x + _SWATCH_LINE, middle)),
                prefix="line",
                category="connector",
                parent=group_id,
                style=None,
                classes=classes,
            )
        else:
            _part(
                doc,
                inkex.Rectangle.new(
                    cell_x + (_SWATCH_CELL - _SWATCH_W) / 2.0,
                    middle - _SWATCH_H / 2.0,
                    _SWATCH_W,
                    _SWATCH_H,
                ),
                prefix="rect",
                category="shape",
                parent=group_id,
                style=None,
                classes=classes,
            )
        _text_part(doc, group_id, entry.label, (cell_x + _SWATCH_CELL + _SWATCH_GAP, middle), ())
    return layout


def add_legend(
    doc: Document,
    *,
    entries: list[LegendItem] | None = None,
    title: str = "",
    columns: int = 1,
    x: float | None = None,
    y: float | None = None,
    parent: str | None = None,
    name: str | None = None,
    style: Style | None = None,
    styles: list[str] | None = None,
    themed: bool = True,
) -> PlacedLegend:
    """Add a key to the picture — by default, one GENERATED from what the document already draws.

    With no ``entries`` the document is scanned: every diagram node kind, then every edge kind,
    then every container kind, then every chart series, each in first-use order and each drawn
    with the class it is actually painted by. A kind nothing uses gets no row, which is the point
    — the legend cannot describe a picture that is not there.

    Pass ``entries`` to say it yourself; the swatches are still real classes, so the key still
    tracks a variant switch. Omit ``x``/``y`` to stack the legend under the last facade in the
    same parent.
    """
    if columns < 1:
        raise InvalidArgument("a legend needs at least one column")
    auto = entries is None
    resolved = scan_legend_entries(doc) if auto else _resolve_entries(doc, entries or ())
    spec = LegendSpec(entries=tuple(resolved), title=title, columns=columns, auto=auto)

    gap = _token(serving_theme(doc, "legend"), "--gap-node", 24.0)
    stacked = _stack_below(doc.resolve_parent(parent), gap)
    at_x = x if x is not None else _ORIGIN
    at_y = y if y is not None else (_ORIGIN if stacked is None else stacked)

    group = inkex.Group()
    ref = _place_facade(
        doc,
        group,
        prefix="legend",
        category="container",
        prim="legend",
        role="legend",
        parent=parent,
        name=name,
        style=style,
        styles=styles,
        themed=themed,
    )
    _write_legend_spec(group, spec)
    layout = _build_legend(doc, group, spec, (at_x, at_y), themed=themed)
    return PlacedLegend(
        ref=ref,
        x=at_x,
        y=at_y,
        w=layout.w,
        h=layout.h,
        entries=[entry.label for entry in spec.entries],
        auto=auto,
    )


def _legend_origin(group: BaseElement) -> Point:
    """Where a legend is drawn, read off the card it owns — never off a transform it has none of."""
    card = next((child for child in group if isinstance(child, inkex.Rectangle)), None)
    box = _bbox_xywh(card) if card is not None else None
    return (_ORIGIN, _ORIGIN) if box is None else (box[0], box[1])


def edit_legend(
    doc: Document,
    target: str,
    *,
    entries: list[LegendItem] | None = None,
    title: str | None = None,
    columns: int | None = None,
    regenerate: bool = False,
) -> LegendEdit:
    """Edit a legend by its SPEC — and, with ``regenerate``, re-scan the document for its entries.

    ``regenerate`` is the call to make after adding a kind the key does not mention yet; it only
    applies to a legend that was generated in the first place, since re-scanning over entries
    somebody wrote by hand would throw their work away. The legend keeps its position: where a
    key sits is a composition decision, and nothing here is entitled to overturn it.
    """
    group = doc.resolve(target)
    spec = read_legend_spec(group)
    if spec is None:
        raise InvalidArgument(f"{target!r} is not a legend (no {_LEGEND_ATTR} spec)")
    if columns is not None and columns < 1:
        raise InvalidArgument("a legend needs at least one column")
    if regenerate and entries is not None:
        raise InvalidArgument(
            "pass `entries` to write the key yourself OR `regenerate` to re-scan the document "
            "for it — not both in one call, since one of the two would be thrown away"
        )
    if regenerate and not spec.auto:
        raise InvalidArgument(
            "this legend's entries were given explicitly, so there is nothing to regenerate "
            "from; pass `entries` to change them"
        )

    if entries is not None:
        resolved, auto = _resolve_entries(doc, entries), False
    elif regenerate:
        resolved, auto = scan_legend_entries(doc), True
    else:
        resolved, auto = list(spec.entries), spec.auto
    updated = LegendSpec(
        entries=tuple(resolved),
        title=title if title is not None else spec.title,
        columns=columns if columns is not None else spec.columns,
        auto=auto,
    )
    _write_legend_spec(group, updated)
    dressing = serving_theme_name(doc, "legend")
    themed = dressing is not None and f"{dressing}-legend" in _class_list(group)
    _build_legend(doc, group, updated, _legend_origin(group), themed=themed)
    return LegendEdit(
        ref=NodeRef(id=str(group.get_id()), tag=str(group.TAG), name=getattr(group, "label", None)),
        entries=[entry.label for entry in updated.entries],
        regenerated=regenerate,
    )


# --- callouts ----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CalloutSpec:
    """What a callout says, what it points at, and how it was placed."""

    target: str
    text: str
    kind: str
    side: AnchorPref
    distance: float
    max_width: float
    auto: bool


def read_callout_spec(element: BaseElement) -> CalloutSpec | None:
    """The callout spec stored on ``element``, or None if it is not a callout (or is corrupt)."""
    raw = element.get(_CALLOUT_ATTR)
    if raw is None:
        return None
    try:
        spec = json.loads(raw)
        return CalloutSpec(
            target=str(spec["target"]),
            text=str(spec["text"]),
            kind=str(spec["kind"]),
            side=_side_pref(str(spec.get("side", "auto"))),
            distance=float(spec["distance"]),
            max_width=float(spec["max_width"]),
            auto=bool(spec.get("auto", False)),
        )
    except (ValueError, TypeError, KeyError):
        return None


def _write_callout_spec(element: BaseElement, spec: CalloutSpec) -> None:
    element.set(
        _CALLOUT_ATTR,
        json.dumps(
            {
                "target": spec.target,
                "text": spec.text,
                "kind": spec.kind,
                "side": spec.side,
                "distance": spec.distance,
                "max_width": spec.max_width,
                "auto": spec.auto,
            },
            separators=(",", ":"),
        ),
    )


def wrap_words(text: str, *, max_width: float, font: str, size: float) -> list[str]:
    """Greedy word wrap at ``max_width``, MEASURED rather than counted in characters.

    A single word wider than the limit keeps its own line rather than being broken mid-word: an
    identifier or a URL sliced in half is a worse answer than a card one word too wide.
    """
    words = text.split()
    if not words:
        return []
    lines: list[str] = []
    current = words[0]
    for word in words[1:]:
        candidate = f"{current} {word}"
        if measure_label(candidate, font, size)[0] <= max_width:
            current = candidate
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines


@dataclass(frozen=True, slots=True)
class CardText:
    """A wrapped card's text and the box that text asks for."""

    lines: tuple[str, ...]
    w: float
    h: float
    pad: float
    line_h: float


def _card_text(doc: Document, spec: CalloutSpec) -> CardText:
    theme = serving_theme(doc, spec.kind)
    font = _label_font(theme)
    pad = _token(theme, "--pad-node", _DEFAULT_PAD)
    lines = wrap_words(spec.text, max_width=spec.max_width, font=font, size=_TEXT_SIZE)
    measured = [measure_label(line, font, _TEXT_SIZE) for line in lines]
    line_h = max((size[1] for size in measured), default=_TEXT_SIZE)
    widest = max((size[0] for size in measured), default=0.0)
    count = max(1, len(lines))
    return CardText(
        lines=tuple(lines),
        w=widest + 2 * pad,
        h=count * line_h + (count - 1) * _LINE_LEAD + 2 * pad,
        pad=pad,
        line_h=line_h,
    )


def _canvas(doc: Document) -> tuple[float, float, float, float]:
    """The drawable rectangle, from the viewBox — what "free canvas" is measured against."""
    try:
        view = doc.svg.get_viewbox()
    except Exception:  # pragma: no cover - a document without a parseable viewBox
        view = []
    if view and len(view) == 4 and view[2] > 0 and view[3] > 0:
        return (float(view[0]), float(view[1]), float(view[2]), float(view[3]))
    return (0.0, 0.0, 0.0, 0.0)


# N before E before S before W: a note above the thing it annotates reads first, and a note
# beside it reads before one below it. Only used to break a tie in free space.
_SIDE_ORDER: tuple[Side, ...] = ("N", "E", "S", "W")


def roomiest_side(box: Box, canvas: tuple[float, float, float, float]) -> Side:
    """The face of ``box`` with the most clear canvas beyond it; ties settled N > E > S > W."""
    left, top, width, height = canvas
    free: dict[Side, float] = {
        "N": box.y - top,
        "E": (left + width) - (box.x + box.w),
        "S": (top + height) - (box.y + box.h),
        "W": box.x - left,
    }
    best = _SIDE_ORDER[0]
    for side in _SIDE_ORDER[1:]:
        if free[side] > free[best]:
            best = side
    return best


def card_box(box: Box, side: Side, distance: float, w: float, h: float) -> tuple[float, float]:
    """Where a card's top-left goes: ``distance`` clear of one face, centered on it."""
    if side == "N":
        return (box.cx - w / 2.0, box.y - distance - h)
    if side == "S":
        return (box.cx - w / 2.0, box.y + box.h + distance)
    if side == "E":
        return (box.x + box.w + distance, box.cy - h / 2.0)
    return (box.x - distance - w, box.cy - h / 2.0)


def _callout_groups(doc: Document) -> Iterator[tuple[BaseElement, CalloutSpec]]:
    for node in _facades(doc, _CALLOUT_ATTR):
        spec = read_callout_spec(node)
        if spec is not None:
            yield node, spec


def _named_part(group: BaseElement, part: str) -> BaseElement | None:
    return next((child for child in group if child.get(_PART_ATTR) == part), None)


def _card_of(group: BaseElement) -> Box | None:
    card = _named_part(group, _PART_CARD)
    box = _bbox_xywh(card) if card is not None else None
    return None if box is None else Box(box[0], box[1], box[2], box[3])


def _leader_ends(target: Box, card: Box, side: AnchorPref) -> tuple[Point, Point]:
    """The two ends of a leader: the target's face midpoint, and the card's facing midpoint.

    ``fraction`` is fixed at 0.5 and nothing is spread — a callout is the only thing pointing at
    its target, so there are no siblings to share the face with.
    """
    at_target = side if side != "auto" else auto_side(target, card)
    return (
        anchor_point(card, auto_side(card, target), 0.5),
        anchor_point(target, at_target, 0.5),
    )


def _derive_leader(doc: Document, group: BaseElement, spec: CalloutSpec) -> bool:
    """Re-draw a callout's leader from where its card and its target are NOW; False if it cannot."""
    target = _box_of(doc, spec.target)
    card = _card_of(group)
    if target is None or card is None:
        return False
    start, end = _leader_ends(target, card, spec.side)
    classes = _part_class(doc, "leader") if _themed_callout(doc, group) else []
    line = _named_part(group, _PART_LEADER)
    if line is None:
        line = _part(
            doc,
            inkex.Line.new(start, end),
            prefix="leader",
            category="connector",
            parent=str(group.get_id()),
            style=None,
            classes=classes,
        )
        line.set(_PART_ATTR, _PART_LEADER)
    for key, value in (("x1", start[0]), ("y1", start[1]), ("x2", end[0]), ("y2", end[1])):
        line.set(key, _num(value))
    dot = _named_part(group, _PART_DOT)
    if dot is None:
        dot = _part(
            doc,
            inkex.Circle.new(end, _DOT_R),
            prefix="leader-dot",
            category="shape",
            parent=str(group.get_id()),
            style=None,
            classes=classes,
        )
        dot.set(_PART_ATTR, _PART_DOT)
    dot.set("cx", _num(end[0]))
    dot.set("cy", _num(end[1]))
    return True


def _themed_callout(doc: Document, group: BaseElement) -> bool:
    """Whether this callout was dressed — read off the role class it is (or is not) wearing."""
    worn = set(_class_list(group))
    return any(cls in worn for meta in doc.theme_meta.values() for cls in meta.class_names)


def _build_card(
    doc: Document, group: BaseElement, spec: CalloutSpec, at: Point, text: CardText, *, themed: bool
) -> None:
    """Draw (or re-draw) a callout's card and its wrapped text. The leader is derived separately."""
    for child in list(group):
        if isinstance(child.tag, str) and child.get(_PART_ATTR) in (None, _PART_CARD, _PART_TEXT):
            child.delete()
    group_id = str(group.get_id())
    card = _part(
        doc,
        inkex.Rectangle.new(at[0], at[1], text.w, text.h),
        prefix="rect",
        category="shape",
        parent=group_id,
        style=None,
        classes=(),
    )
    card.set(_PART_ATTR, _PART_CARD)
    # The card's text carries its own part class rather than relying on a `.note text` descendant
    # rule: `.note` is shared with the diagram note KIND, and a rule written that way would resize
    # every diagram note's label as a side effect of adding callouts.
    classes = _part_class(doc, "callout-text") if themed else []
    for index, line in enumerate(text.lines):
        middle = at[1] + text.pad + index * (text.line_h + _LINE_LEAD) + text.line_h / 2.0
        element = _text_part(doc, group_id, line, (at[0] + text.pad, middle), classes)
        element.set(_PART_ATTR, _PART_TEXT)


def add_callout(
    doc: Document,
    *,
    target: str,
    text: str,
    kind: str = "note",
    side: CalloutSide = "auto",
    distance: float | None = None,
    x: float | None = None,
    y: float | None = None,
    max_width: float = _DEFAULT_MAX_WIDTH,
    parent: str | None = None,
    name: str | None = None,
    style: Style | None = None,
    styles: list[str] | None = None,
    themed: bool = True,
) -> PlacedCallout:
    """Annotate a node: a wrapped card, plus a leader from the card to the thing it is about.

    The card is text-wrapped at ``max_width`` and sized to what it wrapped to. Left to itself it
    sits off the side of the target with the most free canvas, ``--callout-gap`` clear of it; give
    ``x``/``y`` and the card is placed verbatim and ``side`` only steers the leader.

    The leader is DERIVED, never stored: ``reflow`` re-anchors it at both ends after the target
    moves, so an annotation survives a layout pass that a hand-drawn line would not.
    """
    if max_width < _MIN_MAX_WIDTH:
        raise InvalidArgument(f"a callout needs at least {_MIN_MAX_WIDTH:g} to wrap its text into")
    if (x is None) != (y is None):
        raise InvalidArgument(
            "a callout is placed by BOTH x and y or by neither — one of the two on its own "
            "would be silently ignored in favour of the automatic placement"
        )
    box = _box_of(doc, target)
    if box is None:
        raise InvalidArgument(f"callout target {target!r} has no bounding box to point at")
    target_id = str(doc.resolve(target).get_id())
    gap = (
        distance
        if distance is not None
        else _token(serving_theme(doc, kind), "--callout-gap", _DEFAULT_CALLOUT_GAP)
    )

    spec = CalloutSpec(
        target=target_id,
        text=text,
        kind=kind,
        side=side,
        distance=gap,
        max_width=max_width,
        auto=x is None,
    )
    wrapped = _card_text(doc, spec)
    chosen: Side = side if side != "auto" else roomiest_side(box, _canvas(doc))
    if spec.auto:
        at = card_box(box, chosen, gap, wrapped.w, wrapped.h)
    else:
        at = (float(cast(float, x)), float(cast(float, y)))

    group = inkex.Group()
    ref = _place_facade(
        doc,
        group,
        prefix="callout",
        category="container",
        prim="callout",
        role=kind,
        parent=parent,
        name=name,
        style=style,
        styles=styles,
        themed=themed,
    )
    _write_callout_spec(group, spec)
    _build_card(doc, group, spec, at, wrapped, themed=themed)
    _derive_leader(doc, group, spec)
    return PlacedCallout(
        ref=ref,
        x=at[0],
        y=at[1],
        w=wrapped.w,
        h=wrapped.h,
        lines=len(wrapped.lines),
        side=chosen,
        auto=spec.auto,
    )


def edit_callout(
    doc: Document,
    target: str,
    *,
    text: str | None = None,
    kind: str | None = None,
    side: CalloutSide | None = None,
) -> CalloutEdit:
    """Edit a callout by its SPEC — new words, a new kind, a new side — and re-derive its leader.

    New ``text`` is re-wrapped and the card re-sized to it, and a new ``side`` moves an auto-placed
    card to that face — leaving it where it was while re-pointing the leader would run the line
    straight through the card. A card placed by hand keeps its corner either way, because that
    position was a decision; for it, ``side`` steers the leader alone.
    """
    group = doc.resolve(target)
    spec = read_callout_spec(group)
    if spec is None:
        raise InvalidArgument(f"{target!r} is not a callout (no {_CALLOUT_ATTR} spec)")
    if kind is not None and kind != spec.kind:
        _swap_role(doc, group, spec.kind, kind)
    updated = CalloutSpec(
        target=spec.target,
        text=text if text is not None else spec.text,
        kind=kind if kind is not None else spec.kind,
        side=side if side is not None else spec.side,
        distance=spec.distance,
        max_width=spec.max_width,
        auto=spec.auto,
    )
    _write_callout_spec(group, updated)

    replaced = False
    current = _card_of(group)
    if text is not None or (side is not None and updated.auto):
        wrapped = _card_text(doc, updated)
        box = _box_of(doc, updated.target)
        at = (current.x, current.y) if current is not None else (_ORIGIN, _ORIGIN)
        if updated.auto and box is not None:
            chosen: Side = (
                updated.side if updated.side != "auto" else roomiest_side(box, _canvas(doc))
            )
            at = card_box(box, chosen, updated.distance, wrapped.w, wrapped.h)
            replaced = (at[0], at[1]) != (current.x, current.y) if current is not None else True
        _build_card(doc, group, updated, at, wrapped, themed=_themed_callout(doc, group))
        lines = len(wrapped.lines)
    else:
        lines = sum(1 for child in group if child.get(_PART_ATTR) == _PART_TEXT)
    _derive_leader(doc, group, updated)
    return CalloutEdit(
        ref=NodeRef(id=str(group.get_id()), tag=str(group.TAG), name=getattr(group, "label", None)),
        lines=lines,
        replaced=replaced,
    )


def reanchor_callouts(doc: Document, *, scope: set[str] | None = None) -> tuple[int, list[str]]:
    """Re-derive every callout's leader from the CURRENT boxes; returns (how many, which could not).

    The card is never moved: where an annotation sits is a composition decision, and a reflow is
    not entitled to overturn it. Only the two ends of the leader move.
    """
    reanchored, skipped = 0, []
    for group, spec in _callout_groups(doc):
        if scope is not None and not ({str(group.get_id()), spec.target} & scope):
            continue
        if _derive_leader(doc, group, spec):
            reanchored += 1
        else:
            skipped.append(str(group.get_id()))
    return reanchored, skipped
