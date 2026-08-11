# The default theme

The house style every document falls back on. It is a vocabulary of **roles**, not a coat of
paint: nothing is restyled until you name a role.

## Prefer roles over raw styles

Pass `role=` to a constructor rather than an inline `style`. A role survives a theme swap, a
variant switch, and someone else's edit; an inline fill does not — it is pinned, and stays put
when everything around it changes. Reach for `style` only for a genuine one-off.

## Node kinds

`service` · `datastore` · `queue` · `external` · `decision` · `note`

Each names both a shape and a paint. The manifest's `[kinds]` says which primitive to draw —
service is a squircle, queue a pill, decision a polygon, the rest rects — so a facade drawing
`role="service"` gets the right outline as well as the right fill. They are separated by value
and weight rather than hue, so the picture still reads in greyscale.

## Edge kinds

`data` (solid) · `control` (dashed) · `dependency` (finely dotted)

Use the dash pattern to carry meaning: a solid line moves something, a dashed one triggers
something, a dotted one merely relates two things. Do not add a fourth pattern — add a marker.

## Container kinds

`cluster` · `zone` · `swimlane`

A container is a box drawn *behind* a set of nodes — scenery, not another node, so all three
tint with the ink rather than taking a fill of their own and none of them competes with what it
encloses. `cluster` (dashed, barely there) says "these belong together"; `zone` (a solid faint
tint, no border) marks a region; `swimlane` (tinted and outlined) says who owns a band of the
picture. `add_diagram_container` fits the box to its members and `reflow` re-fits it after they
move, so the grouping survives a layout pass.

## Annotation

`add_legend` is **generated**: called with no entries it scans the document and gives you one row
per node kind, edge kind, container kind and chart series actually in use — each swatch wearing
that kind's real class, so a variant switch recolours the key along with the picture. Add a kind
later and the key does not know: call `edit_legend(regenerate=true)`. `add_callout` points a card
at a node by **id**, not by position — `note` (neutral) · `info` · `warning` · `success` ·
`danger` — and its leader is re-derived by every `reflow` and `layout_diagram`, so the note stays
attached to the thing it is about however far that thing moves.

## Text roles

`title` · `subtitle` · `label` · `caption` · `code`

A four-step size ramp plus one monospace role. `label` is the body size of a diagram; `caption`
is the only role that mutes its ink, because that is what "secondary" means here.

## Tokens

Everything tunable is a `:root` token — palette, stroke weights, dash patterns, type sizes, and
the layout spacings (`--pad-node`, `--gap-node`, `--gap-rank`, `--radius`). Copy this theme's
directory and edit the tokens to make a house style of your own; the `dark` variant shows how
far a token overlay alone can take you.

## Charts

`bar` (plain or grouped) · `line` (multi-series, optional points and area) · `donut` · `scatter`
· `sparkline`

`add_chart` takes the data and derives everything else: ticks on a 1/2/5 ladder, margins measured
from the tick labels those produce, and the marks themselves. **Series order is the house
palette** — series 1 is `--series-1`, series 2 is `--series-2`, and so on to eight before it
repeats — so two charts of the same series in the same document agree on colour without anyone
saying so. Order the series meaningfully and that agreement is free; shuffle them between charts
and you have quietly told the reader they are looking at different things.

Scales are linear and the x axis is either categorical (`bar`) or numeric (`line`, `scatter`).
There is no log scale, no stacking, no error bar: those are different promises about what the
picture means, and a facade that faked them would be worse than not having them.

A `sparkline` is deliberately bare — no axes, no ticks, no title, just the shape of the trend,
height-normalized. It is meant to sit inline next to the number it qualifies, and it will pair
with a stat panel once there is one.
