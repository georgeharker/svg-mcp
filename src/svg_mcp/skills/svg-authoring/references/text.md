# Text

- `add_text(x, y, content)` for a text block; `add_text_run` appends `<tspan>` runs. For multiple
  LINES, append runs with an absolute `x` and an incremental `dy` (e.g. `dy="1.2em"`) per line.
  `add_text_on_path` flows text along an existing path (pass the path's id).
- To change wording/position/style of EXISTING text, use `edit_text(target, content=…)` IN PLACE —
  do not delete + re-add (that drops the node's id/clip/filters/z-order).
- Do NOT estimate text width by chars × size — call `measure_text` (below); it's why it exists.
- Fonts: `list_fonts()` returns the family names installed on this machine — pick one and set it
  via `style["font-family"]`. `font-size`, `font-weight` (`bold`/`700`), `font-style`
  (`italic`), `text-anchor` (start/middle/end), `letter-spacing`, `word-spacing`,
  `text-decoration`, `dominant-baseline` (vertical align), and `paint-order` are all settable on
  `style`.
- `measure_text(content, style)` returns `{width, height}` in user units from the font's own
  metrics — use it to fit/center text and size boxes BEFORE rendering (no render round-trip).
  inkex does not shape glyphs, so geometric bbox queries return empty for text; `measure_text`
  fills that gap (Latin-accurate; no kerning/complex shaping).
- `text_to_path(target)` converts a text/textPath node into a real outlined `<path>` (pure-Python
  via fontTools, honoring family/weight/italic/anchor and text-on-path). Use it to make output
  font-independent (renders identically anywhere) or to then manipulate the glyph geometry.
