"""FastMCP server entrypoint.

Exposes the document model (inkex-backed) and the render-and-see loop. Documents live in a
process-wide :class:`DocumentStore`, addressed by explicit ``document_id``. Tools validate
input (pydantic), call the ``ops``/``query`` layers, and return structured handles — plus, for
``render_document``, the rendered image the model iterates against.
"""

from __future__ import annotations

import contextlib
import functools
import inspect
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Literal
from weakref import WeakKeyDictionary

from fastmcp import Context, FastMCP
from fastmcp.server.dependencies import get_context
from mcp.server.session import ServerSession
from pydantic import AnyUrl, BaseModel

from . import ops
from .model.document import Document
from .query import convert_units as _convert_units
from .query import describe_document as _describe_document
from .query import describe_node as _describe_node
from .query import extract_image as _extract_image
from .query import find as _find
from .query import get_bbox as _get_bbox
from .query import get_computed_style as _get_computed_style
from .query import get_subtree as _get_subtree
from .query import get_transform as _get_transform
from .query import list_resources as _list_resources
from .query import outline as _outline
from .query.outline import OutlineNode
from .render import (
    SUPPORTED_FORMATS,
    available_backends,
    build_feedback,
    export_bytes,
    get_renderer,
    rsvg_available,
)
from .render.base import RenderRequest
from .render.feedback import MCPImage
from .schemas import FilterPrimitive, GradientStop, ShapeStyle
from .serialize import export_svg as _export_svg
from .session import DocumentStore
from .typeset import is_bold as _is_bold
from .typeset import list_font_families as _list_font_families
from .typeset import measure_text as _measure_text
from .typeset import parse_font_size as _parse_font_size


def _to_fe(primitive: FilterPrimitive) -> ops.FePrimitive:
    """Convert a validated FilterPrimitive into the ops-layer FePrimitive (recursively)."""
    return ops.FePrimitive(
        tag=primitive.tag,
        attrs=primitive.attrs,
        children=[_to_fe(child) for child in primitive.children],
    )


_INSTRUCTIONS = """\
svg-mcp authors SVG vector graphics as a structured, hierarchical document you build up with
tool calls and **see** by rendering to an image. Work in a tight loop: make a change, call
`render_document` to look at it, then adjust.

WORKFLOW
1. `create_document(width, height)` → returns a `document_id` and makes it the ACTIVE
   document. You may then omit `document_id` on later calls to target the active one.
2. Add content: `add_rect`/`add_circle`/`add_path`/`add_text`/… Each returns the new node's
   `{id, tag, name}` — keep the `id` (or give it a `name`) to refer back to the node.
3. Organize with `create_layer` / `create_group`; nest by passing a parent id.
4. `outline(document_id)` shows the current tree; `render_document(document_id)` returns the
   rendered PNG so you can visually verify. Iterate.
5. `export_svg(document_id)` returns the final SVG source.
6. WHEN THE ARTWORK IS COMPLETE (after you've iterated and verified it): if the user hasn't
   already said what they want done with the result, ASK before finishing — e.g. save to a file
   and where (`export_render` → png/jpeg/webp/pdf/ps/eps/svg), hand back the SVG source, or just
   leave it as the live document. Don't assume; don't silently stop with only an inline preview.

ACTIVE DOCUMENT
- The store tracks an *active* document (the most recently created or touched one). Omit
  `document_id` to operate on it; pass `document_id` explicitly to override (that doc then
  becomes active). Use `set_active_document` to switch deliberately.
- When juggling several documents, pass `document_id` explicitly to avoid ambiguity.

ORIENTING (contextualize queries)
- `current_context()` — active id, all open ids, and an outline of the active document. Call it
  to re-anchor after a long conversation when you may have lost track.
- `outline` — the document tree (names, kinds, ids); `describe_node(target)` — everything about
  one node (kind, world bbox, computed style, local + composed transform, parent) in one call.
- `list_resources()` — the gradients/patterns/filters/clips/masks/markers/symbols and named
  styles already defined, so you know what you can reference/reuse before defining new ones.
- `find(name=…)` to locate nodes, `get_subtree(target)` to read a branch, `get_bbox`/
  `get_transform`/`get_computed_style` for focused lookups.

TARGETING NODES & NAMING
- Every `target`/`parent`/`content` argument accepts a node's **id** (returned on creation) or
  its **friendly name** (the `name`/label you gave it).
- **Name the things you'll refer back to.** Give meaningful nodes and every group/layer a
  `name`, and reason in terms of names via `find(name=…)` / `outline`. Names are stable and
  legible; random ids (`rect_8f3a`) are easy to lose track of across a long session.
- **Keep names unique.** A name that matches more than one node is rejected (it won't silently
  pick one) — e.g. don't name a gradient and a shape the same thing. Disambiguate with a hierarchy
  path `ancestor/name` (each segment an id or name, matched down the ancestor chain) or the id.
- Omitting `parent` places a node at the document root. Pass a group/layer id to nest it.
- **Stacking:** later siblings paint on top. To order a node relative to another, use
  `reparent(target, above=<node>)` / `below=<node>` instead of counting child indices.

COORDINATES
- User units, origin at top-left, x→right, y→down. The viewBox defaults to `0 0 width height`.

STYLING & PAINT
- `style` is a structured object: `fill`, `stroke`, `stroke_width`, `opacity`, `fill_opacity`,
  `stroke_opacity`, `stroke_dasharray`, `stroke_linecap`, `stroke_linejoin`, plus typography
  (`font_family`, `font_size`, `font_weight`, `font_style`, `text_anchor`, `letter_spacing`, …).
- Keys accept **either** snake_case (`font_size`) **or** the CSS name (`font-size`); `font_size`
  also takes a bare number (px). Unknown/misspelled keys are REJECTED with an error (not dropped),
  so set the whole style in one call and trust it stuck.
- Colors accept hex (`#ff0000`), `rgb()/rgba()`, CSS names (`tomato`), or `none`.
- A fill/stroke may also reference a defined resource: `url(#<id>)` or the shorthand `@<name>`
  (e.g. a gradient/pattern you defined). Define the resource first, then use its id as the paint.
- `restyle` MERGES by default — it only changes the properties you pass, keeping the rest. Pass
  `replace=true` to discard the node's current style and set exactly what you provide.

REUSABLE RESOURCES (defs)
- `define_linear_gradient`/`define_radial_gradient`/`define_pattern` return an id — use it as a
  fill: `style={"fill": "url(#<id>)"}` or `@<name>`.
- `define_style(name, style)` creates a named CSS class; `apply_styles(target, [name])` applies it.
- `define_clip`/`define_mask`/`define_symbol`/`define_marker` take `content`: a list of EXISTING
  node ids, which are MOVED into the new resource. So: create the shapes first, then define the
  resource from them, then `apply_clip`/`apply_mask`/`apply_marker` (or `add_use` for a symbol).

TRANSFORMS
- Prefer the primitives, which compose onto a node's existing transform: `translate_node`,
  `rotate_node` (optional center point), `scale_node` (optional anchor point), `skew_node`.
- `apply_transform(target, "rotate(45 100 100)")` accepts any raw SVG transform string.
- Most construction tools also take a `transform` string applied at creation time.

FILTERS & EFFECTS
- Convenience: `apply_blur`, `apply_drop_shadow`, `apply_color_matrix`, `apply_color_overlay`,
  `apply_blend`, `apply_morphology`, `apply_turbulence`, `apply_displacement_map`.
- For full control, `define_filter` builds an arbitrary `fe*` primitive graph; attach with
  `apply_filter`. (SVG has no native feDropShadow — `apply_drop_shadow` synthesizes one.)

TEXT
- `add_text(x, y, content)` for a text block; `add_text_run` appends `<tspan>` runs. For multiple
  LINES, append runs with an absolute `x` and an incremental `dy` (e.g. `dy="1.2em"`) per line.
  `add_text_on_path` flows text along an existing path (pass the path's id).
- Fonts: `list_fonts()` returns the family names installed on this machine — pick one and set it
  via `style.font_family`. `font_size`, `font_weight` (`bold`/`700`), `font_style` (`italic`),
  `text_anchor` (start/middle/end), `letter_spacing`, `word_spacing`, `text_decoration`,
  `dominant_baseline` (vertical align), and `paint_order` are all settable on `style`.
- `measure_text(content, style)` returns `{width, height}` in user units from the font's own
  metrics — use it to fit/center text and size boxes BEFORE rendering (no render round-trip).
  inkex does not shape glyphs, so geometric bbox queries return empty for text; `measure_text`
  fills that gap (Latin-accurate; no kerning/complex shaping).
- `text_to_path(target)` converts a text/textPath node into a real outlined `<path>` (pure-Python
  via fontTools, honoring family/weight/italic/anchor and text-on-path). Use it to make output
  font-independent (renders identically anywhere) or to then manipulate the glyph geometry.

RENDERING & EXPORT
- `render_document` returns a short summary plus the rendered image (base64), via the resvg
  engine. Flowed text (`add_flowed_text`) and mesh gradients have limited/no rendering support;
  prefer `add_text`+`add_text_run` and linear/radial gradients for reliable output.
- `export_render(format, path)` writes a FILE: png/jpeg/webp (faithful raster via resvg),
  pdf/ps/eps (true vector via librsvg), or svg (source). `export_formats()` lists them and reports
  whether vector export is available. cairo is intentionally unused (it drops SVG filters).

RESOURCES
- The server also publishes read-only resources the host may surface as context:
  `svg://documents` (index of open docs + which is active), `svg://{document_id}/svg` (live SVG
  source), and `svg://{document_id}/render` (a PNG preview). Reading them never changes state.

Multiple documents can be open at once; omit `document_id` for the active one, or pass it
explicitly to target a specific document.
"""

mcp: FastMCP = FastMCP(name="svg-mcp", instructions=_INSTRUCTIONS)

_DEFAULT_STORE = DocumentStore()
# Per-connection document stores, keyed by the MCP session OBJECT. MCP exposes no session-close
# hook, so we lean on GC: when the connection ends and the session object is collected, its store
# entry is released automatically — that is what the WeakKeyDictionary buys us. The stable
# `session_id` string identifies the same connection for logging/observability (see
# `_session_id`); we deliberately do NOT key on it, to keep the automatic cleanup.
_SESSION_STORES: WeakKeyDictionary[ServerSession, DocumentStore] = WeakKeyDictionary()


def _store() -> DocumentStore:
    """Resolve the DocumentStore for the current MCP session (per-connection isolation).

    Each client connection gets its own store, keyed by the session object and released by GC when
    the connection ends. Outside a request (direct/programmatic use) a shared default is used.
    """
    try:
        session = get_context().session
    except Exception:
        return _DEFAULT_STORE
    store = _SESSION_STORES.get(session)
    if store is None:
        store = DocumentStore()
        _SESSION_STORES[session] = store
    return store


