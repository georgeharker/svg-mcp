# inkex → MCP Primitive Catalog

A completionist map of the **inkex** element/API surface to the **svg-mcp** tool surface.
This is the source of truth for "which inkex primitive becomes which tool." Pairs with
`DESIGN.md` (architecture). Compiled from the inkex API reference
(<https://inkscape.gitlab.io/extensions/documentation/>).

**Target: PyPI `inkex>=1.4.1`** (verified against the actual install). Its API shape:
`inkex.colors` is a **flat module** with a flat `Color` (`.to`, `.to_rgb`, `.to_hsl`,
`.to_named`), and **`Image.embed_image()` does NOT exist** — so raster embedding is done
**manually** (base64-encode bytes → `data:{mime};base64,…` → set `xlink:href`). All element
classes plus `bounding_box`/`composed_transform`/`specified_style`/`Transform @` are present.
(Git master 1.4.0 differs — colors-as-package, `embed_image` present — but we pin the release.)

Legend for **Tier**: **1** = expose first (core), **2** = next, **3** = advanced/deferred,
**—** = internal engine, not a tool.

## Implementation status

- **Tier 1 — DONE** (54 MCP tools, all gates green). Shapes, path, text + **text runs** +
  **text-on-path**, groups, **layers + state**, **gradients** (linear/radial + stops),
  **named styles** (CSS classes + `@name` paint refs), **clip + mask** (define/apply/clear),
  **filters** (blur, synthesized **drop-shadow**, color-matrix, color-overlay, blend),
  **raster image** (href / base64 / file-embed), **transform primitives** (translate, rotate-
  about-center, scale-about-anchor, skew, raw transform string), queries (outline, bbox,
  describe, computed-style, transform/CTM, unit conversion), render loop. Verified: gradient +
  drop-shadow composition rasterizes through resvg.
- **Tier 2 — DONE** (77 MCP tools total). Path factories (`add_arc`, `add_star`), path-data
  ops (`path_transform`/`to_absolute`/`to_relative`/`path_bbox`), `define_symbol`/`add_use`/
  `unlink_use`, `define_pattern`, `define_marker`/`apply_marker`, raw `define_filter` graph +
  `apply_filter` + `apply_morphology`/`apply_component_transfer`/`apply_turbulence`,
  `extract_image`, metadata (`set_title`/`set_description`/`set_document_metadata`), selectors
  (`find`, `get_subtree`). Verified: pattern-fill + star composition renders.
- **Tier 3 — DONE** (85 MCP tools total). Flowed text (`add_flowed_text`), mesh gradient
  (`define_mesh_gradient`), hyperlinks (`wrap_in_link`), `apply_displacement_map`, and the
  remaining advanced filter primitives (displacement/convolve/lighting/feImage/feTile) reachable
  via the raw `define_filter` graph; guides (`add_guide`/`list_guides`) and pages
  (`add_page`/`list_pages`). Verified: displacement-map composition renders.

**All three tiers implemented and wired** — 91 tools (incl. structural ops: ungroup, z-order,
world-preserving reparent, duplicate), ruff/mypy clean, full test suite green. Drive it via the
project `.mcp.json`, `fastmcp dev`, or `scripts/demo.py` (see README → Experiment with an LLM).

---

## 0. Cross-cutting mechanics (the facade replicates these once)

inkex element classes are thin lxml wrappers; most "set an attribute" work goes through a
handful of shared mechanics that our facade wraps once and reuses for every tool:

- **IDs are the handle.** `svg.getElementById(id)`, `svg.get_unique_id(prefix)`,
  `svg.get_ids()`, `el.get_id(as_url=N)` (`N=0` bare, `1` `#id`, `2` `url(#id)`),
  `el.set_random_id(prefix)`. Every tool keys on id; the facade adds friendly name-paths.
- **Resource-then-reference.** ClipPath, Mask, Marker, Symbol, Gradient, Pattern, Filter all
  follow: *build the element in `<defs>`, then point an attribute at* `url(#id)` *via*
  `get_id(as_url=2)`. One internal helper covers all of them.
- **State via attributes.** label = `inkscape:label` (`.label`); visibility =
  `style display:none` (`is_visible()` walks ancestors); lock = `sodipodi:insensitive`
  (`set_sensitive()` / `is_sensitive()`).
- **Attribute access.** `el.set/.get/.update(**kwargs)`, `el.add(*children)`,
  `el.style` (a `Style` dict), `el.transform` (a `Transform`), `el.href` (resolves/sets the
  referenced element; writes `#id` to `href` or `xlink:href`).
- **Geometry & cascade.** `el.bounding_box()` / `shape_box()` (geometry only — *text bbox is
  empty without a renderer*, see §2), `el.composed_transform()` (CTM), `el.specified_style()`
  / `cascaded_style()` / `get_computed_style(key)`.
- **Units.** `el.unittouu("10mm")` / `el.uutounit(v, "mm")` (element methods, not
  `inkex.units`) normalize human units to/from user units on the way in/out.

---

## 1. Shapes & paths  (`inkex.elements._polygons`, `inkex.paths`)

| inkex class | SVG tag | Tool | Tier | Notes |
|---|---|---|---|---|
| `Rectangle` | `rect` | `add_rect(x,y,width,height,rx?,ry?)` | 1 | props left/top/right/bottom/width/height/rx/ry. |
| `Circle` | `circle` | `add_circle(cx,cy,r)` | 1 | `EllipseBase`: center + radius. |
| `Ellipse` | `ellipse` | `add_ellipse(cx,cy,rx,ry)` | 1 | radius as (rx,ry). |
| `Line` | `line` | `add_line(x1,y1,x2,y2)` | 1 | |
| `Polyline` | `polyline` | `add_polyline(points[])` | 1 | open point list. |
| `Polygon` | `polygon` | `add_polygon(points[])` | 1 | closed point list. |
| `PathElement` | `path` | `add_path(d \| commands[])` | 1 | accepts raw `d` or structured commands (union). |
| `PathElement.arc` | `path` | `add_arc(center,rx,ry?,arctype)` | 2 | factory classmethod. |
| `PathElement.star` | `path` | `add_star(center,radii,sides,rounded,flatsided)` | 2 | factory; stars/polygons. |
| `Path` (ops) | — | `path_transform`, `path_to_absolute/relative`, `path_bbox` | 2 | transform/convert/measure a `d`. |
| 20 `inkex.paths` command classes (`Move/move … ZoneClose/zoneClose`) | — | (backing `commands[]` schema) | — | one pydantic `PathCommand` discriminated union, **not** 20 tools. |
| `CubicSuperPath`, `PathCommand` calculus, `LengthSettings` | — | — | — | internal (arc-length feeds text-on-path). |

Generic over any shape (via `ShapeElement`): `set_style`, `set_transform`, `get_bbox`,
`to_path` (`to_path_element()`), `get_path`.

---

## 2. Text  (`inkex.elements._text`)

> **Renderer caveat:** every text class's `get_path()` is empty — inkex does not shape text,
> so geometric bbox is empty. True extents need `get_inkscape_bbox()` (shells out to Inkscape,
> slow) **or** our resvg render. `measure_text` should use the renderer, not inkex geometry.

| inkex class | SVG tag | Tool | Tier | Notes |
|---|---|---|---|---|
| `TextElement` | `text` | `add_text(x,y,content,style?)` | 1 | base text block. |
| `Tspan` | `tspan` | `add_text_run(parent,text,x?,y?,dx?,dy?,style?)` | 1 | multi-run / multi-line; `superscript()` helper. |
| `TextPath` ★ | `textPath` | `add_text_on_path(path_id,content,start_offset?,side?,style?)` | 1 | **required.** No custom API: set `tp.href = path` (→ `xlink:href`), `tp.set('startOffset',…)`, `tp.set('side',…)`; `<textPath>` is a child of `<text>`. Validate path id exists. |
| `FlowRoot`/`FlowRegion`/`FlowPara`/`FlowSpan`/`FlowDiv` | `flowRoot…` | `add_flowed_text(bounds_rect,paragraphs,style?)` | 3 | Inkscape-specific flowed text; not universally rendered. |
| `SVGfont`/`FontFace`/`Glyph`/`MissingGlyph` | `font…` | — | 3 | SVG-font definitions; niche. Styling uses `.style['font-family']`, not these. |

Text styling is via `.style`: `font-family`, `font-size`, `font-weight/style`, `text-anchor`,
`text-align`, `line-height`, `fill`, `stroke`, `baseline-shift`.

---

## 3. Containers & structure  (`inkex.elements._groups`, `_svg`, `_use`)

| inkex class | SVG tag | Tool | Tier | Notes |
|---|---|---|---|---|
| `Group` | `g` | `create_group`, `ungroup`, `add_to_group`, `reparent` | 1 | `bake_transforms_recursively()` powers world-preserving ungroup. |
| `Layer` | `g[groupmode=layer]` | `create_layer`, `list_layers`, `set_layer_state(visible?,locked?,opacity?)`, `rename_layer`, `move_to_layer` | 1 | **required.** visible=`display`, locked=`set_sensitive(not locked)`, label=`.label`. |
| `Symbol` | `symbol` | `define_symbol` | 2 | reusable unrendered template. |
| `Use` | `use` | `add_use(target_id,x,y)`, `unlink_use` | 2 | `Use.new(elem,x,y)`; `.href` → `#id`; `unlink()` expands. |
| `Defs` | `defs` | (implicit target of all `define_*`) | — | `svg.defs` auto-created. |
| `Anchor` | `a` | `wrap_in_link(href,child_ids)` | 3 | hyperlink wrapper. |
| `SvgDocumentElement` | `svg` | `create_document`, `describe_document`, `get_by_id`, `get_by_label`, `convert_units` | 1 | root handle; viewBox/width/height/unit/scale, `get_page_bbox`. |

---

## 4. Clipping & masking  (`inkex.elements._groups`)  — required

Both extend `GroupBase`: content is **child shapes** added with `.add(shape)`, normally inside
`<defs>`; then point an attribute at `url(#id)`.

| inkex class | SVG tag | Tool | Tier | Notes |
|---|---|---|---|---|
| `ClipPath` | `clipPath` | `define_clip(content[])`, `apply_clip(target,clip)`, `clear_clip(target)` | 1 | geometric intersection; apply via `ShapeElement.clip` setter (`clip-path: url(#id)`); `clipPathUnits`. |
| `Mask` | `mask` | `define_mask(content[])`, `apply_mask(target,mask)`, `clear_mask(target)` | 1 | luminance/alpha (white=opaque); set `mask="url(#id)"` directly; gradients in content → feathering; `maskUnits`/`maskContentUnits`. |
| `Marker` | `marker` | `define_marker(content[],refX,refY,orient,…)`, `apply_marker(path,position,marker)` | 2 | arrowheads; `marker-start/-mid/-end: url(#id)`. |

---

## 5. Gradients & paint servers  (`inkex.elements._filters`)

> Gradients live in `_filters`, **not** `_gradients`. inkex has **no** `add_stop`/`remove_orphans` —
> stops are `Stop` elements you append; the facade owns "add stop". `apply_transform()` bakes
> `gradientTransform` into geometry.

| inkex class | SVG tag | Tool | Tier | Notes |
|---|---|---|---|---|
| `LinearGradient` | `linearGradient` | `define_linear_gradient(x1,y1,x2,y2,stops[],units?,spread?,transform?,href?)` | 1 | getters default 0%/100%; `apply_transform` action. |
| `RadialGradient` | `radialGradient` | `define_radial_gradient(cx,cy,r,fx?,fy?,stops[],units?,spread?,transform?,href?)` | 1 | fx/fy default to center. |
| `Stop` | `stop` | (nested `{offset,stop_color,stop_opacity}` in stops[]) | — | color/opacity live in `.style` (`stop-color`/`stop-opacity`), not attrs. |
| `Pattern` | `pattern` | `define_pattern(x,y,width,height,content[],units?,transform?,href?)` | 2 | tile = children; `get_effective_parent` for href-only tiles. |
| `MeshGradient`/`MeshRow`/`MeshPatch` | `meshgradient…` | `define_mesh_gradient(pos,rows,cols)` | 3 | `MeshGradient.new_mesh(...)`; advanced. |

Paint application: set a shape's `style['fill'] = "url(#<grad id>)"` (or assign the gradient
object on ≥1.2); read back via `style('fill', elem)` to resolve the live resource.

