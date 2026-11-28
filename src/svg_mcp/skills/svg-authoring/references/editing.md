# Targeting, coordinates and editing

## Orienting

- `current_context()` / `list_documents()` — the session's open document ids. Call one to
  re-anchor after a long conversation when you may have lost track, then `outline` the one you
  mean.
- `outline` — the document tree (names, kinds, ids); `describe_node(target)` — everything about
  one node (kind, world bbox, computed style, local + composed transform, parent) in one call.
- `list_resources()` — the gradients/patterns/filters/clips/masks/markers/symbols and named
  styles already defined, so you know what you can reference/reuse before defining new ones.
- `list_styles()` — every attachable style: each resident theme's classes (with descriptions)
  plus doc-local ones. `describe_document` also reports resident themes and their routing.
- `find(name=…)` to locate nodes, `get_subtree(target)` to read a branch, `get_bbox`/
  `get_transform`/`get_computed_style` for focused lookups.

## Targeting nodes and naming

- Every `target`/`parent`/`content` argument accepts a node's **id** (returned on creation) or
  its **friendly name** (the `name`/label you gave it).
- **Name the things you'll refer back to.** Give meaningful nodes and every group/layer a
  `name`, and reason in terms of names via `find(name=…)` / `outline`. Names are stable and
  legible; random ids (`rect_8f3a`) are easy to lose track of across a long session.
- **Keep names unique.** A name that matches more than one node is rejected (it won't silently
  pick one) — e.g. don't name a gradient and a shape the same thing.
- **A handle says which namespace it means.** A bare `target` is an id, else an EXACT name —
  never a hierarchy query, so a name containing `/` (graph imports name nodes `src/ops/x.py`)
  just works. Be explicit when you must: `id:x`, `name:x` (verbatim, slashes and all), or
  `path:ancestor/name` for the ancestor-chain query that disambiguates a duplicated name. The
  prefix is stripped once, so any literal name is expressible (`name:name:foo`).
- Omitting `parent` places a node at the document root. Pass a group/layer id to nest it.
- **Stacking:** later siblings paint on top. To order a node relative to another, use
  `reparent(target, above=<node>)` / `below=<node>` instead of counting child indices.

## Coordinates

- User units, origin at top-left, x→right, y→down. The viewBox defaults to `0 0 width height`.

## Editing geometry in place

- To change an existing node, edit it IN PLACE — do NOT delete + re-add (that drops its clip,
  mask, filters, and z-order). Each `edit_*` mirrors its `add_*` twin (same params + inline
  `style`/`transform`, only what you pass is changed): `edit_rect`/`edit_circle`/`edit_ellipse`/
  `edit_line`/`edit_polyline`/`edit_polygon`/`edit_path`. For a star/arc, `edit_star`/`edit_arc`
  re-derive the shape from its parameters; editing such a node's raw `d` (via `edit_path`) demotes
  it to a plain path. To reshape a clipPath/mask outline, target the shape inside it by id.
- To read a node, `get_params(target)` returns its current settings under the same names the
  `add_*`/`edit_*` tools use (a circle's cx/cy/r, a star's sides/outer_radius/…). `get_geometry(
  target, relative_to=...)` gives x/y/width/height in a chosen frame — `world` (default), `local`,
  `parent`, or another node's id/name — and `get_transform` adds the per-ancestor transform `stack`.

## Transforms

- Prefer the primitives, which compose onto a node's existing transform: `translate_node`,
  `rotate_node` (optional center point), `scale_node` (optional anchor point), `skew_node`.
- `apply_transform(target, "rotate(45 100 100)")` accepts any raw SVG transform string.
- Most construction tools also take a `transform` string applied at creation time.