def _session_id() -> str | None:
    """The stable MCP session id for the current connection (for logging/observability)."""
    try:
        return get_context().session_id
    except Exception:
        return None


Point = tuple[float, float]


class VariableWidthStroke(BaseModel):
    """One variable-width ribbon for the bulk `add_variable_width_paths` tool."""

    points: list[Point]
    widths: list[float] | float
    closed: bool = False
    cap: Literal["butt", "round"] = "butt"
    interpolation: Literal["linear", "cubic"] = "linear"
    samples: int = 8
    style: ShapeStyle | None = None
    name: str | None = None


class RectSpec(BaseModel):
    """One rectangle for the bulk `add_rects` tool."""

    x: float
    y: float
    width: float
    height: float
    rx: float | None = None
    ry: float | None = None
    style: ShapeStyle | None = None
    name: str | None = None


class CircleSpec(BaseModel):
    """One circle for the bulk `add_circles` tool."""

    cx: float
    cy: float
    r: float
    style: ShapeStyle | None = None
    name: str | None = None


class LineSpec(BaseModel):
    """One line segment for the bulk `add_lines` tool."""

    x1: float
    y1: float
    x2: float
    y2: float
    style: ShapeStyle | None = None
    name: str | None = None


class PathSpec(BaseModel):
    """One path for the bulk `add_paths` tool."""

    d: str
    style: ShapeStyle | None = None
    name: str | None = None


def _doc(document_id: str | None) -> Document:
    return _store().get(document_id)


def _style(style: ShapeStyle | None) -> dict[str, str] | None:
    return style.to_style_dict() if style is not None else None


async def _emit_change(ctx: Context) -> None:
    """Notify subscribers that the active document's resources changed."""
    session = ctx.session
    active = _store().active_id
    if active is not None:
        await session.send_resource_updated(AnyUrl(f"svg://{active}/svg"))
        await session.send_resource_updated(AnyUrl(f"svg://{active}/render"))
    await session.send_resource_updated(AnyUrl("svg://documents"))


def emits_change[**P, R](fn: Callable[P, R]) -> Callable[P, Awaitable[R]]:
    """Wrap a mutating tool so it emits resource-change notifications after it runs.

    The request Context is injected by FastMCP via an added keyword-only ``ctx`` parameter
    (excluded from the tool's input schema). If it is not supplied, fall back to the ambient
    request context via ``get_context()``. Notification failures never affect the tool result.
    """

    @functools.wraps(fn)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        ctx = kwargs.pop("ctx", None)  # injected by FastMCP; not one of fn's own params
        result = fn(*args, **kwargs)
        if ctx is None:
            with contextlib.suppress(Exception):
                ctx = get_context()
        if isinstance(ctx, Context):
            with contextlib.suppress(Exception):
                await _emit_change(ctx)
        return result

    signature = inspect.signature(fn)
    params = list(signature.parameters.values())
    params.append(
        inspect.Parameter(
            "ctx", inspect.Parameter.KEYWORD_ONLY, default=None, annotation=Context | None
        )
    )
    wrapper.__signature__ = signature.replace(parameters=params)  # type: ignore[attr-defined]
    return wrapper


# --- session / document ----------------------------------------------------


@mcp.tool
@emits_change
def create_document(
    width: float, height: float, viewbox: str | None = None
) -> dict[str, str | float | bool]:
    """Create a new, empty SVG document and register it in the session.

    Args:
        width: Canvas width in user units.
        height: Canvas height in user units. The viewBox defaults to "0 0 width height".
        viewbox: Optional explicit viewBox ("minX minY width height") to override the default.

    Returns:
        {document_id, width, height, active}. The new document becomes the active document, so
        you may omit document_id on subsequent calls to target it.
    """
    document_id, _ = _store().create(width, height, viewbox)
    return {"document_id": document_id, "width": width, "height": height, "active": True}


@mcp.tool
@emits_change
def import_svg(*, svg: str | None = None, path: str | None = None) -> dict[str, str | bool]:
    """Load an existing SVG into a new session document, so you can render/inspect/edit it.

    Provide the source EITHER inline via `svg` OR from a file via `path` (preferred for large
    documents — avoids inlining the whole string). The imported document becomes active.

    Args:
        svg: Complete SVG source as a string.
        path: Filesystem path to an `.svg` file to read instead.

    Returns:
        {document_id, active}. The new document becomes active.
    """
    document_id = _store().register(ops.load_svg_document(svg=svg, path=path))
    return {"document_id": document_id, "active": True}


@mcp.tool
def list_documents() -> list[str]:
    """List the ids of all open documents in the session.

    Returns:
        A list of document_id strings.
    """
    return _store().list_ids()


@mcp.tool
@emits_change
def set_active_document(document_id: str) -> dict[str, str]:
    """Make a document the active one, so later calls may omit document_id to target it.

    Args:
        document_id: The document to activate.

    Returns:
        {active: <document_id>}.
    """
    return {"active": _store().set_active(document_id)}


@mcp.tool
def current_context() -> dict[str, str | list[str] | OutlineNode | None]:
    """Report the working context so you can re-anchor (e.g. after a long conversation).

    Returns:
        {session_id, active_document, open_documents, active_outline}: the stable id of this
        connection (each chat is isolated to its own documents), the active document id (or None),
        all open document ids, and a depth-limited outline of the active document (or None).
    """
    active = _store().active_id
    outline_summary = _outline(_store().get(active), depth=2) if active is not None else None
    return {
        "session_id": _session_id(),
        "active_document": active,
        "open_documents": _store().list_ids(),
        "active_outline": outline_summary,
    }


@mcp.tool
@emits_change
def delete_document(*, document_id: str | None = None) -> str:
    """Delete an open document and free its resources.

    Returns:
        The deleted document_id.
    """
    return _store().delete(document_id)


@mcp.tool
def export_svg(*, document_id: str | None = None) -> str:
    """Serialize a document to SVG source text.

    Returns:
        The complete SVG document as a string.
    """
    return _export_svg(_doc(document_id))


@mcp.tool
def outline(
    *,
    document_id: str | None = None,
    root: str | None = None,
    depth: int | None = None,
    include_bbox: bool = False,
) -> OutlineNode:
    """Return the document's structural tree — the map you use to orient and find nodes.

    Each node reports its id, tag, kind (document/layer/group/shape) and name. Non-visual
    furniture (defs, namedview, metadata) is omitted.

    Args:
        root: Limit the outline to this node's subtree (id or name); omit for the whole document.
        depth: Max depth to expand; deeper subtrees collapse to a children_count.
        include_bbox: Include each node's world-absolute [x, y, w, h] bounding box.

    Returns:
        A nested node: {id, tag, kind, name?, bbox?, children? | children_count?}.
    """
    return _outline(_doc(document_id), root=root, depth=depth, include_bbox=include_bbox)


@mcp.tool
def get_bbox(*, document_id: str | None = None, target: str) -> dict[str, float] | None:
    """Return a node's world-absolute bounding box (ancestor transforms applied).

    Note: text has no geometric bbox (glyphs are not shaped until render) — judge text size by
    rendering instead.

    Args:
        target: Node id or friendly name.

    Returns:
        {x, y, width, height}, or null if the node has no geometry.
    """
    return _get_bbox(_doc(document_id), target)


# --- construction ----------------------------------------------------------


@mcp.tool
@emits_change
def add_rect(
    *,
    document_id: str | None = None,
    x: float,
    y: float,
    width: float,
    height: float,
    rx: float | None = None,
    ry: float | None = None,
    parent: str | None = None,
    name: str | None = None,
    style: ShapeStyle | None = None,
    transform: str | None = None,
) -> dict[str, str | None]:
    """Add a rectangle to the document.

    Args:
        x: Left edge in user units (origin top-left, y increases downward).
        y: Top edge in user units.
        width: Width in user units.
        height: Height in user units.
        rx: Optional horizontal corner radius for rounded corners.
        ry: Optional vertical corner radius (defaults to rx).
        parent: Group/layer id (or name) to nest under; omit for the document root.
        name: Friendly label for later reference by name.
        style: Fill/stroke/etc. Fill may be a color or a paint ref (url(#id) or @name).
        transform: Optional SVG transform string applied at creation.

    Returns:
        The new node's {id, tag, name}.
    """
    return ops.add_rect(
        _doc(document_id),
        x=x,
        y=y,
        width=width,
        height=height,
        rx=rx,
        ry=ry,
        parent=parent,
        name=name,
        style=_style(style),
        transform=transform,
    ).as_dict()


@mcp.tool
@emits_change
def add_circle(
    *,
    document_id: str | None = None,
    cx: float,
    cy: float,
    r: float,
    parent: str | None = None,
    name: str | None = None,
    style: ShapeStyle | None = None,
    transform: str | None = None,
) -> dict[str, str | None]:
    """Add a circle to the document.

    Args:
        cx: Center x in user units.
        cy: Center y in user units.
        r: Radius.
        parent: Group/layer id (or name); omit for the document root.
        name: Friendly label.
        style: Fill/stroke/etc; fill may be a color or paint ref (url(#id) or @name).
        transform: Optional SVG transform string.

    Returns:
        The new node's {id, tag, name}.
    """
    return ops.add_circle(
        _doc(document_id),
        cx=cx,
        cy=cy,
        r=r,
        parent=parent,
        name=name,
        style=_style(style),
        transform=transform,
    ).as_dict()


@mcp.tool
@emits_change
def add_ellipse(
    *,
    document_id: str | None = None,
    cx: float,
    cy: float,
    rx: float,
    ry: float,
    parent: str | None = None,
    name: str | None = None,
    style: ShapeStyle | None = None,
    transform: str | None = None,
) -> dict[str, str | None]:
    """Add an ellipse to the document.

    Args:
        cx: Center x in user units.
        cy: Center y in user units.
        rx: Horizontal radius.
        ry: Vertical radius.
        parent: Group/layer id (or name); omit for the document root.
        name: Friendly label.
        style: Fill/stroke/etc; fill may be a color or paint ref (url(#id) or @name).
        transform: Optional SVG transform string.

    Returns:
        The new node's {id, tag, name}.
    """
    return ops.add_ellipse(
        _doc(document_id),
        cx=cx,
        cy=cy,
        rx=rx,
        ry=ry,
        parent=parent,
        name=name,
        style=_style(style),
        transform=transform,
    ).as_dict()


@mcp.tool
@emits_change
def add_line(
    *,
    document_id: str | None = None,
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    parent: str | None = None,
    name: str | None = None,
    style: ShapeStyle | None = None,
    transform: str | None = None,
) -> dict[str, str | None]:
    """Add a straight line segment from (x1,y1) to (x2,y2).

    Give it a visible stroke via style (e.g. stroke and stroke_width); lines have no fill.

    Args:
        x1: Start x in user units.
        y1: Start y in user units.
        x2: End x in user units.
        y2: End y in user units.
        parent: Group/layer id (or name); omit for the document root.
        name: Friendly label.
        style: Stroke color/width/etc.
        transform: Optional SVG transform string.

    Returns:
        The new node's {id, tag, name}.
    """
    return ops.add_line(
        _doc(document_id),
        x1=x1,
        y1=y1,
        x2=x2,
        y2=y2,
        parent=parent,
        name=name,
        style=_style(style),
        transform=transform,
    ).as_dict()


