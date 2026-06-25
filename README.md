<p align="center">
  <img src="./logo-variant.png" alt="svg-mcp" width="460">
</p>

# svg-mcp

A [FastMCP](https://github.com/jlowin/fastmcp) server that exposes **structured, hierarchical
SVG authoring** to an LLM — all primitives, gradients/patterns, paths, text-on-path, embedded
raster, reusable styles — built around an **inkex-backed document model** the AI can navigate,
query, and manipulate as groups, layers, transforms, and masks, with a fast
**construct → render → see → iterate** loop.

> The logo above was authored entirely through these tools — see [Usage](#usage).

See [`DESIGN.md`](./DESIGN.md) for the full architecture and the
[`INKEX_PRIMITIVES.md`](./INKEX_PRIMITIVES.md) catalog for the inkex → tool mapping.

## Quickstart (Claude)

1. **Prerequisites** — Python ≥ 3.12 and the [resvg](https://github.com/linebender/resvg)
   renderer:

   ```bash
   brew install resvg          # macOS (or: cargo install resvg)
   ```

2. **Install** the server from a clone of this repo (pulls fastmcp, inkex, fontTools, …):

   ```bash
   uv venv && uv pip install -e .     # entrypoint lands at .venv/bin/svg-mcp
   # or: python -m venv .venv && .venv/bin/pip install -e .
   ```

3. **Connect Claude:**

   - **Claude Code** — from the repo directory:

     ```bash
     claude mcp add svg-mcp -- "$(pwd)/.venv/bin/svg-mcp"
     ```

     (Or just open the project — it ships a [`.mcp.json`](./.mcp.json) you can approve.)

   - **Claude Desktop** — add to `claude_desktop_config.json` (use the **absolute** path) and
     restart the app:

     ```json
     { "mcpServers": { "svg-mcp": { "command": "/ABSOLUTE/PATH/TO/svg-mcp/.venv/bin/svg-mcp" } } }
     ```

4. **Try it** — ask Claude:

   > Use svg-mcp to make a 320×120 badge that says "hello" on a blue gradient, then show me the
   > render.

   Claude creates a document, adds the shapes and text, and calls `render_document` to show you
   the image inline — then you iterate in plain language ("bigger text", "add a drop shadow",
   "outline the text to paths"). See [Usage](#usage) for the conventions and a worked example.

## Status

The full inkex catalog is mapped through to **100 MCP tools** (see
[`INKEX_PRIMITIVES.md`](./INKEX_PRIMITIVES.md)), with ruff/mypy clean and the test suite green.

- **Document model** (inkex-backed, multi-document with an active-document default): shapes,
  paths (+ arc/star factories and path-data ops), text + tspan runs + text-on-path + flowed
  text, images (base64/file embed), groups (+ ungroup, z-order, duplicate, world-preserving
  reparent), **layers** (+ visible/locked/opacity), symbols/use, hyperlinks.
- **Text → paths** (`text_to_path`): pure-Python glyph outlining (fontTools) — flattens tspans,
  honors bold/italic/per-run fill, and walks text **along a curve** (`<textPath>`). `list_fonts`
  enumerates installed families; `measure_text` returns a run's width/height from font metrics so
  you can fit and center text **without a render round-trip**.
- **Reusable resources**: named styles (CSS classes + `@name` paint refs), linear/radial/mesh
  gradients, patterns, markers, **clip + mask**, and **filters** (blur, drop-shadow,
  color-matrix/overlay, blend, morphology, component-transfer, turbulence, displacement, plus a
  raw filter-graph builder).
- **Transforms** as primitives: translate, rotate-about-center, scale-about-anchor, skew, raw.
- **Queries / context**: `current_context`, `describe_node`, `list_resources`, `outline`, bbox,
  computed style, transform/CTM, unit conversion, selectors (`find`/`get_subtree`), image
  extraction; metadata (title/desc/RDF); guides & pages.
- **Render-and-see loop**: serialize-then-rasterize via the **resvg** CLI; the image is handed
  back directly as base64 image content. Documents are also published as readable MCP
  **resources** (`svg://documents`, `svg://{id}/svg`, `svg://{id}/render`) with change
  notifications.
- **File export** (`export_render`): faithful raster (png/jpeg/webp via resvg) and **true vector**
  (pdf/ps/eps via librsvg). cairo is intentionally avoided — it silently drops SVG filters (a drop
  shadow renders blank), so it is not faithful to the document.

## Architecture

<p align="center">
  <img src="./docs/architecture.png" alt="svg-mcp architecture" width="900">
</p>

> Authored entirely through the svg-mcp tools and rendered via the server's resvg backend
> ([`docs/architecture.svg`](./docs/architecture.svg) is the live serialized source).

Three layers: an **interface & contract** tier (the FastMCP server, pydantic input schemas, and
per-session document stores), a **document-operations** tier (inkex-facing construction/edit,
read-only introspection, and the document model), and a **rendering & output** tier (pure-Python
typesetting, the render/export backends, and SVG serialization). See [`DESIGN.md`](./DESIGN.md)
for the full layering rationale.

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

## Usage

The tools are called by an LLM over MCP. The core loop is **create → build → render-and-see →
iterate → export**. A minimal session (arguments shown as the JSON each tool receives):

```text
create_document(width=320, height=120)                 # → {document_id:"doc1", active:true}

# define a reusable gradient, then paint with it by name (@name) or url(#id)
define_linear_gradient(x1=0, y1=0, x2=1, y2=0,
    stops=[{offset:0, color:"#7dd3fc"}, {offset:1, color:"#1e3a8a"}], name="brand")
add_rect(x=0, y=0, width=320, height=120, rx=16, style={fill:"@brand"})

add_text(x=160, y=72, content="svg-mcp", name="title",
    style={font_family:"Helvetica", font_size:"40px", font_weight:"bold",
           text_anchor:"middle", fill:"#ffffff"})
apply_drop_shadow(target="title", dx=0, dy=2, blur=3, color="#000", opacity=0.4)

render_document(scale=2)        # returns the rendered PNG inline — look, then adjust
export_svg()                    # final SVG source string
```

### Conventions

- **Active document.** `create_document` returns a `document_id` and makes it active; you may
  **omit `document_id`** on later calls to target it. Pass it explicitly to switch, or use
  `set_active_document`. Call `current_context()` to re-anchor (active id, open docs, outline).
- **Targets by id or name.** Every `target`/`parent`/`content` arg takes a node's returned id
  **or** the friendly `name` you gave it. Name things you'll revisit; reason via `find(name=…)`
  and `outline`.
- **Coordinates.** User units, origin top-left, y increases downward.
- **Style.** A structured object — `fill`, `stroke`, `stroke_width`, `opacity`, plus typography
  (`font_family`, `font_size`, `font_weight`, `font_style`, `text_anchor`). Colors accept hex /
  `rgb()` / CSS names / `none`, **or** a paint reference `url(#id)` / `@name` to a defined
  gradient or pattern.
- **Resources** follow *create → define → reference/apply*: `define_*` returns an id you use as
  a fill (`url(#id)`/`@name`) or attach via `apply_*` (clip/mask/marker/filter). Clip/mask/
  symbol/pattern definitions **move** the listed content nodes into the resource, so build those
  shapes first. `list_resources()` shows what's defined.
- **Transforms** compose: `translate_node`, `rotate_node` (optional center), `scale_node`
  (optional anchor), `skew_node`, or `apply_transform("rotate(45 100 100)")`.
- **Text.** `add_text` + `add_text_run` for multi-line/styled spans; `add_text_on_path` to flow
  along a path. Judge text size with `render_document` (geometry queries are empty for live
  text). `text_to_path` outlines text to glyph paths — **font-independent**, flattens tspans,
  and walks `<textPath>` along its curve; `list_fonts` lists installable families.

### Resources

Open documents are also exposed as readable MCP resources, so a host can surface live state:
`svg://documents` (index + which is active), `svg://{id}/svg` (source), `svg://{id}/render`
(PNG). Mutations emit `resources/updated` notifications.

### Example: the logo

The header logo (`logo.svg`) was built with these tools — `<mcp>svg</mcp>` as a blue gradient
wordmark, an orange→white starfield clipped into the glyphs, the inner `svg` word under a
translucent white veil (so the `<mcp>` tags read as starry space and `svg` as frosted white), all
under one drop shadow — then `text_to_path` outlined the glyphs so the final file is
font-independent. `scripts/demo.py` shows a smaller end-to-end build you can run directly.