---

## 6. Filters  (`inkex.elements._filters`)

> All 24 `fe*` classes are **thin tag wrappers** (only `tag_name`); no typed attrs, no wiring
> helpers. The **facade owns** per-primitive attribute schemas, `in`/`in2`/`result` wiring, and
> composite recipes. inkex has **no `feDropShadow`** — we synthesize it.

Container: `Filter` (`filter`: x/y/width/height, filterUnits, primitiveUnits) +
`Filter.add_primitive(fe_type, **args)`.

| Tool | Tier | Backing primitives |
|---|---|---|
| `apply_blur(target,std_deviation)` | 1 | `feGaussianBlur` |
| `apply_drop_shadow(target,dx,dy,blur,color,opacity)` | 1 | **synthesized**: `feGaussianBlur`→`feOffset`→`feFlood`+`feComposite(in)`→`feMerge` |
| `apply_color_matrix(target,type,values)` | 1 | `feColorMatrix` (grayscale/saturate/hueRotate/tint/duotone) |
| `apply_color_overlay(target,color,opacity)` | 1 | `feFlood`+`feComposite` |
| `apply_blend(target,mode)` | 1 | `feBlend` |
| `define_filter(primitives[])` (raw graph) | 2 | any `fe*` + `feMerge/feMergeNode`, `feOffset` |
| `apply_morphology`, `apply_component_transfer`, `apply_turbulence` | 2 | `feMorphology`, `feComponentTransfer`+`feFunc{R,G,B,A}`, `feTurbulence` |
| advanced graph: displacement, convolve, lighting, image, tile | 3 | `feDisplacementMap`, `feConvolveMatrix`, `feDiffuse/SpecularLighting`+`fe{Distant,Point,Spot}Light`, `feImage`, `feTile` |

