# SVG MCP — Design Document

A FastMCP server exposing full, structured SVG authoring to an LLM: all primitives,
gradients/patterns, paths, text-on-path, embedded raster, reusable styles — built around
a **hierarchical document model** the AI can navigate, query, and manipulate as groups and
transforms, with a fast **construct → render → see → iterate** loop.

The point of differentiation is **not** the renderer (we can shell out to a binary). It is
the **DOM layer**: a machine-legible hierarchy with stable names, a transform stack,
computed styles, and operations that extract and rewrite subtrees.

---

## 1. Goals / non-goals

**Goals**
- Full SVG primitive coverage: rect, circle, ellipse, line, polyline, polygon, path, text,
  image (embedded raster), use/symbol, group/layer.
- Reusable **resources** in `<defs>`: styles, linear/radial gradients, patterns, markers,
  clipPaths, masks, filters, symbols — all defined once, referenced by name/id.
- A **hierarchical model** exposed to the LLM with: stable ids, friendly names, name-path
  addressing, a structured selector language, the transform stack (local + composed CTM),
  and computed styles.
- **Hierarchy operations**: group/ungroup, reparent (with world-position preservation),
  z-order, duplicate/instance, transform a subtree, restyle a selection, delete.
- **Multiple concurrent documents** keyed by `document_id`.
- **Render to raster** the AI sees inline (PNG via FastMCP `Image`). resvg first.
- Strict **pydantic** validation on every tool; full descriptions; tool-call parsing
  cleanly separated from model construction and from rendering.

**Non-goals (v1)**
- SMIL/CSS animation, scripting, interactivity (we flatten to static).
- Boolean path ops (union/intersect/difference) — roadmap, needs a clipping lib.
- A GUI. This is a headless authoring API.

---

## 2. Architecture / layering

```
svg_mcp/
  server.py        FastMCP app. Thin: pydantic-validated tool I/O -> ops/query -> structured result (+Image).
  schemas/         Pydantic models ONLY (the wire/tool contract): Paint, Color, TransformOp,
                   ShapeUnion, PathCommand, StyleSpec, GradientSpec, Selector, OutlineOptions...
  model/           The canonical document model (source of truth). Pure Python, no I/O:
                   node.py (Node hierarchy), document.py, defs.py (resource registry),
                   names.py (id + name index), matrix.py (affine math), bbox.py.
  ops/             Hierarchy mutations as pure functions over the model: group, ungroup,
                   reparent, reorder, transform, restyle, duplicate, delete.
  query/           Read side: selector resolution, outline/subtree extraction, computed
                   style resolution, composed-transform (CTM) + world-bbox.
  serialize/       model -> lxml tree -> SVG string  (export AND render input).
  ingest/          SVG string -> model  (safe parse via defusedxml; optional usvg normalize).
  render/          Renderer protocol + backends: resvg_backend (primary), cairo_backend (secondary).
  session/         DocumentStore: multi-doc registry, id/name allocation, optional history.
```

**Strict boundaries** (a stated requirement):
- The tool layer (`server.py` + `schemas/`) never touches lxml or a renderer. It validates
  input into schema objects and calls `ops`/`query`.
- `model/` + `ops/` know nothing about SVG text or rasterization — they manipulate an
  in-memory tree. This is what makes them unit-testable in isolation.
- `serialize/` is the only place that emits SVG. Both export and render consume its output,
  which **guarantees preview == export** (see §11).

---

## 3. The document model (the centerpiece)

A document is a **tree of typed nodes** plus a **defs registry**.

### 3.1 Node

Every node carries:

| field | meaning |
|---|---|
| `id` | stable, unique within the document; also the emitted SVG `id`. Auto-generated (`rect_3f1a`). The precise machine handle. |
| `name` | optional friendly label (AI- and human-meaningful). Emitted as `inkscape:label` + a `data-name` for round-trip. Unique *within a parent* (enables name-path addressing). |
| `transform` | the node's **local** affine transform (its own slot in the transform stack). |
| `styles` | ordered list of referenced style names (CSS classes). |
| `style_overrides` | inline presentation attributes layered on top of the classes. |
| `metadata` | free-form semantic tags: `role`, `description`, arbitrary `data-*`. Emitted as `<title>`/`<desc>` + `data-*`. Lets the AI label intent ("car/chassis") and query by it. |
| `children` | for container nodes only. |

