---
name: svg-authoring
description: >-
  How to author SVG with the svg-mcp tools: drawings, icons, logos, box-and-arrow diagrams,
  flowcharts, architecture pictures, charts and tables, built as a structured document and
  checked by rendering. Use it whenever a task would otherwise mean writing SVG markup by
  hand, placing boxes, arrows or bars by coordinate, or styling shapes one at a time. Covers
  the session and document contract, the build-render-look loop, the live preview, and
  references for themes, diagrams, charts, legends and tables, text, styling, effects,
  editing and export.
---

# Authoring SVG with svg-mcp

svg-mcp builds SVG as a structured, hierarchical document through tool calls, and lets you
**see** it by rendering to an image. Work in a tight loop: make a change, render, look, adjust.

Example calls here omit `session_id` and `document_id` for brevity. Every real call passes
`session_id`, and every call that acts on a document passes `document_id`.

## The contract

- **Session first.** Call `create_session()` once per drawing task and pass its `session_id`
  on every svg-mcp call. Nothing injects it for you and there is no per-connection fallback;
  a call without it is rejected, which is the cue to mint one and retry. Documents live
  under the session until `end_session`. Never end it mid-task.
- **Documents are always named.** `create_document(width, height)` returns a `document_id`.
  Pass it on every call that acts on that document. There is no "current" document, even
  when only one is open.
- **Style keys are CSS names**: `stroke-width`, `font-size`, `text-anchor`. They are the only
  spelling the schema accepts.

## Workflow

1. `create_session()`, then `create_document(width, height)`.
2. If more than a couple of calls are coming, `start_preview(document_id)` now and give the
   user the exact `url` it returns (see below).
3. Decide the approach before drawing:
   - things and relationships (architecture, flowchart, pipeline): the diagram facades, not
     rects and lines ([diagrams](references/diagrams.md));
   - numbers: `add_chart` ([charts](references/charts.md));
   - a grid of values: `add_table` ([tables](references/annotations.md));
   - a consistent look: `load_theme` first and say what things ARE with `role=`/`kind=`
     ([themes](references/themes.md)).
4. Add content. Each `add_*` returns the node's `{id, tag, name}`; give nodes you'll revisit a
   `name`. Group with `create_layer` / `create_group`.
5. `render_document` and **look at the image**. `outline` shows the tree. Iterate.
6. When the artwork is complete and the user hasn't said what to do with it, ask: save to a
   file (`export_render`: png, jpeg, webp, pdf, ps, eps, svg), hand back the source
   (`export_svg`), or leave it as the live document.

Never hand over SVG you haven't rendered and looked at.

## Live preview

- Offer it early, without being asked: as soon as a drawing will take more than a couple of
  calls, call `start_preview(document_id)` and put the exact `url` in your reply ("Live
  preview: <url>, it refreshes on every change"). Also do it whenever the user asks to see
  the work.
- The page stays on its document. When you start another document, call `start_preview`
  with its id and give the user the new URL. `index` lists the session's documents.
- The page refreshes on every edit, so don't render for the user's benefit. Render when *you*
  need to inspect the result, and batch the primitives you already know you need first.

## References

Read the one that matches the task before you start it:

| file | covers |
|---|---|
| [references/diagrams.md](references/diagrams.md) | diagram nodes, edges, containers, `layout_diagram`, `add_diagram_graph`, `reflow` |
| [references/charts.md](references/charts.md) | `add_chart` kinds, axes, stacking, labels, `edit_chart` |
| [references/annotations.md](references/annotations.md) | legends, callouts, callout cards, tables |
| [references/themes.md](references/themes.md) | loading themes, roles, variants, inline style vs theme |
| [references/styling.md](references/styling.md) | style keys, colors, paint references, gradients, named styles, clips, masks, markers |
| [references/text.md](references/text.md) | text, multi-line text, fonts, `measure_text`, `text_to_path` |
| [references/effects.md](references/effects.md) | shadows, glows, bevels, gloss, custom filters |
| [references/editing.md](references/editing.md) | orienting, targeting by id or name, coordinates, editing in place, transforms |
| [references/output.md](references/output.md) | rendering, file export, read-only `svg://` resources |