@mcp.tool
@emits_change
def add_polyline(
    *,
    document_id: str | None = None,
    points: list[Point],
    parent: str | None = None,
    name: str | None = None,
    style: ShapeStyle | None = None,
    transform: str | None = None,
) -> dict[str, str | None]:
    """Add an open polyline through a list of points.

    Args:
        points: Ordered vertices as [[x, y], ...] in user units. The path is not closed.
        parent: Group/layer id (or name); omit for the document root.
        name: Friendly label.
        style: Fill/stroke/etc.
        transform: Optional SVG transform string.

    Returns:
        The new node's {id, tag, name}.
    """
    return ops.add_polyline(
        _doc(document_id),
        points=points,
        parent=parent,
        name=name,
        style=_style(style),
        transform=transform,
    ).as_dict()


@mcp.tool
@emits_change
def add_polygon(
    *,
    document_id: str | None = None,
    points: list[Point],
    parent: str | None = None,
    name: str | None = None,
    style: ShapeStyle | None = None,
    transform: str | None = None,
) -> dict[str, str | None]:
    """Add a closed polygon through a list of points.

    Args:
        points: Ordered vertices as [[x, y], ...] in user units; the shape is closed automatically.
        parent: Group/layer id (or name); omit for the document root.
        name: Friendly label.
        style: Fill/stroke/etc.
        transform: Optional SVG transform string.

    Returns:
        The new node's {id, tag, name}.
    """
    return ops.add_polygon(
        _doc(document_id),
        points=points,
        parent=parent,
        name=name,
        style=_style(style),
        transform=transform,
    ).as_dict()


@mcp.tool
@emits_change
def add_path(
    *,
    document_id: str | None = None,
    d: str,
    parent: str | None = None,
    name: str | None = None,
    style: ShapeStyle | None = None,
    transform: str | None = None,
) -> dict[str, str | None]:
    """Add an arbitrary path from SVG path data.

    Args:
        d: SVG path data, e.g. "M10,80 C40,10 65,10 95,80 S150,150 180,80".
        parent: Group/layer id (or name); omit for the document root.
        name: Friendly label.
        style: Fill/stroke/etc; fill may be a color or paint ref (url(#id) or @name).
        transform: Optional SVG transform string.

    Returns:
        The new node's {id, tag, name}.
    """
    return ops.add_path(
        _doc(document_id),
        d=d,
        parent=parent,
        name=name,
        style=_style(style),
        transform=transform,
    ).as_dict()


@mcp.tool
@emits_change
def add_variable_width_path(
    *,
    document_id: str | None = None,
    points: list[Point],
    widths: list[float] | float,
    closed: bool = False,
    cap: Literal["butt", "round"] = "butt",
    interpolation: Literal["linear", "cubic"] = "linear",
    samples: int = 8,
    parent: str | None = None,
    name: str | None = None,
    style: ShapeStyle | None = None,
    transform: str | None = None,
) -> dict[str, str | None]:
    """Expand a polyline into a VARIABLE-WIDTH line — a filled ribbon that swells and tapers.

    SVG `stroke-width` is constant per element, so true variable-width lines (calligraphy,
    engraving, brush strokes, tapered arrows) must be drawn as a FILL. This offsets the centerline
    by ±half-width at each vertex and emits the closed ribbon outline. Set `style.fill` to the ink
    color (stroke is not used for the body).

    Args:
        points: Centerline vertices as [[x, y], ...] in user units (≥ 2 points).
        widths: Full stroke width at each vertex — a list matching `points`, or a single number
            for a constant width.
        closed: Treat the centerline as a loop (annular ribbon); otherwise open with end caps.
        cap: End cap for open ribbons — "butt" (flat) or "round" (semicircular).
        interpolation: "linear" (straight segments) or "cubic" — a Catmull-Rom spline through the
            vertices that smooths BOTH the path and the width (turns jagged input into flowing
            strokes; great for hand-traced or sparse centerlines).
        samples: Sub-segments per span for cubic interpolation (higher = smoother, default 8).
        parent: Group/layer id (or name); omit for the document root.
        name: Friendly label.
        style: Fill/etc; fill may be a color or paint ref (url(#id) or @name).
        transform: Optional SVG transform string.

    Returns:
        The new node's {id, tag, name}.
    """
    width_list = [float(widths)] * len(points) if isinstance(widths, int | float) else widths
    return ops.add_variable_width_path(
        _doc(document_id),
        points=points,
        widths=width_list,
        closed=closed,
        cap=cap,
        interpolation=interpolation,
        samples=samples,
        parent=parent,
        name=name,
        style=_style(style),
        transform=transform,
    ).as_dict()


@mcp.tool
@emits_change
def add_variable_width_paths(
    *,
    document_id: str | None = None,
    strokes: list[VariableWidthStroke],
    parent: str | None = None,
) -> dict[str, int | list[str]]:
    """Add MANY variable-width ribbons in ONE call — a bulk version of add_variable_width_path.

    Construction tools never auto-render (they return handles; rendering is on-demand via
    render_document), so the per-shape cost is just a round-trip. This batches many ribbons into a
    single call to avoid N round-trips — ideal for procedural art (engraving, hatching, fields of
    strokes). Each stroke is its own filled path node.

    Args:
        strokes: A list of {points, widths, closed?, cap?, style?, name?} — each as in
            add_variable_width_path. `widths` may be a list (per vertex) or a single number.
        parent: Group/layer id (or name) to nest all of them under; omit for the document root.

    Returns:
        {count, ids}: how many were added and their node ids (in order).
    """
    doc = _doc(document_id)
    ids: list[str] = []
    for s in strokes:
        widths = (
            [float(s.widths)] * len(s.points) if isinstance(s.widths, int | float) else s.widths
        )
        ref = ops.add_variable_width_path(
            doc,
            points=s.points,
            widths=widths,
            closed=s.closed,
            cap=s.cap,
            interpolation=s.interpolation,
            samples=s.samples,
            parent=parent,
            name=s.name,
            style=_style(s.style),
        )
        ids.append(ref.id)
    return {"count": len(ids), "ids": ids}


@mcp.tool
@emits_change
def add_rects(
    *, document_id: str | None = None, rects: list[RectSpec], parent: str | None = None
) -> dict[str, int | list[str]]:
    """Add MANY rectangles in one call (bulk add_rect) — one round-trip instead of N.

    Args:
        rects: A list of {x, y, width, height, rx?, ry?, style?, name?}.
        parent: Group/layer id (or name) to nest them all under; omit for the document root.

    Returns:
        {count, ids} — how many were added and their node ids, in order.
    """
    doc = _doc(document_id)
    ids = [
        ops.add_rect(
            doc,
            x=r.x,
            y=r.y,
            width=r.width,
            height=r.height,
            rx=r.rx,
            ry=r.ry,
            parent=parent,
            name=r.name,
            style=_style(r.style),
        ).id
        for r in rects
    ]
    return {"count": len(ids), "ids": ids}


@mcp.tool
@emits_change
def add_circles(
    *, document_id: str | None = None, circles: list[CircleSpec], parent: str | None = None
) -> dict[str, int | list[str]]:
    """Add MANY circles in one call (bulk add_circle) — ideal for dot fields / scatter plots.

    Args:
        circles: A list of {cx, cy, r, style?, name?}.
        parent: Group/layer id (or name) to nest them all under; omit for the document root.

    Returns:
        {count, ids} — how many were added and their node ids, in order.
    """
    doc = _doc(document_id)
    ids = [
        ops.add_circle(
            doc, cx=c.cx, cy=c.cy, r=c.r, parent=parent, name=c.name, style=_style(c.style)
        ).id
        for c in circles
    ]
    return {"count": len(ids), "ids": ids}


@mcp.tool
@emits_change
def add_lines(
    *, document_id: str | None = None, lines: list[LineSpec], parent: str | None = None
) -> dict[str, int | list[str]]:
    """Add MANY line segments in one call (bulk add_line) — grids, hatching, axes.

    Args:
        lines: A list of {x1, y1, x2, y2, style?, name?}.
        parent: Group/layer id (or name) to nest them all under; omit for the document root.

    Returns:
        {count, ids} — how many were added and their node ids, in order.
    """
    doc = _doc(document_id)
    ids = [
        ops.add_line(
            doc,
            x1=ln.x1,
            y1=ln.y1,
            x2=ln.x2,
            y2=ln.y2,
            parent=parent,
            name=ln.name,
            style=_style(ln.style),
        ).id
        for ln in lines
    ]
    return {"count": len(ids), "ids": ids}


@mcp.tool
@emits_change
def add_paths(
    *, document_id: str | None = None, paths: list[PathSpec], parent: str | None = None
) -> dict[str, int | list[str]]:
    """Add MANY paths in one call (bulk add_path) — procedural/vector art with one round-trip.

    Args:
        paths: A list of {d, style?, name?} where d is SVG path data.
        parent: Group/layer id (or name) to nest them all under; omit for the document root.

    Returns:
        {count, ids} — how many were added and their node ids, in order.
    """
    doc = _doc(document_id)
    ids = [
        ops.add_path(doc, d=p.d, parent=parent, name=p.name, style=_style(p.style)).id
        for p in paths
    ]
    return {"count": len(ids), "ids": ids}


@mcp.tool
@emits_change
def add_text(
    *,
    document_id: str | None = None,
    x: float,
    y: float,
    content: str,
    parent: str | None = None,
    name: str | None = None,
    style: ShapeStyle | None = None,
    transform: str | None = None,
) -> dict[str, str | None]:
    """Add a single-line text element anchored at (x, y).

    For multiple lines or styled spans, follow up with add_text_run on the returned id. Set the
    font via style (font-family, font-size, font-weight, text-anchor, fill). Text is shaped at
    render time, so judge size/fit with render_document rather than get_bbox.

    Args:
        x: Anchor x in user units.
        y: Baseline y in user units.
        content: The text string.
        parent: Group/layer id (or name); omit for the document root.
        name: Friendly label.
        style: Font and fill properties (e.g. font-size as "24px").
        transform: Optional SVG transform string.

    Returns:
        The new node's {id, tag, name}.
    """
    return ops.add_text(
        _doc(document_id),
        x=x,
        y=y,
        content=content,
        parent=parent,
        name=name,
        style=_style(style),
        transform=transform,
    ).as_dict()


