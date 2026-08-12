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

A TABLE is MEASURED. Every column is as wide as the widest thing in it (clamped, and then the
cell wraps), every row as tall as the tallest cell in it, and a column of numbers right-aligns
itself because a column of numbers is only readable that way. Nothing about the layout is a
parameter the caller has to get right, which is the entire reason to have the facade: hand-laid
table columns are the single most common way a generated picture ends up with text on top of
text.

A CALLOUT CARD is the callout's card WITHOUT the leader — a standalone panel with an accent bar,
a title and a body, for the note that is about the picture rather than about one node in it. The
two are deliberately separate calls: ``add_callout`` needs a target and would be lying without
one, and a card that pointed at nothing in particular is a different thing to reach for.

Tables and cards are placed with a ``translate`` and authored in their own local frame, the way
:mod:`~svg_mcp.ops.chart` does it — both re-derive ALL of their children from their spec on every
edit, and a group that carries its position in a transform keeps that position for free. The
legend and the callout predate that and read their origin back off the card they drew.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Literal, cast

import inkex
from inkex import BaseElement
from pydantic import BaseModel, ConfigDict, ValidationError

from ..model.document import Document
from ..model.errors import InvalidArgument
from ..model.handles import NodeRef
from ..query.outline import _bbox_xywh, _to_local_point
from .chart import (
    _SERIES_COUNT,
    _TITLE_SIZE,
    BarData,
    ChartDatum,
    ChartSpec,
    DonutData,
    LineData,
    ScatterData,
    _part,
    datum_anchor,
    read_chart_spec,
)
from .diagram import (
    _CALLOUT_ATTR,
    _CARD_ATTR,
    _CHART_ATTR,
    _CONTAINER_ATTR,
    _DEFAULT_PAD,
    _EDGE_ATTR,
    _LEGEND_ATTR,
    _NODE_ATTR,
    _TABLE_ATTR,
    AnchorPref,
    Box,
    Point,
    Side,
    _box_of,
    _facade_body,
    _label_font,
    _num,
    _place_facade,
    _rekind,
    _side_pref,
    _stack_below,
    _token,
    anchor_point,
    auto_origin,
    auto_position,
    auto_side,
    measure_label,
    read_container_spec,
    read_edge_spec,
    read_node_spec,
)
from .themes import _fallback_hook, _hook, serving_theme, serving_theme_name, worn_theme

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
    # No ``owner`` preference here, unlike ``_part_class``: a swatch is not part of the legend's
    # own dressing, it STANDS IN for another facade's kind, and it has to be painted by whichever
    # theme paints that kind — or the key stops describing the picture it is a key to.
    hook = _hook(doc, serving_theme_name(doc, key), swatch)
    if hook is None:
        hook = _fallback_hook(doc, swatch)
    if hook is None:
        raise InvalidArgument(
            f"no resident theme (nor the bundled default) paints legend swatch {swatch!r}; "
            "name a diagram kind, a 'series-N' slot, or a materialized class"
        )
    return hook


def _part_class(doc: Document, suffix: str, owner: str | None = None) -> list[str]:
    """A part class (``leader``, ``callout-text``) if some theme defines it, else nothing.

    ``owner`` is the theme the facade being rebuilt is actually WEARING; it gets first refusal,
    so a rebuild dresses its children in the same theme as the group rather than in whichever
    theme a routing scan happens to name today.
    """
    hook = _hook(doc, owner, suffix) if owner is not None else None
    if hook is None:
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
    doc: Document,
    parent: str,
    content: str,
    at: Point,
    classes: Sequence[str],
    *,
    anchor: str = "start",
) -> BaseElement:
    """One line of annotation type: anchored as asked, centered on the row it sits in.

    ``anchor`` is left at ``start`` by everything but a table, which is the only annotation with
    a column to right-align a number in.
    """
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
        style={"text-anchor": anchor, "dominant-baseline": "central"},
        classes=classes,
    )


def _build_legend(doc: Document, group: BaseElement, spec: LegendSpec, at: Point) -> LegendLayout:
    """Draw (or re-draw) every child of a legend from its spec. Idempotent by construction.

    ``at`` is in the GROUP's frame, and the theme to dress the swatches in is read off the group
    itself — so a rebuild lands the key exactly where it was, in the theme it is already wearing.
    """
    for child in list(group):
        if isinstance(child.tag, str):
            child.delete()
    dressing = worn_theme(doc, group, "legend", "container--legend", "container")
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
        classes = [_swatch_class(doc, entry.swatch)] if dressing is not None else []
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
    parent_element = doc.resolve_parent(parent)
    stacked = _stack_below(parent_element, gap)
    # An x/y the caller gave is PARENT-LOCAL already, exactly as it is for ``add_rect``; only a
    # MEASURED position — the stack — is a world number, and ``auto_origin`` crosses that one.
    auto_x, auto_y = auto_origin(parent_element, stacked, _ORIGIN)
    at_x = x if x is not None else auto_x
    at_y = y if y is not None else auto_y

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
    # A swatch nothing paints raises mid-build, and a half-drawn key in the tree is worse than
    # none — the whole group goes again if any row of it fails.
    with _facade_body(doc, ref):
        _write_legend_spec(group, spec)
        layout = _build_legend(doc, group, spec, (at_x, at_y))
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
    """Where a legend is drawn, in the GROUP's frame — read off the card it owns.

    ``_bbox_xywh`` answers in world coordinates, but this origin is fed straight back to the
    drawing pass, which authors in the group's frame; without crossing back, a legend under any
    ancestor transform walks by that transform on every single edit.
    """
    card = next((child for child in group if isinstance(child, inkex.Rectangle)), None)
    box = _bbox_xywh(card) if card is not None else None
    if box is None:
        return (_ORIGIN, _ORIGIN)
    return _to_local_point(group, (box[0], box[1]))


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
    _build_legend(doc, group, updated, _legend_origin(group))
    return LegendEdit(
        ref=NodeRef(id=str(group.get_id()), tag=str(group.TAG), name=getattr(group, "label", None)),
        entries=[entry.label for entry in updated.entries],
        regenerated=regenerate,
    )


