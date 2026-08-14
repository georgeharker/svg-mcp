# Gallery

A tour of what svg-mcp can draw. Every image here was produced entirely from tool calls and
rendered with the built-in renderer — no hand-written SVG. See the [Cookbook](cookbook.md) for the
full, copy-pasteable code behind these.

## A composed icon

![A glowing app icon built from a gradient squircle body, a boolean hexagon ring, a superellipse core, and offset-path glows.](img/icon.png){width=420}

This single icon exercises most of the library at once:

- a **squircle** body filled with a linear **gradient**;
- a hexagonal **ring** made by `boolean` *difference* of two **rounded polygons**;
- a **superellipse** core;
- soft **glows** made by `offset_path` (grow the shape) + `apply_blur`, tucked behind the crisp art;
- **concentric** outlines, each an `offset_path` of the ring at a different distance.

## Parametric shapes

![Squircle, rounded hexagon, superellipse, and pill.](img/shapes.png)

First-class, re-editable primitives — each stores its parameters, so an `edit_*` call re-derives it
without you touching path data:

- **`add_squircle`** — iOS/Figma corner-smoothed rounded rectangle (Apple's app-icon shape).
- **`add_rounded_polygon`** — the same corner smoothing generalized to N sides.
- **`add_superellipse`** — the Lamé curve `|x/rx|^n + |y/ry|^n = 1`; the exponent morphs it from
  diamond → ellipse → squircle → rectangle.
- **`add_pill`** — a stadium with fully rounded ends.

## Boolean operations

![Union, difference, intersection, and exclusion of a square and a circle.](img/booleans.png)

`boolean` combines shapes with **union / difference / intersection / exclusion**, realized with
native SVG constructs (clip, mask, evenodd compound path) — no extra dependency. Operands may be
composite groups. An even-width bezel is simply `difference` of an outer and inner squircle.

## Path offset

![Concentric offsets of a squircle (left) and a freeform path (right).](img/offset-path.png)

`offset_path` makes a parallel curve at a signed distance — concentric rings, even-width bezels,
glow outlines, stroke outlining. A squircle/pill/rounded-polygon is offset **exactly** by
regenerating its parameters (and stays editable); any other path uses an analytic cubic-Bézier
offset with round/miter/bevel joins. Positive distance grows, negative insets.

## Variable-width strokes

![Two calligraphic variable-width strokes that swell and taper.](img/variable-width.png)

SVG strokes are constant width, so swelling/tapering lines — calligraphy, brush strokes, tapered
arrows — are drawn as a **fill**. `add_variable_width_path` expands a centerline with a per-vertex
width into a filled ribbon, with butt/round caps and optional cubic (Catmull-Rom) smoothing.

## Arrowheads

![Arrowhead presets — triangle, barbed, stealth, diamond, open, dot.](img/arrows.png)

`define_arrow_marker` builds an endpoint head from a preset (triangle / barbed / stealth / diamond /
open / dot); `apply_marker` attaches it at the start, middle, or end. Heads are `orient="auto"`, so
they follow the path's tangent — note each arrow above points along its curve.

## Diagrams

![A themed box-arrow diagram: services, a datastore, a queue, containers, and routed edges.](img/facade-diagram.png)

Box-arrow diagrams are **declared, not drawn**: `add_diagram_node` places a labeled, themed box
(kind picks the shape, role picks the style), `add_diagram_edge` connects two nodes *by id* and
routes itself around the boxes, `add_diagram_container` frames a group by reference. Move a node
and `reflow` re-routes every edge. `layout_diagram` computes positions outright — grid, tree, or
layered (Sugiyama) — with pinning for the boxes you've placed by hand. The look comes from a
[theme](themes.md); switch it (or its dark variant) and the whole diagram restyles.

## Charts

![Bar, line, donut, scatter, histogram, radar, and waterfall charts sharing one theme.](img/facade-charts.png)

`add_chart` renders seven kinds — bar (grouped / stacked / waterfall), line (steps, bands,
markers), donut, scatter (bubbles), histogram, sparkline, and radar — from data, with
matplotlib-grade axis control: tick formats, minor ticks, log scales, reference lines, inverted
axes. Every mark carries the theme's series palette, and each datum stays addressable for
callouts.

## Legends, callouts, tables

![A legend, an anchored callout, a measured table, and a callout card.](img/facade-annotate.png)

The annotation family: `add_legend` builds its entries by scanning what's actually on the canvas,
`add_callout` anchors a wrapped note to a node — or to a single chart datum — and re-anchors on
`reflow`, `add_table` measures its columns and right-aligns numbers, `add_callout_card` is a
titled, accent-striped panel. All of them dress from the resident theme like everything else.

## Ingesting a graph

![svg-mcp's own module graph, ingested and laid out in one call.](img/facade-architecture.png)

`add_diagram_graph` turns a graph you already have — nodes, edges, groups, as JSON straight from a
code-index export — into a laid-out, routed, themed diagram in **one call**. The self-portrait
above is svg-mcp's own module graph: seven `collapse` groups, boxes scaled by symbol count, no
coordinates anywhere in the input. See [diagrams.md](diagrams.md) for the spec it consumes.