**Node types**
- `Document` — root; holds `width`, `height`, `viewBox`, `defs`, and the root children.
- `Group` — container with transform/style. A **Layer** is a Group with `is_layer=True`
  (emits `inkscape:groupmode="layer"`); same mechanics, distinct semantic for the AI, plus
  layer-only state: `visible` (`display:none`), `locked` (`sodipodi:insensitive`), `opacity`.
  Layers are first-class (see §6.1): the AI organizes a composition into named layers and the
  outline surfaces them distinctly from plain groups.
- `Rect`, `Circle`, `Ellipse`, `Line`, `Polyline`, `Polygon` — geometry leaves.
- `Path` — `d` as raw string **or** structured command list (union; see §10).
- `Text` — runs/tspans; optional `on_path` referencing a path node id (`<textPath>`).
- `Image` — embedded raster (base64 data-URI) or external href; `preserveAspectRatio`.
- `Use` — an instance of another node/symbol (reuse without duplication).

### 3.2 Defs registry (unified reusable resources)

Key insight reused throughout: **styles, gradients, patterns, markers, clipPaths, masks,
filters, and symbols are the same shape** — a named resource defined once and referenced by
id wherever a value of that kind is accepted. The registry is one keyed table per kind, each
entry addressable as `@name` (friendly) or `url(#id)` (raw SVG).

```
defs:
  styles:    {name -> StyleSpec}        # emitted as CSS classes in <style>
  gradients: {id   -> Gradient}         # linear/radial, stops, transform, units, spread
  patterns:  {id   -> Pattern}
  markers:   {id   -> Marker}
  clips:     {id   -> ClipPath}
  masks:     {id   -> Mask}
  filters:   {id   -> Filter}
  symbols:   {id   -> Symbol}           # targets of <use>
```

---

## 4. Naming & addressing — three machine-legible schemes

The AI must refer to nodes precisely and legibly. Three complementary handles:

1. **By id** — `"rect_3f1a"`. Unique, stable, returned on every create. The exact handle;
   never ambiguous. Always accepted.
2. **By name-path** — filesystem-style: `/scene/car/wheel-front`. Built from `name`s, unique
   within each parent. Legible, stable across a session, ideal for the AI to *reason* about
   structure. Resolves to an id.
3. **By selector** — a structured query returning a *list* of ids (§8.2). This is the
   "machine-understandable extraction of subportions of hierarchy."

A `Target` union accepts any of `{id}` / `{path}` / `{selector}` so most tools take one
parameter and resolve uniformly. Single-node tools require id/path or a selector that
resolves to exactly one node (else a typed error listing the matches).

---

## 5. Transform stack

Each node has a **local** transform. The **composed transform (CTM)** of a node is the
product of all ancestor transforms times its own:

```
CTM(node) = transform(root) · transform(parent_k) · … · transform(parent_1) · transform(node)
```

The model exposes both, because the AI reasons in both spaces:
- `get_transform(target)` → `{ local, composed }` as matrices + a decomposition
  (`translate / rotate / scale / skew`).
- `get_bbox(target)` → `{ local_bbox, world_bbox }` (world = CTM applied).