@mcp.tool
@emits_change
def create_group(
    *,
    document_id: str | None = None,
    name: str | None = None,
    parent: str | None = None,
    children: list[str] | None = None,
    transform: str | None = None,
) -> dict[str, str | None]:
    """Create a <g> group, optionally moving existing nodes into it.

    Groups let you transform, style, or reorder several nodes together.

    Args:
        name: Friendly label for the group.
        parent: Group/layer id (or name) to nest the group under; omit for the document root.
        children: Existing node ids/names to move into the new group.
        transform: Optional SVG transform string applied to the whole group.

    Returns:
        The new group's {id, tag, name}.
    """
    return ops.create_group(
        _doc(document_id),
        name=name,
        parent=parent,
        children=children,
        transform=transform,
    ).as_dict()


@mcp.tool
@emits_change
def create_layer(
    *, document_id: str | None = None, name: str, parent: str | None = None
) -> dict[str, str | None]:
    """Create a layer — a named group that acts as a composition plane.

    Layers support visibility/lock/opacity via set_layer_state and are listed by list_layers.

    Args:
        name: Layer label (shown in the outline and Inkscape).
        parent: Usually omitted (layers are top-level); a parent id nests it.

    Returns:
        The new layer's {id, tag, name}.
    """
    return ops.create_layer(_doc(document_id), name=name, parent=parent).as_dict()


# --- modification ----------------------------------------------------------


@mcp.tool
@emits_change
def set_name(*, document_id: str | None = None, target: str, name: str) -> dict[str, str | None]:
    """Set a node's friendly name (its inkscape:label) so you can target it by name later.

    Args:
        target: Node id or current name.
        name: New friendly name.

    Returns:
        The node's {id, tag, name}.
    """
    return ops.set_name(_doc(document_id), target, name).as_dict()


@mcp.tool
@emits_change
def delete_node(*, document_id: str | None = None, target: str) -> str:
    """Delete a node (and its descendants) from the document.

    Args:
        target: Node id or name to remove.

    Returns:
        The deleted node's id.
    """
    return ops.delete_node(_doc(document_id), target)


@mcp.tool
@emits_change
def restyle(
    *, document_id: str | None = None, target: str, style: ShapeStyle, replace: bool = False
) -> dict[str, str | None]:
    """Update a node's presentation style — MERGE by default, or REPLACE.

    By default (`replace=false`) this is a partial edit: only the properties you pass are changed,
    and every other existing style property is kept. Pass `replace=true` to discard the node's
    entire current style and set it to exactly what you provide here.

    Args:
        target: Node id or name.
        style: The properties to set/override (fill, stroke, stroke_width, opacity, …). Fill/stroke
            may be a color or a paint ref (url(#id) or @name). Properties you omit are left as-is
            when merging, or dropped when replacing.
        replace: false (default) = merge into the existing style; true = replace it wholesale.

    Returns:
        The node's {id, tag, name}.
    """
    style_dict = style.to_style_dict()
    return ops.restyle(_doc(document_id), target, style_dict, replace=replace).as_dict()


@mcp.tool
@emits_change
def reparent(
    *,
    document_id: str | None = None,
    target: str,
    new_parent: str | None = None,
    index: int | None = None,
    keep_world_position: bool = False,
    above: str | None = None,
    below: str | None = None,
) -> dict[str, str | None]:
    """Move a node under a different parent and/or restack it in the hierarchy.

    Args:
        target: Node id or name to move.
        new_parent: Destination group/layer id (or name); omit to move to the document root.
        index: Optional child index to insert at (controls stacking order within the parent).
        keep_world_position: If true, recompute the node's transform so it stays visually fixed
            despite the change of ancestor transforms.
        above: Place the node directly ON TOP OF this sibling (id/name) — SVG paints later
            siblings last, so the node is inserted just after it. Parent is taken from this node.
        below: Place the node directly BENEATH this sibling (id/name) — inserted just before it.
            Prefer `above`/`below` over counting `index`; they take precedence over
            new_parent/index.

    Returns:
        The node's {id, tag, name}.
    """
    return ops.reparent(
        _doc(document_id),
        target,
        new_parent,
        index,
        keep_world_position,
        above=above,
        below=below,
    ).as_dict()


@mcp.tool
@emits_change
def ungroup(*, document_id: str | None = None, target: str) -> list[str]:
    """Dissolve a group/layer, moving its children up into its parent.

    The group's transform is baked into each child so they keep their world position. The group
    itself is removed.

    Args:
        target: Group/layer id or name to dissolve.

    Returns:
        The ids of the freed (formerly child) nodes.
    """
    return ops.ungroup(_doc(document_id), target)


@mcp.tool
@emits_change
def duplicate(
    *, document_id: str | None = None, target: str, into: str | None = None
) -> dict[str, str | None]:
    """Duplicate a node (and its descendants), producing a copy with a fresh id.

    Args:
        target: Node id or name to copy.
        into: Optional parent id/name for the copy; otherwise it sits beside the original.

    Returns:
        The new copy's {id, tag, name}.
    """
    return ops.duplicate(_doc(document_id), target, into).as_dict()


@mcp.tool
@emits_change
def to_front(*, document_id: str | None = None, target: str) -> dict[str, str | None]:
    """Move a node to the top of its parent's stacking order (drawn last, appears on top).

    Args:
        target: Node id or name.

    Returns:
        The node's {id, tag, name}.
    """
    return ops.to_front(_doc(document_id), target).as_dict()


@mcp.tool
@emits_change
def to_back(*, document_id: str | None = None, target: str) -> dict[str, str | None]:
    """Move a node to the bottom of its parent's stacking order (drawn first, behind siblings).

    Args:
        target: Node id or name.

    Returns:
        The node's {id, tag, name}.
    """
    return ops.to_back(_doc(document_id), target).as_dict()


@mcp.tool
@emits_change
def raise_node(*, document_id: str | None = None, target: str) -> dict[str, str | None]:
    """Raise a node one step up its parent's stacking order (one sibling closer to the front).

    Args:
        target: Node id or name.

    Returns:
        The node's {id, tag, name}.
    """
    return ops.raise_node(_doc(document_id), target).as_dict()


@mcp.tool
@emits_change
def lower_node(*, document_id: str | None = None, target: str) -> dict[str, str | None]:
    """Lower a node one step down its parent's stacking order (one sibling closer to the back).

    Args:
        target: Node id or name.

    Returns:
        The node's {id, tag, name}.
    """
    return ops.lower_node(_doc(document_id), target).as_dict()


@mcp.tool
@emits_change
def translate_node(
    *, document_id: str | None = None, target: str, dx: float, dy: float
) -> dict[str, str | None]:
    """Translate a node by (dx, dy), composing onto its existing transform.

    Args:
        target: Node id or name.
        dx: Horizontal offset in user units.
        dy: Vertical offset in user units.

    Returns:
        The node's {id, tag, name}.
    """
    return ops.translate_node(_doc(document_id), target, dx, dy).as_dict()


@mcp.tool
@emits_change
def rotate_node(
    *, document_id: str | None = None, target: str, degrees: float, center: Point | None = None
) -> dict[str, str | None]:
    """Rotate a node, composing onto its existing transform.

    Args:
        target: Node id or name.
        degrees: Clockwise rotation angle.
        center: Optional pivot point [cx, cy] in user units; omit to rotate about the local origin.

    Returns:
        The node's {id, tag, name}.
    """
    return ops.rotate_node(_doc(document_id), target, degrees, center).as_dict()


@mcp.tool
@emits_change
def scale_node(
    *,
    document_id: str | None = None,
    target: str,
    sx: float,
    sy: float | None = None,
    center: Point | None = None,
) -> dict[str, str | None]:
    """Scale a node, composing onto its existing transform.

    Args:
        target: Node id or name.
        sx: Horizontal scale factor.
        sy: Vertical scale factor; defaults to sx (uniform scale).
        center: Optional anchor point [cx, cy] kept fixed during scaling; omit for the local origin.

    Returns:
        The node's {id, tag, name}.
    """
    return ops.scale_node(_doc(document_id), target, sx, sy, center).as_dict()


@mcp.tool
@emits_change
def skew_node(
    *, document_id: str | None = None, target: str, axis: Literal["x", "y"], degrees: float
) -> dict[str, str | None]:
    """Skew (shear) a node, composing onto its existing transform.

    Args:
        target: Node id or name.
        axis: "x" to skew horizontally or "y" to skew vertically.
        degrees: Skew angle.

    Returns:
        The node's {id, tag, name}.
    """
    return ops.skew_node(_doc(document_id), target, axis, degrees).as_dict()


@mcp.tool
@emits_change
def apply_transform(
    *, document_id: str | None = None, target: str, transform: str
) -> dict[str, str | None]:
    """Compose a raw SVG transform string onto a node (escape hatch for full control).

    Args:
        target: Node id or name.
        transform: Any SVG transform, e.g. "rotate(45 100 100)", "translate(10,5) scale(2)",
            "matrix(a,b,c,d,e,f)".

    Returns:
        The node's {id, tag, name}.
    """
    return ops.apply_transform(_doc(document_id), target, transform).as_dict()


# --- layers ----------------------------------------------------------------


@mcp.tool
def list_layers(*, document_id: str | None = None) -> list[dict[str, str | bool | None]]:
    """List the document's layers and their state.

    Returns:
        A list of {id, name, visible, locked}.
    """
    return ops.list_layers(_doc(document_id))


@mcp.tool
@emits_change
def set_layer_state(
    *,
    document_id: str | None = None,
    target: str,
    visible: bool | None = None,
    locked: bool | None = None,
    opacity: float | None = None,
) -> dict[str, str | None]:
    """Set a layer's (or group's) visibility, lock, and/or opacity. Only the given fields change.

    Args:
        target: Layer/group id or name.
        visible: True to show, False to hide (display:none).
        locked: True to lock against editing, False to unlock.
        opacity: Group opacity in 0.0-1.0.

    Returns:
        The node's {id, tag, name}.
    """
    return ops.set_layer_state(
        _doc(document_id), target, visible=visible, locked=locked, opacity=opacity
    ).as_dict()


@mcp.tool
@emits_change
def rename_layer(
    *, document_id: str | None = None, target: str, name: str
) -> dict[str, str | None]:
    """Rename a layer (sets its label).

    Args:
        target: Layer id or current name.
        name: New layer name.

    Returns:
        The layer's {id, tag, name}.
    """
    return ops.rename_layer(_doc(document_id), target, name).as_dict()


@mcp.tool
@emits_change
def move_to_layer(
    *, document_id: str | None = None, target: str, layer: str
) -> dict[str, str | None]:
    """Move a node into a layer (or any group).

    Args:
        target: Node id or name to move.
        layer: Destination layer/group id or name.

    Returns:
        The node's {id, tag, name}.
    """
    return ops.move_to_layer(_doc(document_id), target, layer).as_dict()


# --- text runs / text-on-path / image --------------------------------------


