# Filters and effects

- Convenience: `apply_blur`, `apply_drop_shadow`, `apply_color_matrix`, `apply_color_overlay`,
  `apply_blend`, `apply_morphology`, `apply_turbulence`, `apply_displacement_map`.
- Composite effects (synthesized, no native equivalent — for icons/slides): `apply_inner_shadow`,
  `apply_outer_glow`, `apply_inner_glow`, `apply_outline` (sticker stroke), `apply_bevel`,
  `apply_gloss` (edge shine, gradient roll-off at the light angle), `apply_front_light` (broad
  3D front-face light), `apply_grain` (noise overlay). All stack and have a light `angle`.
- `get_filter(target)` describes the filter on a node — `{id, kind, params}` under the same param
  names — and `edit_filter(target, {param: value, …})` changes them ONE BY ONE in place (works for
  built-ins and composites; reads/edits without re-applying). Custom `define_filter` graphs report
  `kind="custom"` and aren't editable via `edit_filter` (rebuild them with `define_filter`).
- Each `apply_*` filter REPLACES the node's filter — a node has ONE `filter`, so the last apply
  wins (re-applying a drop shadow swaps it, it does not stack). To COMBINE effects (e.g. blur +
  drop shadow) on one node, build a single filter with `define_filter` (one `fe*` graph) and attach
  it with `apply_filter` — not by stacking convenience calls.
- For full control, `define_filter` builds an arbitrary `fe*` primitive graph; attach with
  `apply_filter`. (SVG has no native feDropShadow — `apply_drop_shadow` synthesizes one.)
