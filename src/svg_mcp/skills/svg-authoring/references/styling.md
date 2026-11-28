# Styling, paint and reusable resources

## Styling and paint

- `style` is a structured object keyed by CSS property names: `fill`, `stroke`, `stroke-width`,
  `opacity`, `fill-opacity`, `stroke-opacity`, `stroke-dasharray`, `stroke-linecap`,
  `stroke-linejoin`, plus typography (`font-family`, `font-size`, `font-weight`, `font-style`,
  `text-anchor`, `letter-spacing`, …). Use the hyphenated CSS names: they are what the schema
  accepts. `font-size` also takes a bare number (px). Unknown/misspelled keys are REJECTED with
  an error (not dropped), so set the whole style in one call and trust it stuck.
- Colors accept hex (`#ff0000`), `rgb()/rgba()`, CSS names (`tomato`), or `none`.
- A fill/stroke may also reference a defined resource by id `url(#<id>)`, the shorthand `@<name>`,
  or `url(#<name>)` — all resolve to the defined gradient/pattern. Define the resource first.
- `add_line`/`add_polyline` (and bulk `add_lines`) auto-get a 1px black stroke when none is set,
  so they're never invisible; pass `stroke` (or `stroke:"none"`) to override. `fill` defaults to
  black, so closed shapes show — but an unstyled OPEN `add_path`/`add_paths` (no fill region) can
  still be invisible; give it a stroke.
- `restyle` MERGES by default — it only changes the properties you pass, keeping the rest. Pass
  `replace=true` to discard the node's current style and set exactly what you provide.

## Reusable resources (defs)

- `define_linear_gradient`/`define_radial_gradient`/`define_pattern` return an id — use it as a
  fill: `style={"fill": "url(#<id>)"}` or `@<name>`.
- `define_style(name, style)` creates a named CSS class; `apply_styles(target, [name])` APPENDS it
  to the node's ordered class list (`remove_styles` unlinks). A node's inline `style` is its
  EXPLICIT style and beats every class rule.
- `define_clip`/`define_mask`/`define_symbol`/`define_marker` take `content`: a list of EXISTING
  node ids, which are MOVED into the new resource. So: create the shapes first, then define the
  resource from them, then `apply_clip`/`apply_mask`/`apply_marker` (or `add_use` for a symbol).