@mcp.tool
@emits_change
def add_text_run(
    *,
    document_id: str | None = None,
    parent: str,
    text: str,
    x: float | None = None,
    y: float | None = None,
    dx: float | None = None,
    dy: float | None = None,
    name: str | None = None,
    style: ShapeStyle | None = None,
) -> dict[str, str | None]:
    """Append a styled text run (<tspan>) to an existing text node.

    Use this for multi-line text (give a new absolute y, or a dy offset) or for differently
    styled spans within one text element.

    Args:
        parent: The text (or tspan) node id/name to append to.
        text: The run's text content.
        x: Optional absolute x for the run.
        y: Optional absolute y for the run (e.g. a new line's baseline).
        dx: Optional x offset relative to the preceding text.
        dy: Optional y offset relative to the preceding text.
        name: Friendly label.
        style: Font/fill overrides for this run.

    Returns:
        The new tspan's {id, tag, name}.
    """
    return ops.add_text_run(
        _doc(document_id),
        parent=parent,
        text=text,
        x=x,
        y=y,
        dx=dx,
        dy=dy,
        name=name,
        style=_style(style),
    ).as_dict()


@mcp.tool
@emits_change
def add_text_on_path(
    *,
    document_id: str | None = None,
    path: str,
    content: str,
    x: float | None = None,
    y: float | None = None,
    start_offset: str | None = None,
    side: str | None = None,
    parent: str | None = None,
    name: str | None = None,
    style: ShapeStyle | None = None,
) -> dict[str, str | None]:
    """Add text that flows along an existing path.

    Create the path first, then pass its id here. The path must already exist in the document.

    Args:
        path: Id or name of the path to flow text along.
        content: The text string.
        x: Optional x for the wrapping text element.
        y: Optional y for the wrapping text element.
        start_offset: Where along the path the text starts, e.g. "25%" or a length.
        side: Optional "left" or "right" of the path (SVG2).
        parent: Group/layer id (or name); omit for the document root.
        name: Friendly label.
        style: Font/fill properties.

    Returns:
        The new text node's {id, tag, name}.
    """
    return ops.add_text_on_path(
        _doc(document_id),
        path=path,
        content=content,
        x=x,
        y=y,
        start_offset=start_offset,
        side=side,
        parent=parent,
        name=name,
        style=_style(style),
    ).as_dict()


@mcp.tool
@emits_change
def add_image(
    *,
    document_id: str | None = None,
    x: float,
    y: float,
    width: float,
    height: float,
    href: str | None = None,
    data_base64: str | None = None,
    path: str | None = None,
    mime: str | None = None,
    preserve_aspect_ratio: str | None = None,
    parent: str | None = None,
    name: str | None = None,
    transform: str | None = None,
) -> dict[str, str | None]:
    """Add a raster image. Provide exactly one source: href, data_base64, or path.

    Args:
        x: Left placement in user units.
        y: Top placement in user units.
        width: Display width in user units.
        height: Display height in user units.
        href: External URL or pre-built data URI to reference (not embedded).
        data_base64: Base64-encoded image bytes to embed as a data URI (set mime too).
        path: Local file path to read and embed as a base64 data URI (mime is sniffed).
        mime: MIME type (e.g. "image/png"); used with data_base64, optional with path.
        preserve_aspect_ratio: SVG preserveAspectRatio value (e.g. "xMidYMid meet").
        parent: Group/layer id (or name); omit for the document root.
        name: Friendly label.
        transform: Optional SVG transform string.

    Returns:
        The new image node's {id, tag, name}.
    """
    return ops.add_image(
        _doc(document_id),
        x=x,
        y=y,
        width=width,
        height=height,
        href=href,
        data_base64=data_base64,
        path=path,
        mime=mime,
        preserve_aspect_ratio=preserve_aspect_ratio,
        parent=parent,
        name=name,
        transform=transform,
    ).as_dict()


# --- resources: named styles, gradients, clip/mask, filters ----------------


@mcp.tool
@emits_change
def define_style(*, document_id: str | None = None, name: str, style: ShapeStyle) -> str:
    """Define a reusable named style, emitted as a CSS class. Apply it with apply_styles.

    Args:
        name: Style/class name (used by apply_styles).
        style: The presentation properties this style sets.

    Returns:
        The style name.
    """
    return ops.define_style(_doc(document_id), name, style.to_style_dict())


@mcp.tool
@emits_change
def apply_styles(
    *, document_id: str | None = None, target: str, names: list[str]
) -> dict[str, str | None]:
    """Apply one or more named styles (defined via define_style) to a node, setting its class.

    Args:
        target: Node id or name.
        names: Style names to apply.

    Returns:
        The node's {id, tag, name}.
    """
    return ops.apply_styles(_doc(document_id), target, names).as_dict()


@mcp.tool
@emits_change
def define_linear_gradient(
    *,
    document_id: str | None = None,
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    stops: list[GradientStop],
    name: str | None = None,
    spread: str | None = None,
    units: str | None = None,
    gradient_transform: str | None = None,
) -> str:
    """Define a linear gradient. Use the returned id as a paint: fill "url(#id)" or "@name".

    Args:
        x1: Gradient start x. By default an objectBoundingBox fraction (0-1) of the filled shape;
            with units="userSpaceOnUse" it is an absolute user-unit coordinate.
        y1: Gradient start y (same units as x1).
        x2: Gradient end x (same units as x1).
        y2: Gradient end y (same units as x1).
        stops: Color stops, each {offset (0-1), color, opacity}.
        name: Friendly name, usable as the "@name" paint shorthand.
        spread: Edge behavior: "pad" | "reflect" | "repeat".
        units: "objectBoundingBox" (default) or "userSpaceOnUse".
        gradient_transform: Optional SVG transform applied to the gradient.

    Returns:
        The gradient's id.
    """
    return ops.define_linear_gradient(
        _doc(document_id),
        x1=x1,
        y1=y1,
        x2=x2,
        y2=y2,
        stops=[(s.offset, s.color, s.opacity) for s in stops],
        name=name,
        spread=spread,
        units=units,
        gradient_transform=gradient_transform,
    )


@mcp.tool
@emits_change
def define_radial_gradient(
    *,
    document_id: str | None = None,
    cx: float,
    cy: float,
    r: float,
    stops: list[GradientStop],
    fx: float | None = None,
    fy: float | None = None,
    name: str | None = None,
    spread: str | None = None,
    units: str | None = None,
    gradient_transform: str | None = None,
) -> str:
    """Define a radial gradient. Use the returned id as a paint: fill "url(#id)" or "@name".

    Args:
        cx: Center x. By default an objectBoundingBox fraction (0-1); absolute with
            units="userSpaceOnUse".
        cy: Center y (same units as cx).
        r: Radius (same units as cx).
        stops: Color stops, each {offset (0-1), color, opacity}.
        fx: Optional focal x; defaults to cx.
        fy: Optional focal y; defaults to cy.
        name: Friendly name, usable as the "@name" paint shorthand.
        spread: Edge behavior: "pad" | "reflect" | "repeat".
        units: "objectBoundingBox" (default) or "userSpaceOnUse".
        gradient_transform: Optional SVG transform applied to the gradient.

    Returns:
        The gradient's id.
    """
    return ops.define_radial_gradient(
        _doc(document_id),
        cx=cx,
        cy=cy,
        r=r,
        stops=[(s.offset, s.color, s.opacity) for s in stops],
        fx=fx,
        fy=fy,
        name=name,
        spread=spread,
        units=units,
        gradient_transform=gradient_transform,
    )


@mcp.tool
@emits_change
def define_clip(
    *,
    document_id: str | None = None,
    content: list[str],
    name: str | None = None,
    units: str | None = None,
) -> str:
    """Create a clipPath from existing shapes, then apply it with apply_clip.

    The listed content nodes are MOVED into the clipPath — create the clip shapes first. The
    clip is the intersection of those shapes.

    Args:
        content: Ids/names of existing shapes to use as the clip region.
        name: Friendly name for the clipPath.
        units: "userSpaceOnUse" (default) or "objectBoundingBox".

    Returns:
        The clipPath's id (pass it to apply_clip).
    """
    return ops.define_clip(_doc(document_id), content=content, name=name, units=units)


@mcp.tool
@emits_change
def define_mask(
    *,
    document_id: str | None = None,
    content: list[str],
    name: str | None = None,
    units: str | None = None,
) -> str:
    """Create a luminance mask from existing shapes, then apply it with apply_mask.

    The listed content nodes are MOVED into the mask — create them first. White areas show the
    masked node, black hides it; gradients in the mask produce soft/feathered edges.

    Args:
        content: Ids/names of existing shapes that define the mask.
        name: Friendly name for the mask.
        units: "objectBoundingBox" (default) or "userSpaceOnUse".

    Returns:
        The mask's id (pass it to apply_mask).
    """
    return ops.define_mask(_doc(document_id), content=content, name=name, units=units)


@mcp.tool
@emits_change
def apply_clip(*, document_id: str | None = None, target: str, clip: str) -> dict[str, str | None]:
    """Clip a node to a clipPath (created via define_clip).

    Args:
        target: Node id or name to clip.
        clip: clipPath id or name.

    Returns:
        The node's {id, tag, name}.
    """
    return ops.apply_clip(_doc(document_id), target, clip).as_dict()


@mcp.tool
@emits_change
def apply_mask(*, document_id: str | None = None, target: str, mask: str) -> dict[str, str | None]:
    """Apply a mask to a node (created via define_mask).

    Args:
        target: Node id or name to mask.
        mask: mask id or name.

    Returns:
        The node's {id, tag, name}.
    """
    return ops.apply_mask(_doc(document_id), target, mask).as_dict()


@mcp.tool
@emits_change
def clear_clip(*, document_id: str | None = None, target: str) -> dict[str, str | None]:
    """Remove a node's clip-path (undo apply_clip).

    Args:
        target: Node id or name.

    Returns:
        The node's {id, tag, name}.
    """
    return ops.clear_clip(_doc(document_id), target).as_dict()


@mcp.tool
@emits_change
def clear_mask(*, document_id: str | None = None, target: str) -> dict[str, str | None]:
    """Remove a node's mask (undo apply_mask).

    Args:
        target: Node id or name.

    Returns:
        The node's {id, tag, name}.
    """
    return ops.clear_mask(_doc(document_id), target).as_dict()


@mcp.tool
@emits_change
def apply_blur(
    *, document_id: str | None = None, target: str, std_deviation: float
) -> dict[str, str | None]:
    """Apply a Gaussian blur to a node (attaches a filter).

    Args:
        target: Node id or name.
        std_deviation: Blur radius (larger = blurrier).

    Returns:
        The node's {id, tag, name}.
    """
    return ops.apply_blur(_doc(document_id), target, std_deviation=std_deviation).as_dict()