Full 24-class list (for the raw `define_filter` graph schema): GaussianBlur, Flood, Offset,
Merge, MergeNode, Composite, ColorMatrix, Blend, Morphology, ComponentTransfer, FuncR, FuncG,
FuncB, FuncA, Turbulence, DisplacementMap, Image, Tile, ConvolveMatrix, DiffuseLighting,
SpecularLighting, DistantLight, PointLight, SpotLight.

---

## 7. Raster images  (`inkex.elements._image`)  — required

| inkex API | SVG tag | Tool | Tier | Notes |
|---|---|---|---|---|
| `Image` (`RectangleBase`) | `image` | `add_image(href \| bytes, x,y,width,height, preserve_aspect_ratio?, embed?)` | 1 | external href or embedded data URI. |
| (manual embed — no `embed_image` in 1.4.1) | — | (the `embed=True` path) | 1 | base64-encode bytes, sniff MIME, set `xlink:href = data:{mime};base64,…`. We own this. |
| (extract) | — | `extract_image(target,path)` | 2 | manual: split `data:…;base64,` and `b64decode` (no inkex extract). |

---

## 8. Document furniture  (`inkex.elements._svg`, `_meta`)

| inkex class | SVG tag | Tool | Tier | Notes |
|---|---|---|---|---|
| `NamedView` | `sodipodi:namedview` | `add_guide`, `list_guides`, `add_page`, `list_pages`, `set_active_layer` | 2/3 | guides/pages/Inkscape state. |
| `Guide` | `sodipodi:guide` | (above) | 2 | `Guide.new(x,y,angle)`, `set_position`. |
| `Page` | `inkscape:page` | (above) | 3 | multi-page docs (1.2+). |
| `Metadata` | `metadata` | `set_metadata(field,value)` | 2 | RDF: title/creator/rights/date/… |
| `StyleElement` | `style` | (internal: named styles → CSS classes) | — | `set_text` CDATA stylesheet. |
| `Title`/`Desc` | `title`/`desc` | `set_title`, `set_description` (accessibility/semantics) | 2 | also our node `metadata`. |
| `Script`/`ForeignObject`/`Switch` | `script`/`foreignObject`/`switch` | — | 3 | out of scope (scripting/non-SVG). |

