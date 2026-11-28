# Rendering, export and resources

## Rendering and export

- `render_document` returns a short summary plus the rendered image (base64), via the resvg
  engine. Flowed text (`add_flowed_text`) and mesh gradients have limited/no rendering support;
  prefer `add_text`+`add_text_run` and linear/radial gradients for reliable output.
- `export_render(format, path)` writes a FILE: png/jpeg/webp (faithful raster via resvg),
  pdf/ps/eps (true vector via librsvg), or svg (source). `export_formats()` lists them and reports
  whether vector export is available. cairo is intentionally unused (it drops SVG filters).

## Resources (read-only svg:// context)

- The server also publishes read-only resources the host may surface as context, scoped to
  the session by a token: `svg://<token>/documents` (index of open docs),
  `svg://<token>/<document_id>/svg` (live SVG source) and `svg://<token>/<document_id>/render`
  (a PNG preview). Tools return these URIs (`create_document`'s `resources`, `list_documents`'
  `index`); use them as given. Reading them never changes state.