@mcp.tool
@emits_change
def apply_drop_shadow(
    *,
    document_id: str | None = None,
    target: str,
    dx: float = 2,
    dy: float = 2,
    blur: float = 2,
    color: str = "#000000",
    opacity: float = 0.5,
) -> dict[str, str | None]:
    """Add a drop shadow to a node.

    SVG has no native feDropShadow; this synthesizes one (blur + offset + flood + composite +
    merge) and attaches it as a filter.

    Args:
        target: Node id or name.
        dx: Horizontal shadow offset in user units.
        dy: Vertical shadow offset in user units.
        blur: Shadow blur radius.
        color: Shadow color (hex/rgb/name).
        opacity: Shadow opacity in 0.0-1.0.

    Returns:
        The node's {id, tag, name}.
    """
    return ops.apply_drop_shadow(
        _doc(document_id), target, dx=dx, dy=dy, blur=blur, color=color, opacity=opacity
    ).as_dict()


@mcp.tool
@emits_change
def apply_color_matrix(
    *, document_id: str | None = None, target: str, type: str = "matrix", values: str | None = None
) -> dict[str, str | None]:
    """Apply a color-matrix filter (grayscale, saturation, hue-rotate, etc.).

    Args:
        target: Node id or name.
        type: "matrix" | "saturate" | "hueRotate" | "luminanceToAlpha".
        values: The values string for the chosen type — a 20-number matrix, a saturation amount
            (e.g. "0" for grayscale), or degrees for hueRotate. Omit for luminanceToAlpha.

    Returns:
        The node's {id, tag, name}.
    """
    return ops.apply_color_matrix(_doc(document_id), target, type=type, values=values).as_dict()


@mcp.tool
@emits_change
def apply_color_overlay(
    *, document_id: str | None = None, target: str, color: str, opacity: float = 1.0
) -> dict[str, str | None]:
    """Tint a node by flooding a color and compositing it inside the node's alpha.

    Args:
        target: Node id or name.
        color: Overlay color (hex/rgb/name).
        opacity: Overlay opacity in 0.0-1.0.

    Returns:
        The node's {id, tag, name}.
    """
    return ops.apply_color_overlay(
        _doc(document_id), target, color=color, opacity=opacity
    ).as_dict()


@mcp.tool
@emits_change
def apply_blend(*, document_id: str | None = None, target: str, mode: str) -> dict[str, str | None]:
    """Apply a blend mode to a node via a filter.

    Args:
        target: Node id or name.
        mode: e.g. "multiply", "screen", "overlay", "darken", "lighten", "color-dodge",
            "hard-light", "difference".

    Returns:
        The node's {id, tag, name}.
    """
    return ops.apply_blend(_doc(document_id), target, mode=mode).as_dict()


# --- inspection ------------------------------------------------------------


@mcp.tool
def describe_document(*, document_id: str | None = None) -> dict[str, str | int | None]:
    """Summarize a document at a glance.

    Returns:
        {width, height, viewBox, unit, layers, shapes}.
    """
    return _describe_document(_doc(document_id))


@mcp.tool
def get_computed_style(*, document_id: str | None = None, target: str) -> dict[str, str]:
    """Return a node's fully resolved presentation style (cascade + inheritance applied).

    Args:
        target: Node id or name.

    Returns:
        A flat map of CSS property -> value (e.g. {"fill": "url(#g)", "stroke-width": "2"}).
    """
    return _get_computed_style(_doc(document_id), target)


@mcp.tool
def get_transform(*, document_id: str | None = None, target: str) -> dict[str, str | list[float]]:
    """Return a node's local transform and its composed transform-to-root (CTM).

    Args:
        target: Node id or name.

    Returns:
        {local, composed} as transform strings, plus {local_matrix, composed_matrix} as
        [a, b, c, d, e, f] hexads.
    """
    return _get_transform(_doc(document_id), target)


@mcp.tool
def convert_units(*, document_id: str | None = None, value: str, to_unit: str) -> float:
    """Convert a length between units using the document's scale.

    Args:
        value: A length with a unit, e.g. "10mm", "1in", "72pt".
        to_unit: Target unit, e.g. "px", "mm", "in", "pt".

    Returns:
        The converted numeric value in to_unit.
    """
    return _convert_units(_doc(document_id), value, to_unit)


@mcp.tool
def describe_node(
    *, document_id: str | None = None, target: str
) -> dict[str, str | int | None | list[float] | dict[str, str]]:
    """Get everything about one node in a single call.

    Args:
        target: Node id or name to inspect.

    Returns:
        {id, name, tag, kind, parent, children, world_bbox, computed_style, transform} — the
        node's kind, world-absolute bbox, fully-cascaded style, and local + composed transforms.
    """
    return _describe_node(_doc(document_id), target)


@mcp.tool
def list_resources(*, document_id: str | None = None) -> dict[str, list[dict[str, str | None]]]:
    """List the reusable resources defined in the document, so you know what you can reference.

    Returns:
        Buckets of {id, name} per kind — gradients, patterns, filters, clips, masks, markers,
        symbols — plus named `styles` (CSS classes). Reference gradients/patterns as a fill
        `url(#id)` or `@name`; attach clips/masks/filters/markers with the matching apply_* tool.
    """
    return _list_resources(_doc(document_id))


@mcp.tool
def list_fonts() -> list[str]:
    """List the font families installed on this system, usable as a text style's font-family.

    Returns:
        Sorted proper-case family names (e.g. "Helvetica", "Menlo"). Set one via a text node's
        style font-family when creating text.
    """
    return _list_font_families()


@mcp.tool
@emits_change
def text_to_path(*, document_id: str | None = None, target: str) -> dict[str, str | None]:
    """Convert a text node into an outlined <path> (font-independent, usable in clips/booleans).

    Bakes the glyph geometry in place, preserving the node's id/name/transform/paint. Pure
    Python (fontTools). Outlines a single run's direct text; tspans aren't flattened.

    Args:
        target: The text node id or name to outline.

    Returns:
        The new path's {id, tag, name}.
    """
    return ops.text_to_path(_doc(document_id), target).as_dict()


# --- path factories & path ops ---------------------------------------------


@mcp.tool
@emits_change
def add_arc(
    *,
    document_id: str | None = None,
    cx: float,
    cy: float,
    rx: float,
    ry: float | None = None,
    arctype: str = "arc",
    parent: str | None = None,
    name: str | None = None,
    style: ShapeStyle | None = None,
    transform: str | None = None,
) -> dict[str, str | None]:
    """Add an elliptical arc, pie slice, or chord as a path.

    Args:
        cx: Center x in user units.
        cy: Center y in user units.
        rx: Radius (x).
        ry: Optional radius (y); defaults to rx (circular).
        arctype: "arc" (open curve), "slice" (pie wedge), or "chord" (closed by a straight line).
        parent: Group/layer id (or name); omit for the document root.
        name: Friendly label.
        style: Fill/stroke/etc.
        transform: Optional SVG transform string.

    Returns:
        The new path node's {id, tag, name}.
    """
    return ops.add_arc(
        _doc(document_id),
        cx=cx,
        cy=cy,
        rx=rx,
        ry=ry,
        arctype=arctype,
        parent=parent,
        name=name,
        style=_style(style),
        transform=transform,
    ).as_dict()


@mcp.tool
@emits_change
def add_star(
    *,
    document_id: str | None = None,
    cx: float,
    cy: float,
    outer_radius: float,
    inner_radius: float,
    sides: int = 5,
    rounded: float = 0.0,
    flatsided: bool = False,
    parent: str | None = None,
    name: str | None = None,
    style: ShapeStyle | None = None,
    transform: str | None = None,
) -> dict[str, str | None]:
    """Add a star or regular polygon as a path.

    Args:
        cx: Center x in user units.
        cy: Center y in user units.
        outer_radius: Distance to the outer points.
        inner_radius: Distance to the inner vertices (ignored when flatsided=True).
        sides: Number of points/sides.
        rounded: Corner rounding amount (0 = sharp).
        flatsided: True for a regular polygon (uses only outer_radius); False for a star.
        parent: Group/layer id (or name); omit for the document root.
        name: Friendly label.
        style: Fill/stroke/etc.
        transform: Optional SVG transform string.

    Returns:
        The new path node's {id, tag, name}.
    """
    return ops.add_star(
        _doc(document_id),
        cx=cx,
        cy=cy,
        outer_radius=outer_radius,
        inner_radius=inner_radius,
        sides=sides,
        rounded=rounded,
        flatsided=flatsided,
        parent=parent,
        name=name,
        style=_style(style),
        transform=transform,
    ).as_dict()


@mcp.tool
@emits_change
def path_transform(
    *, document_id: str | None = None, target: str, transform: str
) -> dict[str, str | None]:
    """Bake an SVG transform into a path's data, rewriting its coordinates in place.

    Unlike transform_node (which sets a transform attribute), this modifies the "d" geometry
    directly and leaves the node transform unchanged. Path nodes only.

    Args:
        target: Path id or name.
        transform: SVG transform string, e.g. "translate(5,5)" or "scale(2)".

    Returns:
        The path node's {id, tag, name}.
    """
    return ops.path_transform(_doc(document_id), target, transform).as_dict()


@mcp.tool
@emits_change
def path_to_absolute(*, document_id: str | None = None, target: str) -> dict[str, str | None]:
    """Normalize a path's data to absolute commands (uppercase). Path nodes only.

    Args:
        target: Path id or name.

    Returns:
        The path node's {id, tag, name}.
    """
    return ops.path_to_absolute(_doc(document_id), target).as_dict()


@mcp.tool
@emits_change
def path_to_relative(*, document_id: str | None = None, target: str) -> dict[str, str | None]:
    """Normalize a path's data to relative commands (lowercase). Path nodes only.

    Args:
        target: Path id or name.

    Returns:
        The path node's {id, tag, name}.
    """
    return ops.path_to_relative(_doc(document_id), target).as_dict()


@mcp.tool
def path_bbox(*, document_id: str | None = None, target: str) -> dict[str, float] | None:
    """Return the bounding box of a path's raw data, ignoring any node transform. Path nodes only.

    For the on-canvas box including transforms, use get_bbox instead.

    Args:
        target: Path id or name.

    Returns:
        {x, y, width, height}, or null if empty.
    """
    return ops.path_bbox(_doc(document_id), target)


# --- symbols / use ---------------------------------------------------------


@mcp.tool
@emits_change
def define_symbol(
    *, document_id: str | None = None, content: list[str], name: str | None = None
) -> str:
    """Create a reusable <symbol> from existing nodes, then instantiate it with add_use.

    The listed content nodes are MOVED into the symbol (which is not drawn directly). Create
    them first.

    Args:
        content: Ids/names of existing nodes to move into the symbol.
        name: Friendly name for the symbol.

    Returns:
        The symbol's id (pass it to add_use).
    """
    return ops.define_symbol(_doc(document_id), content=content, name=name)


@mcp.tool
@emits_change
def add_use(
    *,
    document_id: str | None = None,
    target: str,
    x: float = 0,
    y: float = 0,
    parent: str | None = None,
    name: str | None = None,
    transform: str | None = None,
) -> dict[str, str | None]:
    """Place a <use> instance that references an existing node or symbol (reuse without copying).

    Args:
        target: Id or name of the node/symbol to instance.
        x: Horizontal offset applied to the instance.
        y: Vertical offset applied to the instance.
        parent: Group/layer id (or name); omit for the document root.
        name: Friendly label.
        transform: Optional SVG transform string.

    Returns:
        The new use node's {id, tag, name}.
    """
    return ops.add_use(
        _doc(document_id), target=target, x=x, y=y, parent=parent, name=name, transform=transform
    ).as_dict()