# --- callouts ----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CalloutSpec:
    """What a callout says, what it points at, and how it was placed.

    ``themed`` is the dressing intent it was built with — see
    :class:`~svg_mcp.ops.diagram.NodeSpec`.
    """

    target: str
    text: str
    kind: str
    side: AnchorPref
    distance: float
    max_width: float
    auto: bool
    themed: bool = True
    datum: ChartDatum | None = None
    """The chart datum the leader points at, when it points at one datum rather than at a box.

    Stored so a ``reflow`` re-anchors the leader on the same bar after an ``edit_chart`` has
    moved it — the WHOLE point of naming a datum rather than a coordinate."""


def read_callout_spec(element: BaseElement) -> CalloutSpec | None:
    """The callout spec stored on ``element``, or None if it is not a callout (or is corrupt)."""
    raw = element.get(_CALLOUT_ATTR)
    if raw is None:
        return None
    try:
        spec = json.loads(raw)
        datum = spec.get("datum")
        return CalloutSpec(
            target=str(spec["target"]),
            text=str(spec["text"]),
            kind=str(spec["kind"]),
            side=_side_pref(str(spec.get("side", "auto"))),
            distance=float(spec["distance"]),
            max_width=float(spec["max_width"]),
            auto=bool(spec.get("auto", False)),
            themed=bool(spec.get("themed", True)),
            datum=None if datum is None else ChartDatum.model_validate(datum),
        )
    except (ValueError, TypeError, KeyError, ValidationError):
        return None


def _write_callout_spec(element: BaseElement, spec: CalloutSpec) -> None:
    """Store the spec. A callout that points at a BOX writes no ``datum`` key at all — an absent
    optional block is what keeps an unchanged callout's markup unchanged."""
    stored: dict[str, object] = {
        "target": spec.target,
        "text": spec.text,
        "kind": spec.kind,
        "side": spec.side,
        "distance": spec.distance,
        "max_width": spec.max_width,
        "auto": spec.auto,
        "themed": spec.themed,
    }
    if spec.datum is not None:
        stored["datum"] = spec.datum.model_dump()
    element.set(_CALLOUT_ATTR, json.dumps(stored, separators=(",", ":")))


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


def _leader_ends(
    target: Box, card: Box, side: AnchorPref, at: Point | None = None
) -> tuple[Point, Point]:
    """The two ends of a leader: the target's face midpoint, and the card's facing midpoint.

    ``fraction`` is fixed at 0.5 and nothing is spread — a callout is the only thing pointing at
    its target, so there are no siblings to share the face with.

    ``at`` is a DATUM anchor, and it replaces the target's face outright: a note about one bar
    points at that bar, not at the side of the chart it happens to be in. ``side`` is then moot
    (there is no face to choose), but the card still turns whichever of its own faces looks at it.
    """
    if at is not None:
        return (anchor_point(card, auto_side(card, Box(at[0], at[1], 0.0, 0.0)), 0.5), at)
    at_target = side if side != "auto" else auto_side(target, card)
    return (
        anchor_point(card, auto_side(card, target), 0.5),
        anchor_point(target, at_target, 0.5),
    )


def _derive_leader(doc: Document, group: BaseElement, spec: CalloutSpec) -> bool:
    """Re-draw a callout's leader from where its card and its target are NOW; False if it cannot.

    Both boxes are WORLD boxes, so both ends of the leader are computed in world and then crossed
    into the group's frame — the line is a child of the group, and its x1/y1/x2/y2 are read there.
    """
    target = _box_of(doc, spec.target)
    card = _card_of(group)
    if target is None or card is None:
        return False
    at = None
    if spec.datum is not None:
        # The datum is re-derived, not remembered: this is what re-anchors the leader on the same
        # bar after an `edit_chart` changed the data under it.
        at = datum_anchor(doc, doc.resolve(spec.target), spec.datum)
        if at is None:
            return False
    world_start, world_end = _leader_ends(target, card, spec.side, at)
    start = _to_local_point(group, world_start)
    end = _to_local_point(group, world_end)
    dressing = worn_theme(doc, group, spec.kind, "container--callout", "container")
    classes = _part_class(doc, "leader", dressing) if dressing is not None else []
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


