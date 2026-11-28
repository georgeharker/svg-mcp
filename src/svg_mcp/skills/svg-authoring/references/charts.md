# Charts

- For ANY plot of numbers — bar, line, donut, scatter, histogram, sparkline, radar — use
  `add_chart(kind, data)`,
  NOT hand-placed rects and polylines. It picks round tick values, MEASURES the margins from
  the tick labels so nothing is clipped, and paints each series from the theme's palette in
  series order. It works with no theme loaded: charts are the one category the bundled default
  styles outright.
- `axes=` frames the plot when the defaults won't do: `y_min`/`y_max` (and `x_min`/`x_max` on a
  numeric x) pin the scale so two charts compare honestly — data outside a pinned range is
  clipped to the plot, never drawn over the axis; `ticks`/`x_ticks` take a target count or an
  explicit list; `tick_format` writes the labels as plain/percent/currency/si/fixed (a closed
  vocabulary, no format strings); `gridlines` and `tick_marks` and `x_tick_rotate` do the rest.
- `scale="log"` is available on the value axis, for strictly positive data. Bars on it measure
  from the axis minimum rather than from zero, because a log axis has no zero to measure from.
- `axes.reference_lines=[{value, label, to, axis, kind}]` draws the thresholds the data is READ
  AGAINST — a target, a budget, a tolerance band (`to` makes it a band). Lines are drawn over the
  data, bands behind it; one off the axis is dropped, a band clamps to it.
- Per-kind, in `data`: `value_labels` writes each number on its mark (bars flip the label inside
  when the plot edge is close; nothing is ever clipped); `stacked` sums the series per category
  (negatives stack DOWNWARD from zero, the axis follows the TOTALS, `stack_total_labels` writes
  them); `orientation="horizontal"` swaps the axes so long category names get the left margin —
  which is then measured from THEM; `order` = "value_desc"/"value_asc"/"label"/an explicit list;
  `step="post"|"pre"|"mid"` on a line draws a staircase; a donut takes `center_text`/
  `center_subtext` (the KPI-in-the-hole idiom), `slice_labels` and `start_angle`.
- The parity batch, in one line each: marks are circle/square/diamond/triangle/tri_down/plus/
  cross/star (`open` draws them hollow, `markevery` thins them, `sizes` + `marker_scale` makes
  them bubbles sized by AREA); `axes.minor`/`minor_gridlines` subdivide the ruler (2..9 mantissas
  on a log axis) and `tick_direction`/`y_tick_rotate` finish it; `invert_x`/`invert_y` turn a
  scale round and `zero_spine` stands the axis line at 0 with the labels left at the edge;
  `bands=[{between:[a,b], label}]` on a line fills between two identically-sampled series;
  `waterfall` (+ `total_label`) walks a running total with dashed connectors and `normalized`
  turns a stack into shares of 100; `kind="histogram"` takes RAW values and bins them itself
  (`bins` = a count or explicit edges) into contiguous bars.
- `kind="radar"` compares PROFILES over three or more named `axes` (clockwise from 12 o'clock,
  one shared radial scale, so only commensurable spokes belong on one wheel): `fill` washes each
  polygon, `rings`/`r_max` are the ruler (`r_max` pins it so two radars compare), `ring_labels`
  writes it up the 12 o'clock spoke. Negative values are REFUSED — a radius has no other
  direction to run in; that data is a bar chart. `axes=` (the cartesian frame) is ignored here.
- Still out of scope: statistical transforms, a second y axis, colormaps. If you need one, the
  answer is a different picture, not a hack.
- `edit_chart(target, data=…)` re-derives the whole plot in place, keeping the group's id,
  classes and position. `get_params` hands the data back exactly as it went in.
