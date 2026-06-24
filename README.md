# svg-mcp

A [FastMCP](https://github.com/jlowin/fastmcp) server that exposes **structured, hierarchical
SVG authoring** to an LLM — all primitives, gradients/patterns, paths, text-on-path, embedded
raster, reusable styles — built around an **inkex-backed document model** the AI can navigate,
query, and manipulate as groups, layers, transforms, and masks, with a fast
**construct → render → see → iterate** loop.

See [`DESIGN.md`](./DESIGN.md) for the full architecture and the
[`INKEX_PRIMITIVES.md`](./INKEX_PRIMITIVES.md) catalog for the inkex → tool mapping.

## Status

The full inkex catalog is mapped through to **91 MCP tools** (all three tiers — see
[`INKEX_PRIMITIVES.md`](./INKEX_PRIMITIVES.md)), with ruff/mypy clean and the test suite green.

- **Document model** (inkex-backed, multi-document): shapes, paths (+ arc/star factories and
  path-data ops), text + tspan runs + text-on-path + flowed text, images (base64/file embed),
  groups (+ ungroup, z-order, duplicate, world-preserving reparent), **layers** (+ visible/
  locked/opacity), symbols/use, hyperlinks.
- **Reusable resources**: named styles (CSS classes + `@name` paint refs), linear/radial/mesh
  gradients, patterns, markers, **clip + mask**, and **filters** (blur, drop-shadow,
  color-matrix/overlay, blend, morphology, component-transfer, turbulence, displacement, plus a
  raw filter-graph builder).
- **Transforms** as primitives: translate, rotate-about-center, scale-about-anchor, skew, raw.
- **Queries**: outline, bbox, computed style, transform/CTM, unit conversion, selectors
  (`find`/`get_subtree`), image extraction; metadata (title/desc/RDF); guides & pages.
- **Render-and-see loop**: serialize-then-rasterize via the **resvg** CLI; the image is handed
  back directly as base64 image content (optional downscale cap).

## Install

```bash
pip install -e ".[dev]"
```

### inkex (the SVG DOM)

Depends on the released **PyPI `inkex>=1.4.1`**. Note its API shape (verified against the
install): `inkex.colors` is a flat module, and there is **no `Image.embed_image()`** — raster
embedding is done manually (base64 data URI). All element classes plus `bounding_box`,
`composed_transform`, `specified_style`, and `Transform @` composition are present.

### resvg (the default renderer)

The default backend shells out to the `resvg` binary — a deterministic, cross-platform static
renderer with native text-on-path and broad filter support, no system libs:

```bash
brew install resvg          # macOS
# or: cargo install resvg
```

Override the path with `SVG_MCP_RESVG_BINARY=/path/to/resvg`. An optional in-process binding
is available via the `resvg` extra (`pip install -e ".[resvg]"`).

### Optional backends

- `cairo` extra — secondary vector/raster backend (PDF/PS/SVG out) via cairocffi + pango;
  needs Homebrew `cairo` + `pango`. Stub today.
- Headless Inkscape — reference renderer + heavy ops, driven via `--shell` (no D-Bus on
  macOS). Stub today.

## Configuration

All settings are env vars prefixed `SVG_MCP_` (or a `.env` file):

| Var | Default | Meaning |
|---|---|---|
| `SVG_MCP_RENDERER` | `resvg` | Default render backend (`resvg`/`cairo`/`inkscape`) |
| `SVG_MCP_RESVG_BINARY` | auto | Path to the resvg CLI |
| `SVG_MCP_INKSCAPE_BINARY` | auto | Path to the Inkscape CLI |
| `SVG_MCP_FEEDBACK_MAX_EDGE` | unset | Optional long-edge cap (px); unset = raw image handed back directly as base64 |
| `SVG_MCP_DEFAULT_BACKGROUND` | transparent | Default render background (CSS color) |
| `SVG_MCP_RENDER_TIMEOUT_S` | `30` | Per-render subprocess timeout |
| `SVG_MCP_TRANSPORT` | `stdio` | Server transport: `stdio` or `http` |
| `SVG_MCP_HOST` / `SVG_MCP_PORT` | `127.0.0.1` / `8000` | Bind address for the http transport |

## Develop

```bash
pytest          # tests (resvg smoke test auto-skips if the binary is absent)
ruff check .    # lint
mypy src        # types (no Any / object — precise types only)
```

## Run

```bash
svg-mcp                              # FastMCP server over stdio (default)
SVG_MCP_TRANSPORT=http svg-mcp       # or over HTTP at 127.0.0.1:8000
```

## Experiment with an LLM

Quickest sanity check (no LLM): render a sample poster to PNG.

```bash
.venv/bin/python scripts/demo.py            # writes demo_output.png
```

**Claude Code** — this repo ships a project [`.mcp.json`](./.mcp.json); open the project and
approve the `svg-mcp` server, or add it explicitly:

```bash
claude mcp add svg-mcp -- /Users/geohar/Development/svg-mcp/.venv/bin/svg-mcp
```

**Claude Desktop** — add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "svg-mcp": { "command": "/Users/geohar/Development/svg-mcp/.venv/bin/svg-mcp" }
  }
}
```

**MCP Inspector** — interactively call tools and view rendered images in a browser:

```bash
uv run fastmcp dev src/svg_mcp/server.py:mcp
```

The model's loop is: `create_document` → add nodes / resources → `render_document` to see the
result inline → iterate → `export_svg`. The server's `instructions` describe the full workflow
and conventions; each tool carries its own description.