@mcp.tool
@emits_change
def unlink_use(*, document_id: str | None = None, target: str) -> dict[str, str | None]:
    """Expand a <use> instance into a real, independent copy of its referenced content.

    Args:
        target: The use node's id or name.

    Returns:
        The expanded node's {id, tag, name}.
    """
    return ops.unlink_use(_doc(document_id), target).as_dict()


# --- patterns / markers ----------------------------------------------------


@mcp.tool
@emits_change
def define_pattern(
    *,
    document_id: str | None = None,
    content: list[str],
    width: float,
    height: float,
    x: float = 0,
    y: float = 0,
    units: str | None = None,
    pattern_transform: str | None = None,
    name: str | None = None,
) -> str:
    """Create a tiling pattern from existing nodes. Use the returned id as a fill: "url(#id)".

    The listed content nodes are MOVED into the pattern tile — create them first.

    Args:
        content: Ids/names of existing nodes that make up one tile.
        width: Tile width.
        height: Tile height.
        x: Tile origin x offset.
        y: Tile origin y offset.
        units: "objectBoundingBox" or "userSpaceOnUse" (absolute tile size).
        pattern_transform: Optional SVG transform applied to the pattern.
        name: Friendly name, usable as the "@name" paint shorthand.

    Returns:
        The pattern's id.
    """
    return ops.define_pattern(
        _doc(document_id),
        content=content,
        width=width,
        height=height,
        x=x,
        y=y,
        units=units,
        pattern_transform=pattern_transform,
        name=name,
    )


@mcp.tool
@emits_change
def define_marker(
    *,
    document_id: str | None = None,
    content: list[str],
    ref_x: float = 0,
    ref_y: float = 0,
    marker_width: float = 10,
    marker_height: float = 10,
    orient: str = "auto",
    units: str = "strokeWidth",
    name: str | None = None,
) -> str:
    """Create a marker (arrowhead, dot, tick) from existing nodes, then attach with apply_marker.

    The listed content nodes are MOVED into the marker — create them first.

    Args:
        content: Ids/names of existing shapes drawn as the marker glyph.
        ref_x: Anchor x that sits on the path vertex.
        ref_y: Anchor y that sits on the path vertex.
        marker_width: Marker viewport width.
        marker_height: Marker viewport height.
        orient: "auto" (rotate to follow the path), "auto-start-reverse", or an angle in degrees.
        units: "strokeWidth" (scale with stroke) or "userSpaceOnUse".
        name: Friendly name.

    Returns:
        The marker's id (pass it to apply_marker).
    """
    return ops.define_marker(
        _doc(document_id),
        content=content,
        ref_x=ref_x,
        ref_y=ref_y,
        marker_width=marker_width,
        marker_height=marker_height,
        orient=orient,
        units=units,
        name=name,
    )


@mcp.tool
@emits_change
def apply_marker(
    *, document_id: str | None = None, target: str, marker: str, position: str = "end"
) -> dict[str, str | None]:
    """Attach a marker (from define_marker) to a path/line/polyline.

    Args:
        target: Path/line id or name.
        marker: Marker id or name.
        position: "start", "mid" (every interior vertex), or "end".

    Returns:
        The node's {id, tag, name}.
    """
    return ops.apply_marker(_doc(document_id), target, marker, position).as_dict()


# --- advanced filters ------------------------------------------------------


@mcp.tool
@emits_change
def define_filter(
    *, document_id: str | None = None, primitives: list[FilterPrimitive], name: str | None = None
) -> str:
    """Define a custom filter from a raw fe* primitive graph (full control), then apply_filter it.

    This reaches every SVG filter primitive (feGaussianBlur, feColorMatrix, feComposite,
    feDisplacementMap, feConvolveMatrix, lighting, feImage, feTile, ...). Wire primitives with
    their in/in2/result attributes.

    Args:
        primitives: An ordered list of FilterPrimitive {tag, attrs, children}. attrs are raw SVG
            attribute strings; children carry nested primitives (e.g. feMergeNode inside feMerge,
            feFuncR/G/B inside feComponentTransfer).
        name: Friendly name for the filter.

    Returns:
        The filter's id (pass it to apply_filter).
    """
    return ops.define_filter(
        _doc(document_id), primitives=[_to_fe(p) for p in primitives], name=name
    )


@mcp.tool
@emits_change
def apply_filter(
    *, document_id: str | None = None, target: str, filter: str
) -> dict[str, str | None]:
    """Attach an existing filter (from define_filter) to a node.

    Args:
        target: Node id or name.
        filter: Filter id or name.

    Returns:
        The node's {id, tag, name}.
    """
    return ops.apply_filter(_doc(document_id), target, filter).as_dict()


@mcp.tool
@emits_change
def apply_morphology(
    *, document_id: str | None = None, target: str, operator: str = "dilate", radius: float = 1
) -> dict[str, str | None]:
    """Thicken or thin a node's shapes with a morphology filter.

    Args:
        target: Node id or name.
        operator: "dilate" (grow/thicken) or "erode" (shrink/thin).
        radius: Amount in user units.

    Returns:
        The node's {id, tag, name}.
    """
    return ops.apply_morphology(
        _doc(document_id), target, operator=operator, radius=radius
    ).as_dict()


@mcp.tool
@emits_change
def apply_component_transfer(
    *,
    document_id: str | None = None,
    target: str,
    func_type: str = "table",
    table_values: str | None = None,
    slope: float | None = None,
    intercept: float | None = None,
) -> dict[str, str | None]:
    """Remap RGB channels with a component-transfer filter (levels, posterize, gamma).

    The same transfer is applied to the R, G, and B channels.

    Args:
        target: Node id or name.
        func_type: "table", "discrete", "linear", or "gamma".
        table_values: Space-separated values for table/discrete (e.g. "0 1" for a hard threshold).
        slope: Slope for the "linear" type.
        intercept: Intercept for the "linear" type.

    Returns:
        The node's {id, tag, name}.
    """
    return ops.apply_component_transfer(
        _doc(document_id),
        target,
        func_type=func_type,
        table_values=table_values,
        slope=slope,
        intercept=intercept,
    ).as_dict()


@mcp.tool
@emits_change
def apply_turbulence(
    *,
    document_id: str | None = None,
    target: str,
    base_frequency: float,
    num_octaves: int = 1,
    type: str = "fractalNoise",
    seed: int = 0,
) -> dict[str, str | None]:
    """Fill a node's shape with procedural Perlin noise (texture/clouds), clipped to its alpha.

    Args:
        target: Node id or name.
        base_frequency: Noise frequency (small = large soft blobs, large = fine grain).
        num_octaves: Detail levels to sum (more = richer texture).
        type: "fractalNoise" (smooth) or "turbulence" (wispy).
        seed: Random seed for reproducibility.

    Returns:
        The node's {id, tag, name}.
    """
    return ops.apply_turbulence(
        _doc(document_id),
        target,
        base_frequency=base_frequency,
        num_octaves=num_octaves,
        type=type,
        seed=seed,
    ).as_dict()


# --- metadata --------------------------------------------------------------


@mcp.tool
@emits_change
def set_title(*, document_id: str | None = None, target: str, text: str) -> dict[str, str | None]:
    """Set a node's <title> (accessibility label / hover tooltip).

    Args:
        target: Node id or name.
        text: The title text.

    Returns:
        The node's {id, tag, name}.
    """
    return ops.set_title(_doc(document_id), target, text).as_dict()


@mcp.tool
@emits_change
def set_description(
    *, document_id: str | None = None, target: str, text: str
) -> dict[str, str | None]:
    """Set a node's <desc> (accessibility long description).

    Args:
        target: Node id or name.
        text: The description text.

    Returns:
        The node's {id, tag, name}.
    """
    return ops.set_description(_doc(document_id), target, text).as_dict()


@mcp.tool
@emits_change
def set_document_metadata(
    *,
    document_id: str | None = None,
    title: str | None = None,
    creator: str | None = None,
    rights: str | None = None,
    date: str | None = None,
) -> dict[str, str | None]:
    """Set document-level metadata. Only the provided fields are written.

    Args:
        title: Document title.
        creator: Author/creator name.
        rights: Copyright/license statement.
        date: Date string.

    Returns:
        A map of the fields that were applied.
    """
    return ops.set_document_metadata(
        _doc(document_id), title=title, creator=creator, rights=rights, date=date
    )


# --- selectors -------------------------------------------------------------


@mcp.tool
def find(
    *,
    document_id: str | None = None,
    types: list[str] | None = None,
    name: str | None = None,
    name_contains: str | None = None,
    has_class: str | None = None,
    within: str | None = None,
) -> list[dict[str, str | None]]:
    """Find visual nodes matching ALL of the given predicates. Use to locate nodes to act on.

    Args:
        types: Restrict to these SVG tags, e.g. ["rect", "path"].
        name: Exact friendly-name match.
        name_contains: Substring match on the friendly name.
        has_class: Match nodes carrying this CSS class (see define_style/apply_styles).
        within: Restrict the search to this subtree (id or name).

    Returns:
        A list of matching {id, tag, name}.
    """
    return _find(
        _doc(document_id),
        types=types,
        name=name,
        name_contains=name_contains,
        has_class=has_class,
        within=within,
    )


@mcp.tool
def get_subtree(*, document_id: str | None = None, target: str) -> dict[str, str | OutlineNode]:
    """Extract a node's subtree for inspection — both rendered SVG and structured form.

    Args:
        target: Node id or name.

    Returns:
        {svg: the subtree's SVG fragment, outline: its structured node tree}.
    """
    return _get_subtree(_doc(document_id), target)


@mcp.tool
def extract_image(*, document_id: str | None = None, target: str) -> dict[str, str] | None:
    """Extract the embedded bytes of an <image> node that uses a data URI.

    Args:
        target: Image node id or name.

    Returns:
        {mime, data_base64}, or null if the image references an external URL.
    """
    return _extract_image(_doc(document_id), target)


# --- flowed text / links / mesh / advanced filter / pages (Tier 3) ---------


@mcp.tool
@emits_change
def add_flowed_text(
    *,
    document_id: str | None = None,
    x: float,
    y: float,
    width: float,
    height: float,
    paragraphs: list[str],
    parent: str | None = None,
    name: str | None = None,
    style: ShapeStyle | None = None,
) -> dict[str, str | None]:
    """Add Inkscape flowed (auto-wrapping) text in a rectangular region.

    Note: flowed text has limited renderer support — for reliably rendered text, prefer add_text
    plus add_text_run.

    Args:
        x: Flow region left in user units.
        y: Flow region top in user units.
        width: Flow region width.
        height: Flow region height; text wraps to fit.
        paragraphs: One string per paragraph.
        parent: Group/layer id (or name); omit for the document root.
        name: Friendly label.
        style: Font/fill properties.

    Returns:
        The new flowRoot node's {id, tag, name}.
    """
    return ops.add_flowed_text(
        _doc(document_id),
        x=x,
        y=y,
        width=width,
        height=height,
        paragraphs=paragraphs,
        parent=parent,
        name=name,
        style=_style(style),
    ).as_dict()


