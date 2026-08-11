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

## Text roles

`title` · `subtitle` · `label` · `caption` · `code`

A four-step size ramp plus one monospace role. `label` is the body size of a diagram; `caption`
is the only role that mutes its ink, because that is what "secondary" means here.

## Tokens

Everything tunable is a `:root` token — palette, stroke weights, dash patterns, type sizes, and
the layout spacings (`--pad-node`, `--gap-node`, `--gap-rank`, `--radius`). Copy this theme's
directory and edit the tokens to make a house style of your own; the `dark` variant shows how
far a token overlay alone can take you.