def _build_card(
    doc: Document, group: BaseElement, spec: CalloutSpec, at: Point, text: CardText
) -> None:
    """Draw (or re-draw) a callout's card and its wrapped text. The leader is derived separately.

    ``at`` is in the GROUP's frame, and the theme the text is set in is read off the group.
    """
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
    dressing = worn_theme(doc, group, spec.kind, "container--callout", "container")
    classes = _part_class(doc, "callout-text", dressing) if dressing is not None else []
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
    datum: ChartDatum | None = None,
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

    ``datum`` points the leader at ONE mark of a chart — a bar, a point, a slice — instead of at
    the side of the chart's box. It names the datum by series and position rather than by
    coordinate, so it is re-derived on every reflow and still lands on the same number after an
    ``edit_chart`` has moved every mark on the plot.
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
    element = doc.resolve(target)
    if datum is not None:
        if read_chart_spec(element) is None:
            raise InvalidArgument(
                f"callout target {target!r} is not a chart, so it has no data to point at; drop "
                "`datum` to point at the node's own box instead"
            )
        if datum_anchor(doc, element, datum) is None:
            raise InvalidArgument(
                f"this chart has no datum at series {datum.series!r}, index {datum.index}"
            )
    target_id = str(element.get_id())
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
        themed=themed,
        datum=datum,
    )
    wrapped = _card_text(doc, spec)
    chosen: Side = side if side != "auto" else roomiest_side(box, _canvas(doc))
    parent_element = doc.resolve_parent(parent)
    if spec.auto:
        # The side placement is derived from the target's WORLD box, so it crosses into the
        # parent's frame; an x/y the caller gave is parent-local already and is used verbatim.
        at = auto_position(parent_element, card_box(box, chosen, gap, wrapped.w, wrapped.h))
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
    with _facade_body(doc, ref):
        _write_callout_spec(group, spec)
        _build_card(doc, group, spec, at, wrapped)
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
    _rekind(doc, group, spec.kind, kind, themed=spec.themed)
    updated = CalloutSpec(
        target=spec.target,
        text=text if text is not None else spec.text,
        kind=kind if kind is not None else spec.kind,
        side=side if side is not None else spec.side,
        distance=spec.distance,
        max_width=spec.max_width,
        auto=spec.auto,
        themed=spec.themed,
        datum=spec.datum,
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
        _build_card(doc, group, updated, _to_local_point(group, at), wrapped)
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


# --- tables ------------------------------------------------------------------

Align = Literal["left", "right", "center"]
"""How one table column is set. Derived from what is IN the column unless the caller says."""

_ALIGNS: frozenset[str] = frozenset({"left", "right", "center"})

# The clear air a cell leaves above and below its text. Not a token: a row's vertical rhythm is
# type-set (it is a function of the line height), where the horizontal padding is a look and so
# comes from `--pad-cell`.
_CELL_PAD_Y = 5.0
_DEFAULT_CELL_PAD = 8.0
_DEFAULT_MAX_COL = 220.0
_MIN_COL_WIDTH = 20.0


def _is_number(cell: str) -> bool:
    """Whether a cell READS as a number — the test that decides a column right-aligns itself.

    Thousands separators, a currency mark and a percent sign are stripped BEFORE the test and
    nowhere else: the cell is drawn exactly as it was handed over, because "$1,200" is what the
    author wrote and re-formatting somebody's data is not a layout decision.
    """
    text = cell.strip().replace(",", "").replace("$", "").replace("%", "").replace("−", "-")
    if not text:
        return False
    try:
        float(text)
    except ValueError:
        return False
    return True


def _align(value: str) -> Align:
    if value == "right":
        return "right"
    if value == "center":
        return "center"
    return "left"


@dataclass(frozen=True, slots=True)
class TableSpec:
    """What a table is, as stored on its group. Position lives in the group's transform."""

    rows: tuple[tuple[str, ...], ...]
    header: tuple[str, ...] | None
    title: str
    col_align: tuple[Align, ...]
    zebra: bool
    max_col_width: float
    x_pad: float | None
    auto: bool


@dataclass(frozen=True, slots=True)
class PlacedTable:
    """A new table: its handle, the box it took, and the columns it measured itself into."""

    ref: NodeRef
    x: float
    y: float
    w: float
    h: float
    columns: list[float]
    col_align: list[Align]
    auto: bool


@dataclass(frozen=True, slots=True)
class TableEdit:
    """A patched table: its handle, how many children the rebuild produced, and its alignments."""

    ref: NodeRef
    children: int
    col_align: list[Align]


def read_table_spec(element: BaseElement) -> TableSpec | None:
    """The table spec stored on ``element``, or None if it is not a table (or is corrupt)."""
    raw = element.get(_TABLE_ATTR)
    if raw is None:
        return None
    try:
        spec = json.loads(raw)
        rows = spec["rows"]
        header = spec.get("header") or None
        if not isinstance(rows, list) or (header is not None and not isinstance(header, list)):
            return None
        pad = spec.get("x_pad")
        return TableSpec(
            rows=tuple(tuple(str(cell) for cell in row) for row in rows),
            header=None if header is None else tuple(str(cell) for cell in header),
            title=str(spec.get("title", "")),
            col_align=tuple(_align(str(value)) for value in spec.get("col_align", ())),
            zebra=bool(spec.get("zebra", True)),
            max_col_width=float(spec.get("max_col_width", _DEFAULT_MAX_COL)),
            x_pad=None if pad is None else float(pad),
            auto=bool(spec.get("auto", False)),
        )
    except (ValueError, TypeError, KeyError):
        return None


def _write_table_spec(element: BaseElement, spec: TableSpec) -> None:
    element.set(
        _TABLE_ATTR,
        json.dumps(
            {
                "rows": [list(row) for row in spec.rows],
                # A header of no columns cannot exist, so [] is how "no header row" is written —
                # a null here would make the whole spec unreadable to `get_params`.
                "header": list(spec.header) if spec.header is not None else [],
                "title": spec.title,
                "col_align": list(spec.col_align),
                "zebra": spec.zebra,
                "max_col_width": spec.max_col_width,
                "x_pad": spec.x_pad,
                "auto": spec.auto,
            },
            separators=(",", ":"),
        ),
    )


def table_columns(rows: Sequence[Sequence[str]], header: Sequence[str] | None) -> int:
    """How many columns a table has — and the one place a ragged table is refused.

    A short row is never padded silently: the missing cell might be the one that mattered, and a
    table quietly one column narrower than its header is a lie about what the numbers mean.
    """
    widths = {len(row) for row in rows}
    if len(widths) > 1:
        raise InvalidArgument(
            f"a table's rows must all have the same number of cells; got rows of "
            f"{sorted(widths)} cells. Pad the short ones with '' rather than leaving them ragged"
        )
    columns = widths.pop() if widths else (len(header) if header is not None else 0)
    if header is not None and len(header) != columns:
        raise InvalidArgument(
            f"the header has {len(header)} cells but the rows have {columns}; a header names the "
            "columns, so there has to be exactly one entry per column"
        )
    if columns < 1:
        raise InvalidArgument("a table needs at least one column of cells to draw")
    return columns


def _numeric_align(rows: Sequence[Sequence[str]], index: int) -> Align:
    """A column right-aligns itself only if EVERY body cell in it is a number."""
    return "right" if rows and all(_is_number(row[index]) for row in rows) else "left"


def resolve_col_align(
    rows: Sequence[Sequence[str]], columns: int, given: Sequence[str] | None
) -> tuple[Align, ...]:
    """The alignment of each column: the caller's, or measured from what the column holds.

    The header is deliberately NOT consulted — a column of numbers under the word "Latency" is
    still a column of numbers, and letting its title decide would flip the digits back to the
    left just because somebody named the column.
    """
    if given is None:
        return tuple(_numeric_align(rows, index) for index in range(columns))
    if len(given) != columns:
        raise InvalidArgument(
            f"col_align has {len(given)} entries but the table has {columns} columns; "
            "give one alignment per column or none at all"
        )
    for value in given:
        if value not in _ALIGNS:
            raise InvalidArgument(
                f"{value!r} is not a column alignment; use 'left', 'right' or 'center'"
            )
    return tuple(_align(value) for value in given)


@dataclass(frozen=True, slots=True)
class TableLayout:
    """A table measured: the column widths, the row heights, and every cell already wrapped."""

    columns: tuple[float, ...]
    header_cells: tuple[tuple[str, ...], ...]
    body_cells: tuple[tuple[tuple[str, ...], ...], ...]
    header_h: float
    row_h: tuple[float, ...]
    title_h: float
    line_h: float
    pad: float
    w: float
    h: float


def _block_h(lines: Sequence[str], line_h: float) -> float:
    """How tall one wrapped cell's text is — an empty cell still occupies its line."""
    count = max(1, len(lines))
    return count * line_h + (count - 1) * _LINE_LEAD


def _table_layout(doc: Document, spec: TableSpec) -> TableLayout:
    """Measure a table: every column as wide as its widest cell, every row as tall as its tallest.

    The clamp to ``max_col_width`` is what makes a paragraph in a cell survivable — the column
    stops growing and the cell wraps into it instead, taking its row with it.
    """
    theme = serving_theme(doc, "table")
    font = _label_font(theme)
    pad = spec.x_pad if spec.x_pad is not None else _token(theme, "--pad-cell", _DEFAULT_CELL_PAD)
    limit = spec.max_col_width
    columns = len(spec.col_align)

    def wrapped(cell: str) -> tuple[str, ...]:
        return tuple(wrap_words(cell, max_width=limit, font=font, size=_TEXT_SIZE))

    sources: list[Sequence[str]] = list(spec.rows)
    if spec.header is not None:
        sources.insert(0, spec.header)
    measured = [measure_label(cell, font, _TEXT_SIZE) for row in sources for cell in row]
    line_h = max((size[1] for size in measured), default=_TEXT_SIZE)
    widths = tuple(
        min(
            limit,
            max(
                (measure_label(row[index], font, _TEXT_SIZE)[0] for row in sources),
                default=0.0,
            ),
        )
        + 2 * pad
        for index in range(columns)
    )

    header_cells = tuple(wrapped(cell) for cell in (spec.header or ()))
    body_cells = tuple(tuple(wrapped(cell) for cell in row) for row in spec.rows)
    row_h = tuple(
        max((_block_h(cell, line_h) for cell in cells), default=line_h) + 2 * _CELL_PAD_Y
        for cells in body_cells
    )
    header_h = (
        0.0
        if spec.header is None
        else max((_block_h(cell, line_h) for cell in header_cells), default=line_h)
        + 2 * _CELL_PAD_Y
    )
    title_h = measure_label(spec.title, font, _TITLE_SIZE)[1] + _ROW_LEAD if spec.title else 0.0
    return TableLayout(
        columns=widths,
        header_cells=header_cells,
        body_cells=body_cells,
        header_h=header_h,
        row_h=row_h,
        title_h=title_h,
        line_h=line_h,
        pad=pad,
        w=sum(widths),
        h=title_h + header_h + sum(row_h),
    )


def _cell_anchor(x: float, width: float, pad: float, align: Align) -> tuple[float, str]:
    """Where one cell's text starts and what it is anchored by, for the alignment it was given."""
    if align == "right":
        return (x + width - pad, "end")
    if align == "center":
        return (x + width / 2.0, "middle")
    return (x + pad, "start")


def _draw_row(
    doc: Document,
    parent: str,
    layout: TableLayout,
    aligns: Sequence[Align],
    cells: Sequence[Sequence[str]],
    top: float,
    height: float,
    classes: Sequence[str],
) -> None:
    """One row of cell text: each cell aligned in its column and centered in the row's height."""
    x = 0.0
    for index, lines in enumerate(cells):
        width = layout.columns[index]
        start = top + (height - _block_h(lines, layout.line_h)) / 2.0
        at_x, anchor = _cell_anchor(x, width, layout.pad, aligns[index])
        for line_index, line in enumerate(lines):
            middle = start + line_index * (layout.line_h + _LINE_LEAD) + layout.line_h / 2.0
            _text_part(doc, parent, line, (at_x, middle), classes, anchor=anchor)
        x += width


def _rect_part(
    doc: Document, parent: str, box: tuple[float, float, float, float], classes: Sequence[str]
) -> BaseElement:
    return _part(
        doc,
        inkex.Rectangle.new(box[0], box[1], box[2], box[3]),
        prefix="rect",
        category="shape",
        parent=parent,
        style=None,
        classes=classes,
    )


def _build_table(doc: Document, group: BaseElement, spec: TableSpec) -> TableLayout:
    """Draw (or re-draw) every child of a table from its spec. Idempotent by construction.

    Everything is authored in the group's own frame, so the group's ``translate`` is the ONLY
    statement of where the table is and a rebuild cannot move it.
    """
    for child in list(group):
        if isinstance(child.tag, str):
            child.delete()
    layout = _table_layout(doc, spec)
    group_id = str(group.get_id())

    # A table not wearing its role hook was built with themed=False; a rebuild that dressed it
    # anyway would quietly overturn that decision, so the absence is honoured (as in a chart).
    dressing = worn_theme(doc, group, "table", "container--table", "container")

    def part(suffix: str) -> list[str]:
        return _part_class(doc, suffix, dressing) if dressing is not None else []

    top = layout.title_h
    # The border is drawn bare: the GROUP wears `.table`, so its canvas fill and hairline edge
    # inherit into this rect exactly the way a legend's panel is painted.
    _rect_part(doc, group_id, (0.0, top, layout.w, layout.h - top), ())
    if spec.title:
        # A table's title is a chart's title: same size, same weight, same job above a block of
        # data. Reusing the class is what keeps a table and a chart beside it agreeing.
        _text_part(doc, group_id, spec.title, (0.0, layout.title_h / 2.0), part("chart-title"))
    if spec.header is not None:
        _rect_part(doc, group_id, (0.0, top, layout.w, layout.header_h), part("table-header"))
    body_top = top + layout.header_h
    if spec.zebra:
        at = body_top
        for index, height in enumerate(layout.row_h):
            if index % 2 == 0:
                _rect_part(doc, group_id, (0.0, at, layout.w, height), part("table-stripe"))
            at += height
    if spec.header is not None:
        _part(
            doc,
            inkex.Line.new((0.0, body_top), (layout.w, body_top)),
            prefix="line",
            category="connector",
            parent=group_id,
            style=None,
            classes=part("table-rule"),
        )
        _draw_row(
            doc,
            group_id,
            layout,
            spec.col_align,
            layout.header_cells,
            top,
            layout.header_h,
            part("table-header-text"),
        )
    at = body_top
    cell_class = part("table-cell")
    for index, cells in enumerate(layout.body_cells):
        _draw_row(doc, group_id, layout, spec.col_align, cells, at, layout.row_h[index], cell_class)
        at += layout.row_h[index]
    return layout


def add_table(
    doc: Document,
    *,
    rows: list[list[str]],
    header: list[str] | None = None,
    title: str = "",
    x: float | None = None,
    y: float | None = None,
    col_align: list[str] | None = None,
    max_col_width: float = _DEFAULT_MAX_COL,
    zebra: bool = True,
    x_pad: float | None = None,
    parent: str | None = None,
    name: str | None = None,
    style: Style | None = None,
    styles: list[str] | None = None,
    themed: bool = True,
) -> PlacedTable:
    """Add a table — columns measured from the cells, rows sized to what wrapped into them.

    Nothing about the geometry is a parameter: each column is as wide as the widest thing in it
    (up to ``max_col_width``, past which the cell WRAPS and its row grows), and a column whose
    every body cell reads as a number right-aligns itself, because that is the only way a column
    of numbers can be compared down the page. Say ``col_align`` to overrule that.

    Omit ``x``/``y`` to stack the table under the last facade in the same parent.
    """
    if max_col_width < _MIN_COL_WIDTH:
        raise InvalidArgument(f"a table column needs at least {_MIN_COL_WIDTH:g} to wrap text into")
    if x_pad is not None and x_pad < 0:
        raise InvalidArgument("a table's cell padding cannot be negative")
    columns = table_columns(rows, header)
    aligns = resolve_col_align(rows, columns, col_align)
    spec = TableSpec(
        rows=tuple(tuple(str(cell) for cell in row) for row in rows),
        header=None if header is None else tuple(str(cell) for cell in header),
        title=title,
        col_align=aligns,
        zebra=zebra,
        max_col_width=max_col_width,
        x_pad=x_pad,
        auto=x is None and y is None,
    )

    gap = _token(serving_theme(doc, "table"), "--gap-node", 24.0)
    parent_element = doc.resolve_parent(parent)
    stacked = _stack_below(parent_element, gap)
    # The translate below is read in the PARENT's frame, so the derived stack position crosses
    # into it; an x/y the caller gave is already in that frame and is written verbatim.
    auto_x, auto_y = auto_origin(parent_element, stacked, _ORIGIN)
    at_x = x if x is not None else auto_x
    at_y = y if y is not None else auto_y

    group = inkex.Group()
    ref = _place_facade(
        doc,
        group,
        prefix="table",
        category="container",
        prim="table",
        role="table",
        parent=parent,
        name=name,
        style=style,
        styles=styles,
        themed=themed,
    )
    group.set("transform", f"translate({_num(at_x)},{_num(at_y)})")
    with _facade_body(doc, ref):
        _write_table_spec(group, spec)
        layout = _build_table(doc, group, spec)
    return PlacedTable(
        ref=ref,
        x=at_x,
        y=at_y,
        w=layout.w,
        h=layout.h,
        columns=list(layout.columns),
        col_align=list(spec.col_align),
        auto=spec.auto,
    )


def edit_table(
    doc: Document,
    target: str,
    *,
    rows: list[list[str]] | None = None,
    header: list[str] | None = None,
    title: str | None = None,
    col_align: list[str] | None = None,
    zebra: bool | None = None,
) -> TableEdit:
    """Edit a table by its SPEC — new cells, a new header, a new title — and re-measure it.

    The children are thrown away and re-derived rather than patched, for the same reason a chart's
    are: the layout is a pure function of the cells, and nothing outside the table may reference
    its internals. The GROUP survives untouched, so its id, its classes and its position all mean
    what they meant before.

    New rows do NOT silently re-align the columns — an alignment, once resolved, is part of the
    spec and may have been the caller's. Pass ``col_align`` to change it. An empty ``header``
    list removes the header row.
    """
    group = doc.resolve(target)
    spec = read_table_spec(group)
    if spec is None:
        raise InvalidArgument(f"{target!r} is not a table (no {_TABLE_ATTR} spec)")
    new_rows = (
        spec.rows if rows is None else tuple(tuple(str(cell) for cell in row) for row in rows)
    )
    # An empty header list is how a header is REMOVED; a missing one leaves it alone.
    new_header = spec.header if header is None else (tuple(str(cell) for cell in header) or None)
    columns = table_columns(new_rows, new_header)
    if col_align is not None or columns != len(spec.col_align):
        aligns = resolve_col_align(new_rows, columns, col_align)
    else:
        aligns = spec.col_align
    updated = TableSpec(
        rows=new_rows,
        header=new_header,
        title=title if title is not None else spec.title,
        col_align=aligns,
        zebra=zebra if zebra is not None else spec.zebra,
        max_col_width=spec.max_col_width,
        x_pad=spec.x_pad,
        auto=spec.auto,
    )
    _write_table_spec(group, updated)
    _build_table(doc, group, updated)
    return TableEdit(
        ref=NodeRef(id=str(group.get_id()), tag=str(group.TAG), name=getattr(group, "label", None)),
        children=sum(1 for child in group if isinstance(child.tag, str)),
        col_align=list(aligns),
    )


# --- callout cards -----------------------------------------------------------

# The accent bar down a card's left edge, in user units. Fixed rather than themed: it is a
# thickness the eye reads as "edge", and a card whose accent scaled with the type would stop
# reading as the same component across two themes.
_ACCENT_W = 4.0
_CARD_TITLE_SIZE = 12.0
_CARD_BODY_SIZE = 11.0
# The clear air between a card's title and its body — larger than the lead between two lines of
# the same paragraph, which is what makes them read as two things rather than one.
_CARD_TITLE_GAP = 5.0
_DEFAULT_CARD_W = 240.0
_MIN_CARD_W = 60.0


@dataclass(frozen=True, slots=True)
class CardSpec:
    """What a callout card says and how wide it says it. Position lives in the transform.

    ``themed`` is the dressing intent it was built with — see
    :class:`~svg_mcp.ops.diagram.NodeSpec`.
    """

    title: str
    body: str
    kind: str
    width: float
    auto: bool
    themed: bool = True


@dataclass(frozen=True, slots=True)
class PlacedCard:
    """A new card: its handle, the box it took, and how many lines its body wrapped to."""

    ref: NodeRef
    x: float
    y: float
    w: float
    h: float
    lines: int
    auto: bool


@dataclass(frozen=True, slots=True)
class CardEdit:
    """A patched card: its handle, its body's line count, and the height it re-derived."""

    ref: NodeRef
    lines: int
    h: float


def read_card_spec(element: BaseElement) -> CardSpec | None:
    """The card spec stored on ``element``, or None if it is not a card (or is corrupt)."""
    raw = element.get(_CARD_ATTR)
    if raw is None:
        return None
    try:
        spec = json.loads(raw)
        return CardSpec(
            title=str(spec["title"]),
            body=str(spec.get("body", "")),
            kind=str(spec["kind"]),
            width=float(spec["width"]),
            auto=bool(spec.get("auto", False)),
            themed=bool(spec.get("themed", True)),
        )
    except (ValueError, TypeError, KeyError):
        return None


def _write_card_spec(element: BaseElement, spec: CardSpec) -> None:
    element.set(
        _CARD_ATTR,
        json.dumps(
            {
                "title": spec.title,
                "body": spec.body,
                "kind": spec.kind,
                "width": spec.width,
                "auto": spec.auto,
                "themed": spec.themed,
            },
            separators=(",", ":"),
        ),
    )


@dataclass(frozen=True, slots=True)
class CardLayout:
    """A card measured: both blocks of text already wrapped, and the height they add up to."""

    title_lines: tuple[str, ...]
    body_lines: tuple[str, ...]
    title_h: float
    body_h: float
    text_x: float
    text_w: float
    pad: float
    w: float
    h: float


def _card_layout(doc: Document, spec: CardSpec) -> CardLayout:
    """Measure a card: the text wraps into what the accent and the padding leave of its width.

    The HEIGHT is derived, never given — a card is exactly as tall as the words in it, so nobody
    has to guess a height and nobody ends up with a paragraph hanging out of a box.
    """
    theme = serving_theme(doc, spec.kind)
    font = _label_font(theme)
    pad = _token(theme, "--pad-node", _DEFAULT_PAD)
    text_w = max(_MIN_MAX_WIDTH, spec.width - _ACCENT_W - 2 * pad)
    title_lines = tuple(wrap_words(spec.title, max_width=text_w, font=font, size=_CARD_TITLE_SIZE))
    body_lines = tuple(wrap_words(spec.body, max_width=text_w, font=font, size=_CARD_BODY_SIZE))
    title_h = max(
        (measure_label(line, font, _CARD_TITLE_SIZE)[1] for line in title_lines),
        default=_CARD_TITLE_SIZE,
    )
    body_h = max(
        (measure_label(line, font, _CARD_BODY_SIZE)[1] for line in body_lines),
        default=_CARD_BODY_SIZE,
    )
    title_block = _stack_h(len(title_lines), title_h)
    body_block = _stack_h(len(body_lines), body_h)
    gap = _CARD_TITLE_GAP if title_lines and body_lines else 0.0
    return CardLayout(
        title_lines=title_lines,
        body_lines=body_lines,
        title_h=title_h,
        body_h=body_h,
        text_x=_ACCENT_W + pad,
        text_w=text_w,
        pad=pad,
        w=spec.width,
        h=2 * pad + title_block + gap + body_block,
    )


def _stack_h(count: int, line_h: float) -> float:
    """How tall ``count`` lines of ``line_h`` are, leading included; nothing is nothing."""
    return 0.0 if count == 0 else count * line_h + (count - 1) * _LINE_LEAD


def _build_callout_card(doc: Document, group: BaseElement, spec: CardSpec) -> CardLayout:
    """Draw (or re-draw) a card: its body, its accent, its title and its words. Idempotent.

    The card rect is bare, so the KIND class on the group paints it — the same way a callout's
    card is painted, and the reason a card and a callout of the same kind cannot drift apart. The
    accent is drawn after it, so it covers the border it sits on rather than being halved by it.
    """
    for child in list(group):
        if isinstance(child.tag, str):
            child.delete()
    layout = _card_layout(doc, spec)
    group_id = str(group.get_id())

    # A card not wearing its kind's hook was built with themed=False; a rebuild that dressed it
    # anyway would quietly overturn that decision, so the absence is honoured (as in a chart).
    dressing = worn_theme(doc, group, spec.kind, "container--callout-card", "container")

    def part(suffix: str) -> list[str]:
        return _part_class(doc, suffix, dressing) if dressing is not None else []

    _rect_part(doc, group_id, (0.0, 0.0, layout.w, layout.h), ())
    _rect_part(doc, group_id, (0.0, 0.0, _ACCENT_W, layout.h), part("card-accent"))
    cursor = layout.pad
    title_class = part("card-title")
    for line in layout.title_lines:
        _text_part(doc, group_id, line, (layout.text_x, cursor + layout.title_h / 2.0), title_class)
        cursor += layout.title_h + _LINE_LEAD
    if layout.title_lines and layout.body_lines:
        cursor += _CARD_TITLE_GAP - _LINE_LEAD
    body_class = part("card-body")
    for line in layout.body_lines:
        _text_part(doc, group_id, line, (layout.text_x, cursor + layout.body_h / 2.0), body_class)
        cursor += layout.body_h + _LINE_LEAD
    return layout


def _card_words(title: str, body: str) -> None:
    """A card with neither a title nor a body is an empty box; refuse it rather than draw it."""
    if not title.strip() and not body.strip():
        raise InvalidArgument(
            "a callout card needs a title (or at least a body) — a card with no words in it is "
            "an empty rectangle, which `add_rect` already draws"
        )


def add_callout_card(
    doc: Document,
    *,
    title: str,
    body: str = "",
    kind: str = "info",
    x: float | None = None,
    y: float | None = None,
    width: float = _DEFAULT_CARD_W,
    parent: str | None = None,
    name: str | None = None,
    style: Style | None = None,
    styles: list[str] | None = None,
    themed: bool = True,
) -> PlacedCard:
    """Add a standalone card: an accent bar, a title, and a wrapped body — and NO leader.

    This is the note that is about the picture rather than about one node in it; use
    ``add_callout`` when there is something to point AT, since a leader that re-anchors itself is
    the whole reason that call exists.

    The card is as tall as the words it wrapped to, and its ``kind`` (``note``, ``info``,
    ``warning``, ``success``, ``danger``, or any role a resident theme serves) paints both the
    card and the accent down its left edge. Omit ``x``/``y`` to stack it under the last facade in
    the same parent.
    """
    if width < _MIN_CARD_W:
        raise InvalidArgument(
            f"a callout card needs at least {_MIN_CARD_W:g} of width to fit an accent, its "
            "padding and a word between them"
        )
    _card_words(title, body)
    spec = CardSpec(
        title=title, body=body, kind=kind, width=width, auto=x is None and y is None, themed=themed
    )

    gap = _token(serving_theme(doc, kind), "--gap-node", 24.0)
    parent_element = doc.resolve_parent(parent)
    stacked = _stack_below(parent_element, gap)
    # As in ``add_table``: the derived stack position is a world y, the caller's x/y is not.
    auto_x, auto_y = auto_origin(parent_element, stacked, _ORIGIN)
    at_x = x if x is not None else auto_x
    at_y = y if y is not None else auto_y

    group = inkex.Group()
    ref = _place_facade(
        doc,
        group,
        prefix="callout-card",
        category="container",
        prim="callout-card",
        role=kind,
        parent=parent,
        name=name,
        style=style,
        styles=styles,
        themed=themed,
    )
    group.set("transform", f"translate({_num(at_x)},{_num(at_y)})")
    with _facade_body(doc, ref):
        _write_card_spec(group, spec)
        layout = _build_callout_card(doc, group, spec)
    return PlacedCard(
        ref=ref,
        x=at_x,
        y=at_y,
        w=layout.w,
        h=layout.h,
        lines=len(layout.body_lines),
        auto=spec.auto,
    )


def edit_callout_card(
    doc: Document,
    target: str,
    *,
    title: str | None = None,
    body: str | None = None,
    kind: str | None = None,
) -> CardEdit:
    """Edit a card by its SPEC — new words or a new kind — and re-wrap and re-size it around them.

    A new ``kind`` swaps the role class the group wears, which repaints the card AND its accent
    together; the card keeps its corner either way, because where a card sits is a composition
    decision and re-deriving its height is not entitled to move it.
    """
    group = doc.resolve(target)
    spec = read_card_spec(group)
    if spec is None:
        raise InvalidArgument(f"{target!r} is not a callout card (no {_CARD_ATTR} spec)")
    updated = CardSpec(
        title=title if title is not None else spec.title,
        body=body if body is not None else spec.body,
        kind=kind if kind is not None else spec.kind,
        width=spec.width,
        auto=spec.auto,
        themed=spec.themed,
    )
    _card_words(updated.title, updated.body)
    _rekind(doc, group, spec.kind, kind, themed=spec.themed)
    _write_card_spec(group, updated)
    layout = _build_callout_card(doc, group, updated)
    return CardEdit(
        ref=NodeRef(id=str(group.get_id()), tag=str(group.TAG), name=getattr(group, "label", None)),
        lines=len(layout.body_lines),
        h=layout.h,
    )