---

## 9. Support engines — internal, not tools  (`inkex.transforms/styles/colors/units`)

These back the `transform`/`style`/color/unit handling but are **not** their own tools:

- **`Transform`** — affine hexad `(a,b,c,d,e,f)`; `add_translate/scale/rotate/skewx/skewy`,
  compose with **`@`**, `apply_to_point`, `to_hexad`, `is_translate/scale/rotate`,
  `rotation_degrees`, `interpolate`. Backs `transform_node` and CTM queries.
- **`BoundingBox`** — ranges; `+`/`|` union, `&` intersection; left/right/top/bottom/center/
  width/height/area; `new_xywh`. Backs `get_bbox` and world-bbox aggregation.
- **`DirectedLineSegment` / `Vector2d` / `ImmutableVector2d`** — geometry helpers.
- **`Style`** — ordered CSS dict; `parse_str`/`to_str`, `update` (handles `!important`),
  `add_inherited`, `cascaded_style`/`specified_style`, `get_color`/`set_color`. Backs
  `define_style`, `restyle`, `get_computed_style`.
- **`Color`** (1.5 package) — `Color(value, alpha)`, `.to(ColorRGB/HSL/…)`, `can_parse`,
  `interpolate`; spaces RGB/HSL/HSV/CMYK/Named/None. Backs the `Paint`/`Color` schema types.
