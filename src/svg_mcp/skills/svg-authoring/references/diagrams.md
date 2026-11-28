# Diagrams

- For ANY box-and-arrow diagram — architecture, flowchart, pipeline — use the diagram facades,
  NOT hand-placed rects and lines: `add_diagram_node(kind, label)`, `add_diagram_edge(source,
  target, kind)`, `add_diagram_container(members)`. Nodes size themselves to their labels;
  edges pick sides, fan out across a face, route at right angles, and carry haloed labels;
  containers draw behind their members and re-fit as they move. The bundled default theme
  styles all of it with no setup.
- Edges store WHAT they connect, not where they run. **After moving, resizing, or deleting
  diagram nodes, call `reflow()`** — that one call re-routes every edge and re-fits containers
  from current positions.
- Skip coordinates entirely: add nodes and edges, then `layout_diagram(algorithm="layered",
  direction="LR")` (or "tree"/"grid"). Fine-tune with `translate_node` + `reflow` afterwards —
  layout is opt-in and won't run again behind your back.
- Have a graph already (service map, dependency tree, state machine, call graph)? One
  `add_diagram_graph` call ingests `{nodes, edges}` — the producer's own shape, `from`/`to` and
  extra keys and all — and lays it out. Don't hand-write N `add_diagram_node` calls.
- **It never decides what matters — you do**, and a raw graph is usually too big to read as-is.
  Say which of the three you mean: *graph it all* (then read `bounds` and `resize_document`);
  *graph it all except* (`exclude=[ids or globs]`); or *group it this way*
  (`collapse=[{id, members}]`, drawing several nodes as one). Judge that from what the diagram is
  FOR — no score can. Everything dropped or folded is counted in the response.
- Edit in place: `edit_diagram_node`/`edit_diagram_edge`/`edit_diagram_container` patch
  label/kind/route/members; `get_params` reads any facade's spec back.
