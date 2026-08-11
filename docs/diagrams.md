# Diagrams, charts & annotations — declare, don't draw

The facades in this page share one idea: **you state what things are and how they relate;
geometry is derived.** Every facade is a group carrying its spec in a `data-*` attribute
(`get_params` reads it back), styled through the [theme system](./themes.md), and editable
after the fact — nothing is baked that can't be re-derived.

The zero-coordinate happy path:

```
api = add_diagram_node(kind="service",   label="API Gateway")
db  = add_diagram_node(kind="datastore", label="Postgres")
add_diagram_edge(source=api.id, target=db.id, kind="data", label="reads/writes")
add_diagram_container(members=[api.id, db.id], kind="zone", label="backend")
layout_diagram(algorithm="layered", direction="LR")
```

![architecture example](./img/facade-diagram.png)

## Nodes

`add_diagram_node(kind, label, ...)` — the serving theme's `[kinds]` manifest picks the
shape (service → squircle, queue → pill, decision → diamond, ...); the box sizes itself to
its label plus the theme's `--pad-node`; omitted positions stack below the previous node.
Built-in kinds: `service`, `datastore`, `queue`, `external`, `decision`, `note` — themes
add more by declaring a role and a `[kinds]` entry.

## Edges

`add_diagram_edge(source, target, kind, route, label)` — edges store **what they connect,
not where they run**. Sides are chosen from the geometry (`auto`) or named (`N/S/E/W`);
several edges sharing a face fan out across it instead of piling on one point; routes are
`orthogonal` (right angles, rounded elbows), `straight`, or `spline`; labels sit on the
longest segment with a canvas-colored halo. Built-in kinds: `data` (solid), `control`
(dashed), `dependency` (dotted).

## Containers

`add_diagram_container(members, kind, label)` — a box drawn *behind* its members (a
sibling, not a parent: nothing is reparented). Fitted automatically around the members
plus `--pad-container` and label headroom, or fixed by giving all of x/y/width/height.
Kinds: `cluster`, `zone`, `swimlane`.

## reflow — the one call after you move things

```
translate_node("db", dx=60, dy=0)
reflow()
```

`reflow()` re-routes every edge, re-anchors every callout leader, and re-fits every
fitted container from current positions. It is **explicit by design** — moves stay local
operations, and nothing rearranges behind your back. Edges/callouts whose target vanished
are reported, not guessed at.

## layout_diagram — opt-in automatic placement

`layout_diagram(algorithm, direction)` places every diagram node in scope, then reflows:

- `layered` (default) — Sugiyama-style: cycle-safe ranking, crossing-minimized orders,
  nodes sharing a container kept adjacent. For flows and architectures.
- `tree` — parents centered over children. For genuine hierarchies.
- `grid` — document order into rows. Ignores edges.

A layout pass moves **every** node in scope — hand placement is preserved by *not*
calling it, never by exempting individual nodes.

## Charts

`add_chart(kind, data, ...)` — data-parametric presentation charts: `bar` (plain/grouped),
`line` (points/area options), `donut`, `scatter`, `sparkline`. Scales are linear with
nice-number ticks; margins are **measured from the actual tick labels**; series take the
theme's `--series-1..8` palette in order. `edit_chart` replaces the data and re-derives
the whole picture while the group keeps its id, classes, and position.

![chart sheet](./img/facade-charts.png)

Deliberately out of scope: log scales, stacking, subplots, statistical transforms — data
that doesn't fit a tool-call argument belongs to a plotting library; `import_svg` its
output instead.

## Annotations

- `add_legend()` — **generated** from what the document actually uses: node, edge, and
  container kinds plus chart series, each swatch wearing its real theme class (edge kinds
  draw as lines with their true dash patterns). A variant switch recolors the legend for
  free. `edit_legend(regenerate=true)` re-scans after you add kinds.
- `add_callout(target, text, kind)` — a wrapped-text card with a leader line pointing at
  a node **id**; the leader re-anchors on every `reflow`, so annotations survive layout
  changes. Kinds: `note`, `info`, `warning`, `success`, `danger`.
- `add_callout_card(title, body, kind)` — a standalone card with a kind-colored accent
  bar; same kind vocabulary, no leader.
- `add_table(rows, header, title)` — column widths measured from content, numeric columns
  right-aligned automatically, header wash + zebra stripes from theme classes, cells
  word-wrapped at `max_col_width`.

![annotations](./img/facade-annotate.png)

## Editing after the fact

Every facade has an `edit_*` twin that patches the spec and re-derives (`edit_diagram_node`,
`edit_diagram_edge`, `edit_diagram_container`, `edit_chart`, `edit_legend`, `edit_callout`,
`edit_table`, `edit_callout_card`), and `get_params` returns any facade's spec under the
same names. Facade groups respond to the ordinary tools too — translate, restyle, apply
styles, delete — because they are ordinary document nodes.