- **`inkex.units`** — `parse_unit`, `convert_unit`, `render_unit`, `CONVERSIONS`; plus the
  element `unittouu`/`uutounit`. Backs unit normalization on every coordinate input.

---

## 10. Deliberate exclusions / deferrals

Completionist means these are *conscious* calls, not oversights:

- **Per-path-command tools** — collapsed into one `PathCommand` union; 20 tools would be noise.
- **SVG fonts** (`SVGfont`/`FontFace`/`Glyph`) — niche; ordinary font needs go through `.style`.
- **Flowed text** (`FlowRoot…`) — Inkscape-specific, not universally rendered → Tier 3.
- **Mesh gradients** — Tier 3 (advanced, limited renderer support).
- **Lighting/convolve/displacement filters** — Tier 3 behind an "advanced" `define_filter`.
- **`script`/`foreignObject`/`switch`** — excluded (scripting / non-SVG / sanitized on import).
- **Pages/guides/namedview** — Tier 2/3 furniture, after the core authoring loop.

---

## 11. Phase-1 tool set (the minimal completion of the loop)

`create_document`, `describe_document`, `outline`, `export_svg`, `render`,
`add_rect/circle/ellipse/line/polyline/polygon`, `add_path`, `add_text`, `add_text_run`,
`add_text_on_path`, `add_image`, `create_group`, `create_layer`, `set_layer_state`,
`reparent`, `ungroup`, `reorder`, `transform_node`, `restyle`, `set_name`, `delete_node`,
`define_style`, `define_linear_gradient`, `define_radial_gradient`, `define_clip`,
`define_mask`, `apply_clip`, `apply_mask`, `get_bbox`, `get_computed_style`, `get_transform`,
`find`, `get_subtree`.