**Transform operations** (`transform_node`) take an op (`translate|rotate|scale|skew|matrix`),
an optional `anchor` (e.g. rotate about the group's center or a point), and a `space`:
- `space=local` — compose into the node's own transform.
- `space=world` — apply in canvas space (we conjugate by the parent CTM so it behaves as the
  AI expects regardless of nesting).

**World-position preservation** is a first-class concern in two operations:
- `reparent(target, new_parent, keep_world_position=True)` — recompute the node's local
  transform so it doesn't visually jump when its ancestor chain changes.
- `ungroup(target)` — bake the group's transform/style into its children before dissolving,
  so the picture is unchanged.

This is exactly the kind of DOM nicety hand-rolled SVG editing usually gets wrong; making it
explicit in the model is a core value-add.

---

## 6. Styles & paint

A **style** is a named bundle of presentation attributes (fill, stroke, stroke-width,
opacity, stroke-dasharray, font-*, etc.). Stored in defs, emitted as a CSS class in `<style>`.

- `define_style(name, props)` / `update_style(name, props)`.
- Nodes reference styles via `styles=[...]` and override per-node via `style_overrides`.
- **Resolution order** (exposed via `get_computed_style`): inherited-from-ancestors →
  style classes (in order) → inline overrides. Inheritance respects SVG rules (fill/font
  inherit; geometry props don't).

**Paint** values (fill/stroke) are a validated union — either a literal
(`#ff0000`, `rgb(...)`, named color, `none`) **or** a reference to a defs resource
(`@brand-grad` / `url(#grad1)`). The same `Paint` type is reused everywhere a paint is
accepted, so gradients/patterns "just work" wherever a color does. This satisfies
"the AI can both define and use styles."

### 6.1 Layers (first-class)

Layers are Groups with layer semantics and their own state, exposed via dedicated tools so the
AI treats them as composition planes rather than incidental groups:
- `create_layer(name, index?)` · `set_layer_state(target, visible?, locked?, opacity?)`
  · `list_layers(document_id)` · `move_to_layer(target, layer)`.
- The `outline` marks layer nodes distinctly and reports `visible/locked/opacity`.
- Z-order at the top level is layer order; within a layer it's child order.

### 6.2 Masking & clipping (first-class)

Both are reusable resources in defs whose **content is itself a subtree built from our normal
primitives** — so the AI authors a mask/clip with the same `add_shape`/`add_path`/gradient
tools it uses everywhere:
- **Mask** (`define_mask(name, content=[…])`) — luminance or alpha; soft edges, gradients in
  the mask content produce feathering. Applied via `mask` on any node/group/layer.
- **ClipPath** (`define_clip(name, content=[…])`, `units`) — hard geometric clip. Applied via
  `clip_path`.
- `apply_mask(target, mask)` / `apply_clip(target, clip)` / `clear_mask`/`clear_clip` attach
  or detach by reference; because they're url-referenced resources, one mask can be shared
  across many nodes. resvg renders both (luminance + alpha masks, nested clips) natively;
  cairo and Inkscape do too.

---

## 7. Hierarchy operations (`ops/`)

All are pure functions `model -> model'` (enabling undo/history if we keep snapshots):

- `create_group(name, parent, children=[…])` — wrap existing nodes (or empty).
- `ungroup(target)` — dissolve, baking transform/style down (§5).
- `reparent(target, new_parent, index, keep_world_position=True)`.
- `reorder(target, index)` and z-order sugar: `to_front / to_back / raise / lower`.
- `transform_node(target, op, anchor, space)` (§5).
- `restyle(target, styles?, style_overrides?)` — single node or a whole selection.
- `duplicate(target, into?)` / `make_use(target)` — copy vs instance.
- `set_name` / `set_metadata` / `delete`.
- **Bulk**: any of the above accept a selector target to operate over many nodes
  (`apply_style_to(selector, style)`, `transform(selector, …)`).

---

## 8. Hierarchical exposure to the LLM

### 8.1 Outline (the AI's map)

`outline(document_id, root?, depth?, fields?)` returns a compact, structured tree — the
AI's primary way to orient and re-orient mid-edit. Depth-limited; collapses deep subtrees to
`children_count`; optional per-node `bbox` / `composed_transform` / `style` summary.

```json
{
  "document_id": "doc1",
  "viewBox": [0, 0, 800, 600],
  "tree": {
    "id": "root", "type": "document", "children": [
      { "id": "g_scene", "name": "scene", "type": "group",
        "transform": "translate(40,40)", "world_bbox": [40,40,720,520],
        "children": [
          { "id": "g_car", "name": "car", "type": "group",
            "role": "vehicle", "children_count": 5, "world_bbox": [60,300,400,180] }
        ]
      }
    ]
  }
}
```

### 8.2 Selectors (machine extraction of subportions)

A structured (not stringly-typed) selector, validated by pydantic, composed of predicates:

```
Selector = {
  within?:   Target,            # restrict to a subtree (e.g. /scene/car)
  type?:     [NodeType],        # e.g. ["rect","path"]
  name?:     str | {glob|regex},
  has_style? : str,             # references style class "@card"
  role?:     str,               # metadata role
  intersects_bbox?: [x,y,w,h],  # spatial query in world space
  depth?:    {min?, max?}
}
```

`find(selector)` → list of `{id, name, path, type, world_bbox}`.
`get_subtree(target)` → the subtree as both structured JSON **and** an SVG fragment, so the
AI can extract, reason about, or hand back a portion of the hierarchy.

### 8.3 Inspection

`describe_document`, `get_computed_style`, `get_transform`, `get_bbox`, `list_resources`.

---

## 9. Tool surface

Discoverability is ours to manage, so the surface is organized for *clarity*, not minimal
count. Verb tools + pydantic discriminated unions.

**Session / document**
`create_document` · `list_documents` · `describe_document` · `outline` · `delete_document`
· `export_svg` · `import_svg` · `render`

**Construction**
`add_shape`(ShapeUnion: rect|circle|ellipse|line|polyline|polygon) · `add_path` · `add_text`
· `add_image` · `add_use` · `create_group`

**Hierarchy / manipulation**
`reparent` · `ungroup` · `reorder` (+ z-order sugar) · `transform_node` · `update_node`
· `restyle` · `set_name` · `set_metadata` · `duplicate` · `delete_node`

**Resources (defs)**
`define_style` · `update_style` · `define_linear_gradient` · `define_radial_gradient`
· `define_pattern` · `define_marker` · `define_clip` · `define_mask` · `define_filter`
· `define_symbol` · `list_resources`

**Query**
`find` · `get_subtree` · `get_computed_style` · `get_transform` · `get_bbox`

Every create/mutate tool returns the affected node's `{id, name, path}` so the AI keeps a
precise handle without re-outlining.

---

## 10. Pydantic schema sketches

Illustrative, not final.

```python
class Color(RootModel[str]):
    # "#rgb", "#rrggbb", "rgb()/rgba()", named, "none" — validated by regex/table
    ...

class Paint(RootModel[str]):
    # a Color OR a resource ref: "@brand-grad" or "url(#grad1)"
    ...

class TransformOp(BaseModel):
    kind: Literal["translate","rotate","scale","skew","matrix"]
    # discriminated fields per kind; rotate/scale accept optional anchor
    ...

# add_shape input — discriminated union on `kind`
class RectSpec(BaseModel):
    kind: Literal["rect"]; x: float; y: float; width: float; height: float
    rx: float | None = None; ry: float | None = None
class CircleSpec(BaseModel):
    kind: Literal["circle"]; cx: float; cy: float; r: float
# … ellipse / line / polyline / polygon …
ShapeUnion = Annotated[RectSpec | CircleSpec | ..., Field(discriminator="kind")]

# Path: raw OR structured
class PathCommand(BaseModel):
    cmd: Literal["M","L","H","V","C","S","Q","T","A","Z"]
    # discriminated args per cmd (e.g. C -> x1,y1,x2,y2,x,y)
class PathSpec(BaseModel):
    d: str | None = None
    commands: list[PathCommand] | None = None   # exactly one of d/commands

class GradientStop(BaseModel):
    offset: float = Field(ge=0, le=1); color: Color; opacity: float = 1.0
class LinearGradientSpec(BaseModel):
    name: str; x1: float; y1: float; x2: float; y2: float
    stops: list[GradientStop]
    spread: Literal["pad","reflect","repeat"] = "pad"
    units: Literal["objectBoundingBox","userSpaceOnUse"] = "objectBoundingBox"

class Target(BaseModel):
    id: str | None = None
    path: str | None = None
    selector: "Selector | None" = None   # exactly one of the three
```

Validation (e.g. "exactly one of `d`/`commands`", "stop offsets ascending",
"`Target` has exactly one handle", "`Paint` reference resolves to an existing resource") lives
in schema validators, so the tool body receives already-valid, resolved input.

---

## 11. Rendering — two backends, resvg first

We **render by serializing** (`model → lxml SVG → renderer`) rather than redrawing the model,
because the export path already produces that SVG. This **guarantees the AI sees exactly what
we export** and keeps the renderer a swappable backend behind one protocol:

```python
class Renderer(Protocol):
    def render_png(self, svg: str, scale: float = 1.0) -> bytes: ...
```

### 11.1 resvg — primary (start here)
- Fed our serialized SVG; returns PNG bytes → FastMCP `Image`.
- Free, correct **text-on-path**, broad **filters** (blur…turbulence/lighting), HarfBuzz
  shaping (rustybuzz), high fidelity. Exactly our stated requirements with zero reimplementation.
- Deterministic across platforms (bundle fonts) — same SVG → same pixels, which matters for
  an AI that reasons about the render.
- Robust: runs as a subprocess (or a long-lived render server); a render crash can't take down
  the MCP.

### 11.2 cairo + pango — secondary (documented, added later)
- `model`/SVG → cairocffi surface; text via **pangocffi / pangocairocffi** (HarfBuzz — text
  *quality* is at parity with resvg, not cairo's toy fonts).
- Wins cairo can't get from resvg: **vector output** (PDF / PS / SVG), lowest latency
  (in-process, no subprocess, no SVG round-trip), and full Python control/debuggability —
  and it leverages the project's improved cairocffi bindings.
- Costs we take on to use it: **text-on-path** is not native (Pango shapes; we walk the path's
  arc-length parameterization and place each glyph with its own rotation), and **SVG filters**
  must be hand-rolled. Also a second renderer of our model risks **drift** from the export SVG
  unless validated against resvg.

### 11.3 resvg vs cairo/pango — the tradeoff, in full

| Axis | resvg (primary) | cairo + pango (secondary) |
|---|---|---|
| Text-on-path | **Native** | Hand-rolled glyph-walk along arc-length |
| SVG filters | **Broad, free** | Hand-rolled (blur easy; turbulence/lighting hard) |
| Text shaping | HarfBuzz (rustybuzz) | HarfBuzz (pango) — **parity** |
| Gradients/patterns/clip/mask | Excellent | Excellent (maps to cairo) |
| Embedded raster | Native `<image>` | `set_source_surface` / native — easy both |
| Vector output (PDF/PS/SVG) | **No** (raster only) | **Yes** |
| Latency per render | Subprocess spawn + SVG parse | **In-process, no round-trip** — lowest |
| Determinism across machines | **High** (bundle fonts) | Varies with system cairo/fontconfig |
| Preview == export fidelity | **Guaranteed** (same SVG) | Risk of two-renderer drift |
| Crash isolation | **Process-isolated** | In-process segfault kills server |
| Hackability / debuggability | Opaque binary (extend = Rust) | **Full Python control** |
| Maintenance burden | Low (reuse engine) | Higher (own text-on-path + filters) |

**Reading:** resvg trades latency, opacity, and raster-only output for *correctness,
determinism, preview==export, robustness, and free text-on-path/filters*. cairo/pango trades
a second renderer's drift risk and hand-built text-on-path/filters for *vector output, lowest
latency, and total control*. Starting on resvg gets the requirements met immediately; cairo
arrives later as the **vector-export** backend and an optional fast-preview path, validated
against resvg as the fidelity reference.

### 11.4 Optional third backend — headless Inkscape

If we're willing to ship the Inkscape binary, a headless Inkscape process is a **reference
renderer** (the most faithful SVG raster available — full filters, masks, markers, text-on-path)
*and* unlocks heavy operations no pure-Python path gives cheaply: boolean path ops, path
effects, trace bitmap, align/distribute, optimization.

**macOS constraint:** D-Bus is Linux-only and not available here, so the persistent-process
driver is **`--shell`** (a long-lived REPL fed `file-open` / `--actions` / `export-do` over
stdin), plus one-shot `inkscape --export-type=png …` for batch export. The macOS binary lives
at `/Applications/Inkscape.app/Contents/MacOS/inkscape`; modern 1.x runs CLI export headless
without XQuartz, though a few actions historically wanted a display — we'd validate the exact
action set we rely on. Costs: a heavy (~hundreds of MB) dependency, slow startup (amortized by
keeping the `--shell` process alive), version/font-dependent output, and a stringly-typed
action vocabulary. → A **power-tier backend** for reference export and heavy ops, not the
live-edit engine. Distinct from using **inkex as a library** (§12), which needs no Inkscape
process at all.

---

## 12. The DOM-model libraries — build vs. borrow

The canonical model (§3) is **ours** — we will not hand the AI-facing semantics, naming,
metadata, multi-doc, and outline format to a third-party API that churns. But we should
**borrow proven, stateless math** rather than reinvent affine transforms and curve bboxes.
Evaluation of the candidates:

- **`lxml.etree`** — the substrate. libxml2-fast, full namespaced **XPath**, real mutation
  API. Knows nothing about SVG semantics. → We use it in `serialize/` and `ingest/`, and its
  XPath backs selector resolution. Foundational, not the model.

- **`inkex`** (Inkscape extensions) — the richest *semantic* SVG DOM in Python: typed
  elements, `Transform` (affine algebra), `Style` (cascade), **`composed_transform()` (CTM
  for free)**, `bounding_box()`, **Layers and masks/clips first-class**, `inkscape:label`
  naming, group/ungroup helpers. **Crucially, inkex used as a library needs NO headless
  Inkscape and no display** — it is pure Python over lxml; only the Inkscape *binary* (§11.4)
  needs an environment. So "can I run headless?" is a non-issue for inkex-as-library. We pin the
  released **PyPI `inkex>=1.4.1`**, whose API (verified) has a flat `colors` module and no
  `Image.embed_image()` (we embed raster manually); all element classes + bbox/CTM are present.
  The full inkex→tool mapping lives in **`INKEX_PRIMITIVES.md`**. It is lxml-backed, so it round-trips SVG natively
  and we layer our AI-facing semantics (stable handles, name-paths, selectors, outline,
  metadata, multi-doc) on top of its elements. The given requirements — **layers and
  masking** — are exactly inkex's strengths. Costs: it carries Inkscape namespace
  conventions (arguably desirable for round-trip), its API shifted across Inkscape 1.0→1.2 (so
  pin/vendor a version), and text bbox is approximate without a renderer.

- **`svgelements`** (tatarize) — single-file, zero-dependency, geometry-first: excellent
  `Matrix`, `Path` segment algebra with real `point(t)`/`length()`/`bbox()` (line/quad/
  cubic/arc), `Color`, unit `Length` resolution; it *reifies the cascade* so each parsed
  element carries computed values + absolute transform. Superb for the **query/geometry**
  side (bbox, hit-testing, world-space reasoning) and trivially **vendorable**. Weaker as a
  mutable round-tripping DOM and no layer concept. → **Borrow its `Matrix`/`Path`/bbox math**
  for `model/matrix.py` + `bbox.py` and ingest-time geometry.

- **`svgpathtools`** — path geometry: arc-length parameterization, tangents. → Directly
  useful for **text-on-path glyph placement** (cairo backend) and accurate path bbox.

- **`usvg`** (resvg) — normalizes arbitrary SVG (resolves `use`, inheritance, units,
  transforms) into a clean canonical tree. → Optional **import normalizer**: messy external
  SVG → normalized → ingest into our model.

- **`defusedxml`** — harden the **import** path against XXE/entity-expansion attacks before
  handing bytes to lxml.

- **`svgwrite` / `drawsvg`** — write-only builders, no DOM/query. → Out for this layer.

**The live fork — who owns the in-memory DOM:**

- **(A) inkex as the DOM** — make inkex's lxml-backed element tree the working model and put
  our AI-facing facade (handles, name-paths, selectors, outline, metadata, multi-doc) on top.
  We get **layers, masks/clips, `composed_transform` (CTM), bbox, styles, group/ungroup** for
  free — precisely the stated requirements — and native SVG round-trip. Cost: couple to inkex's
  API (mitigate by pinning/vendoring a version) and live with Inkscape namespace conventions.
- **(B) own model + borrowed math** — own node tree as source of truth; borrow `svgelements`
  (Matrix/Path/bbox) + `svgpathtools` (arc-length); lxml for serialize/XPath; inkex only as a
  reference/import adapter. Maximum control and stability; cost: we build layers, masks, CTM
  composition, group/ungroup ourselves (math borrowed, so straightforward but real work).

Given that you can run headless *and* want layers + masking — inkex's home turf — **(A) is now
the stronger default**: it collapses the most-requested features into a maintained library and
removes the largest chunk of "build it ourselves." We keep (B)'s instinct where it matters by
keeping the **AI-facing model ours** as a facade, so the tool contract and outline format stay
stable even if inkex churns underneath. Common to both: **lxml** substrate, **defusedxml** (+
optional **usvg**) on import, **svgpathtools** for text-on-path arc-length, **resvg** to render.

---

## 13. Multi-document session

`DocumentStore` keyed by `document_id`. `create_document` returns an id; every mutate/query/
render takes one explicitly — **no hidden "active document"** (safer when the AI interleaves
work across docs). `list_documents` / `describe_document` for re-orientation. Optional
per-document snapshot history enables `undo` later (cheap because `ops/` are pure).

---

## 14. Import & safety

`import_svg` → `defusedxml` parse → optional `usvg` normalize → map into our model (using
`svgelements` for geometry). External/untrusted SVG is sanitized (no scripts, no external
entity fetches). Embedded raster size limits enforced.

---

## 15. Phasing

1. **Core model + resvg render loop**: Document/Group/shapes/path/text, defs(styles+gradients),
   create/update/transform/reorder, `outline`, `export_svg`, `render` (resvg). Multi-doc.
2. **Full hierarchy ops + query**: reparent (world-preserving), ungroup, selectors, `find`,
   `get_subtree`, computed style/CTM/bbox, metadata/roles.
3. **Remaining resources + features**: patterns, markers, clip/mask/filter, symbols/use,
   text-on-path, embedded images, import (defusedxml/usvg).
4. **cairo/pango backend**: vector PDF/PS export + optional fast in-process preview, validated
   against resvg; hand-rolled text-on-path glyph-walk and filter shim.
5. **Polish**: undo/history, boolean path ops, align/distribute, accessibility (`title`/`desc`).
```