@mcp.tool
@emits_change
def wrap_in_link(
    *,
    document_id: str | None = None,
    href: str,
    children: list[str],
    parent: str | None = None,
    name: str | None = None,
) -> dict[str, str | None]:
    """Wrap existing nodes in an <a> hyperlink.

    Args:
        href: Link target URL.
        children: Ids/names of existing nodes to move inside the link.
        parent: Group/layer id (or name); omit for the document root.
        name: Friendly label.

    Returns:
        The new anchor node's {id, tag, name}.
    """
    return ops.wrap_in_link(
        _doc(document_id), href=href, children=children, parent=parent, name=name
    ).as_dict()


@mcp.tool
@emits_change
def define_mesh_gradient(
    *,
    document_id: str | None = None,
    x: float,
    y: float,
    rows: int = 1,
    cols: int = 1,
    name: str | None = None,
) -> str:
    """Define a skeleton mesh gradient (advanced; limited renderer support).

    Args:
        x: Mesh origin x in user units.
        y: Mesh origin y in user units.
        rows: Number of patch rows.
        cols: Number of patch columns.
        name: Friendly name.

    Returns:
        The mesh gradient's id.
    """
    return ops.define_mesh_gradient(_doc(document_id), x=x, y=y, rows=rows, cols=cols, name=name)


@mcp.tool
@emits_change
def apply_displacement_map(
    *,
    document_id: str | None = None,
    target: str,
    scale: float = 10,
    base_frequency: float = 0.05,
    num_octaves: int = 2,
) -> dict[str, str | None]:
    """Warp a node with turbulence-driven displacement (organic, watery/rippled distortion).

    Args:
        target: Node id or name.
        scale: Displacement strength in user units.
        base_frequency: Noise frequency driving the warp.
        num_octaves: Noise detail levels.

    Returns:
        The node's {id, tag, name}.
    """
    return ops.apply_displacement_map(
        _doc(document_id),
        target,
        scale=scale,
        base_frequency=base_frequency,
        num_octaves=num_octaves,
    ).as_dict()


@mcp.tool
@emits_change
def add_guide(
    *,
    document_id: str | None = None,
    position: Point,
    angle: float = 90.0,
    name: str | None = None,
) -> dict[str, str | None]:
    """Add an alignment guide line (editor aid; not rendered into the artwork).

    Args:
        position: A point [x, y] the guide passes through, in user units.
        angle: Guide angle in degrees (90 = vertical, 0 = horizontal).
        name: Friendly label.

    Returns:
        {id, name} of the guide.
    """
    return ops.add_guide(_doc(document_id), position=position, angle=angle, name=name)


@mcp.tool
def list_guides(*, document_id: str | None = None) -> list[dict[str, str | None]]:
    """List the document's guides.

    Returns:
        A list of {id, name}.
    """
    return ops.list_guides(_doc(document_id))


@mcp.tool
@emits_change
def add_page(
    *,
    document_id: str | None = None,
    x: float,
    y: float,
    width: float,
    height: float,
    label: str | None = None,
) -> dict[str, str | None]:
    """Add a page to a multi-page document (Inkscape pages).

    Args:
        x: Page left in user units.
        y: Page top in user units.
        width: Page width.
        height: Page height.
        label: Optional page name.

    Returns:
        {id, label} of the page.
    """
    return ops.add_page(_doc(document_id), x=x, y=y, width=width, height=height, label=label)


@mcp.tool
def list_pages(*, document_id: str | None = None) -> list[dict[str, str | None]]:
    """List the document's pages.

    Returns:
        A list of {id, label}.
    """
    return ops.list_pages(_doc(document_id))


# --- render ----------------------------------------------------------------


@mcp.tool
def render_document(
    *,
    document_id: str | None = None,
    scale: float = 1.0,
    background: str | None = None,
    backend: str | None = None,
) -> list[str | MCPImage]:
    """Render a document to a raster image so you can SEE the current result and iterate.

    This is the core feedback step — call it after changes to visually verify the artwork.

    Args:
        scale: Zoom factor on the document's natural pixel size (e.g. 2.0 for a sharper preview).
        background: Optional CSS background color; omit for a transparent canvas.
        backend: Render backend name; omit to use the default (resvg).

    Returns:
        A short text summary plus the rendered PNG image (base64) shown inline.
    """
    svg = _export_svg(_doc(document_id))
    renderer = get_renderer(backend)
    result = renderer.render(RenderRequest(svg=svg, scale=scale, background=background))
    feedback = build_feedback(result)
    return [feedback.summary, feedback.image]


@mcp.tool
def render_svg(
    svg: str,
    scale: float = 1.0,
    width: int | None = None,
    height: int | None = None,
    background: str | None = None,
    backend: str | None = None,
) -> list[str | MCPImage]:
    """Rasterize a raw SVG string directly, without creating a document.

    Useful for one-off previews of externally produced SVG.

    Args:
        svg: A complete SVG document as a string.
        scale: Zoom factor on the SVG's natural size (ignored if width/height given).
        width: Explicit output width in px (overrides scale).
        height: Explicit output height in px (overrides scale).
        background: Optional CSS background color; omit for transparent.
        backend: Render backend name; omit for the default (resvg).

    Returns:
        A short text summary plus the rendered PNG image (base64).
    """
    renderer = get_renderer(backend)
    result = renderer.render(
        RenderRequest(svg=svg, scale=scale, width=width, height=height, background=background)
    )
    feedback = build_feedback(result)
    return [feedback.summary, feedback.image]


@mcp.tool
def render_backends() -> dict[str, bool]:
    """Report which render backends are installed and usable in this environment.

    Returns:
        A map of backend name -> availability, e.g. {"resvg": true, "cairo": false}.
    """
    return available_backends()


@mcp.tool
def export_render(
    *,
    document_id: str | None = None,
    format: str = "png",
    scale: float = 1.0,
    path: str | None = None,
    background: str | None = None,
) -> dict[str, str | int]:
    """Export a document to a file on disk in a chosen format (raster or true vector).

    Engines: raster (png/jpeg/webp) is rendered faithfully via resvg; vector (pdf/ps/eps) via
    librsvg's ``rsvg-convert``; ``svg`` writes the serialized source. cairo is intentionally not
    used — it silently drops SVG filters (e.g. drop shadows render blank).

    Args:
        format: One of png, jpeg, webp, pdf, ps, eps, svg.
        scale: Zoom factor on the document's natural size (raster) or page (vector).
        path: Output file path; defaults to ``render.<format>`` in the working directory.
        background: Optional CSS background color; omit for transparent (raster) / white (vector).

    Returns:
        ``{path, format, bytes}`` — the absolute path written and the file size.
    """
    svg = _export_svg(_doc(document_id))
    data = export_bytes(svg, format, scale=scale, background=background)
    out = Path(path) if path else Path(f"render.{format.lower()}")
    out.write_bytes(data)
    return {"path": str(out.resolve()), "format": format.lower(), "bytes": len(data)}


@mcp.tool
def export_formats() -> dict[str, list[str] | bool]:
    """List the file formats ``export_render`` can write, and whether vector export is available.

    Returns:
        ``{formats, vector_available}`` — vector (pdf/ps/eps) needs the librsvg ``rsvg-convert``
        binary (macOS: ``brew install librsvg``).
    """
    return {"formats": list(SUPPORTED_FORMATS), "vector_available": rsvg_available()}


@mcp.tool
def measure_text(content: str, style: ShapeStyle | None = None) -> dict[str, float]:
    """Measure a single text run without rendering — its advance width and line height.

    Uses the system font's own metrics (via fontTools) so you can fit, center, or wrap text and
    size boxes around it before drawing. Latin-accurate (per-glyph advances; no kerning/shaping).

    Args:
        content: The text to measure.
        style: Font properties (font_family, font_size, font_weight, font_style); defaults to
            sans-serif at 16px when omitted.

    Returns:
        ``{width, height}`` in user units.
    """
    style = style or ShapeStyle()
    family = (style.font_family or "sans-serif").split(",")[0].strip().strip("'\"")
    size = _parse_font_size(style.font_size)
    bold = _is_bold(style.font_weight)
    italic = (style.font_style or "").strip().lower() in ("italic", "oblique")
    width, height = _measure_text(
        content, font_family=family, font_size=size, bold=bold, italic=italic
    )
    return {"width": width, "height": height}


# --- resources (readable ambient context) ----------------------------------


@mcp.resource("svg://documents", mime_type="application/json")
def documents_resource() -> dict[str, str | list[dict[str, str | int | None]] | None]:
    """Index of open documents: id, size, counts, and which one is active."""
    active = _store().active_id
    docs: list[dict[str, str | int | None]] = []
    for did in _store().list_ids():
        info = _describe_document(_store().peek(did))
        docs.append({"id": did, "active": did == active, **info})
    return {"active": active, "documents": docs}


@mcp.resource("svg://{document_id}/svg", mime_type="image/svg+xml")
def document_svg_resource(document_id: str) -> str:
    """The live SVG source of a document."""
    return _export_svg(_store().peek(document_id))


@mcp.resource("svg://{document_id}/render", mime_type="image/png")
def document_render_resource(document_id: str) -> bytes:
    """A rendered PNG preview of a document."""
    svg = _export_svg(_store().peek(document_id))
    return get_renderer().render(RenderRequest(svg=svg)).png


def main() -> None:
    """Console-script entrypoint (`svg-mcp`).

    Transport and bind address come from CLI flags, falling back to env vars, then defaults:

        --transport / SVG_MCP_TRANSPORT   stdio (default) | http | streamable-http | sse
        --host      / SVG_MCP_HOST        bind host for http transports (default 127.0.0.1)
        --port      / SVG_MCP_PORT        bind port for http transports (default 8000)

    The http / streamable-http transports serve streamable HTTP at ``/mcp``.
    """
    import argparse
    import os

    parser = argparse.ArgumentParser(prog="svg-mcp", description="svg-mcp MCP server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "http", "streamable-http", "sse"],
        default=os.environ.get("SVG_MCP_TRANSPORT", "stdio"),
        help="MCP transport (default: stdio)",
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("SVG_MCP_HOST", "127.0.0.1"),
        help="Bind host for http transports (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("SVG_MCP_PORT", "8000")),
        help="Bind port for http transports (default: 8000)",
    )
    args = parser.parse_args()

    if args.transport == "stdio":
        mcp.run()
    else:
        mcp.run(transport=args.transport, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
