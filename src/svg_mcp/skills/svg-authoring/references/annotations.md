# Legends, callouts and tables

## Legends and callouts

- Finished a diagram or a chart? `add_legend()` with NO entries generates the key from what the
  document actually draws — every node/edge/container kind and chart series in use, each swatch
  wearing that kind's real class, so a variant switch recolours the key too. Add a kind later
  and `edit_legend(target, regenerate=true)` picks it up.
- To annotate a node, `add_callout(target, text, kind)` — "note"/"info"/"warning"/"success"/
  "danger". It wraps the text, places the card off the roomiest side, and draws a LEADER that
  `reflow`/`layout_diagram` re-anchor, so the note stays attached however far the node moves.
  Do NOT hand-place a text box beside a node: that annotation dies at the next layout pass.
- To annotate ONE number of a chart, add `datum={series, index}` and target the chart: the leader
  lands on that bar/point/slice and is re-derived from the spec, so it follows the mark through
  an `edit_chart` that changes the data.
- For a note about the PICTURE rather than about one node — a takeaway, a caveat, a status —
  `add_callout_card(title, body, kind)`: the same card, no leader, height derived from the words.

## Tables

- For ANY grid of values use `add_table(rows, header)`, NOT hand-placed text. It measures every
  column to the widest cell in it, wraps a cell past `max_col_width` and grows its row, and
  right-aligns a column whose every body cell is a number. Guessed x positions are how a
  generated table ends up with one column written over the next.
- `edit_table(target, rows=…)` re-derives it in place, keeping the group's id and position.
