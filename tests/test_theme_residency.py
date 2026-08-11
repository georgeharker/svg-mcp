"""Theme residency: the routing table, the residency verbs, and category auto-apply."""

from __future__ import annotations

import asyncio
import io
import shutil
from collections.abc import Awaitable
from pathlib import Path

import pytest

from svg_mcp import ops, server
from svg_mcp.model.document import Document
from svg_mcp.model.errors import InvalidArgument, ThemeError
from svg_mcp.query import describe_document
from svg_mcp.render import get_renderer
from svg_mcp.render.base import RenderRequest
from svg_mcp.serialize import export_svg
from svg_mcp.session import DocumentStore

FIXTURES = Path(__file__).parent / "fixtures" / "themes"


def _doc() -> Document:
    return DocumentStore().create(200, 120)[1]


def _classes(doc: Document, target: str) -> list[str]:
    return str(doc.resolve(target).get("class") or "").split()


def _load(
    doc: Document,
    name: str,
    *,
    roles: list[str] | None = None,
    variant: str | None = None,
    expect_free: bool = False,
) -> ops.ThemeResidency:
    return ops.load_theme(
        doc, name, roles=roles, variant=variant, expect_free=expect_free, search_paths=[FIXTURES]
    )


def _stylesheet(doc: Document) -> str:
    svg = export_svg(doc)
    start = svg.index("<style")
    return svg[start : svg.index("</style>", start)]


# --- 1. routing -------------------------------------------------------------


def test_load_takes_the_routes_the_manifest_claims() -> None:
    doc = _doc()
    result = _load(doc, "house")
    assert result.routes_taken == ["shape", "text", "container", "title", "label"]
    assert result.evicted == {}
    assert doc.theme_routing["shape"] == "house"
    assert doc.theme_meta["house"].routes == result.routes_taken


def test_explicit_roles_take_only_that_subset() -> None:
    doc = _doc()
    result = _load(doc, "house", roles=["text", "title"])
    assert result.routes_taken == ["text", "title"]
    assert doc.theme_routing == {"text": "house", "title": "house"}


def test_load_reports_the_theme_guidance_and_its_styles() -> None:
    result = _load(_doc(), "house")
    assert result.guidance is not None and "House style" in result.guidance
    described = {style.name: style.description for style in result.styles}
    assert described["house-shape"] == "Every shape the theme paints."
    assert described["house-card"] is None  # defined, undocumented
    assert "house-container" in described  # defined only as an ancestor part


def test_a_second_theme_evicts_the_route_it_contends_for() -> None:
    doc = _doc()
    _load(doc, "house")
    result = _load(doc, "alt")
    assert result.evicted == {"shape": "house"}
    assert doc.theme_routing["shape"] == "alt"
    assert doc.theme_routing["text"] == "house"
    assert "shape" not in doc.theme_meta["house"].routes
    assert set(doc.theme_css) == {"house", "alt"}


def test_expect_free_refuses_to_take_a_held_route() -> None:
    doc = _doc()
    _load(doc, "house")
    with pytest.raises(InvalidArgument, match="shape") as excinfo:
        _load(doc, "alt", expect_free=True)
    assert "house" in str(excinfo.value)
    assert doc.theme_routing["shape"] == "house"  # nothing taken
    assert "alt" not in doc.theme_css  # nothing installed


def test_a_role_the_theme_cannot_serve_is_rejected() -> None:
    doc = _doc()
    with pytest.raises(InvalidArgument, match="cannot serve role 'service'"):
        _load(doc, "house", roles=["service"])
    assert doc.theme_routing == {}


def test_bare_css_theme_routes_nothing_by_default() -> None:
    doc = _doc()
    result = _load(doc, "mini")
    assert result.routes_taken == []
    assert doc.theme_routing == {}
    assert ".mini-badge" in _stylesheet(doc)  # materialized, just not routed


def test_bare_css_theme_serves_a_role_it_defines_a_class_for() -> None:
    doc = _doc()
    assert _load(doc, "mini", roles=["badge"]).routes_taken == ["badge"]
    assert doc.theme_routing == {"badge": "mini"}


def test_reloading_with_narrower_roles_releases_the_rest() -> None:
    doc = _doc()
    _load(doc, "house")
    _load(doc, "house", roles=["text"])
    assert doc.theme_routing == {"text": "house"}
    assert doc.theme_meta["house"].routes == ["text"]


# --- 2. unload / sync / replace ---------------------------------------------


def test_unload_removes_the_rules_and_reports_dangling_classes() -> None:
    doc = _doc()
    _load(doc, "house")
    rect = ops.add_rect(doc, x=0, y=0, width=10, height=10)
    removal = ops.unload_theme(doc, "house")
    assert removal.routes_freed == ["container", "label", "shape", "text", "title"]
    assert removal.dangling_classes == ["house-shape"]
    assert ".house-shape" not in _stylesheet(doc)
    assert _classes(doc, rect.id) == ["house-shape"]  # the link is kept, the rule is gone
    assert doc.theme_routing == {} and doc.theme_meta == {}


def test_unloading_a_theme_that_is_not_resident_is_rejected() -> None:
    with pytest.raises(InvalidArgument, match="not loaded"):
        ops.unload_theme(_doc(), "house")


def _write_live_theme(base: Path, fill: str) -> Path:
    directory = base / "live"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "theme.toml").write_text('[serves]\ncategories = ["shape"]\n', encoding="utf-8")
    styles = directory / "styles.css"
    styles.write_text(f".shape {{ fill: {fill} }}", encoding="utf-8")
    return styles


def test_sync_picks_up_an_edited_stylesheet(tmp_path: Path) -> None:
    styles = _write_live_theme(tmp_path, "#010203")
    doc = _doc()
    ops.load_theme(doc, "live", search_paths=[tmp_path])
    ops.add_rect(doc, x=0, y=0, width=10, height=10)
    assert "#010203" in export_svg(doc)

    styles.write_text(".shape { fill: #040506 }", encoding="utf-8")
    result = ops.sync_theme(doc, "live")
    assert result.routes_taken == ["shape"]
    assert "#040506" in export_svg(doc) and "#010203" not in export_svg(doc)
    assert doc.theme_routing == {"shape": "live"}


def test_sync_keeps_the_variant_in_force() -> None:
    doc = _doc()
    _load(doc, "house", variant="dark")
    assert ops.sync_theme(doc, "house").variant == "dark"
    assert "#204020" in _stylesheet(doc)


def test_sync_errors_when_the_theme_left_the_disk(tmp_path: Path) -> None:
    _write_live_theme(tmp_path, "#010203")
    doc = _doc()
    ops.load_theme(doc, "live", search_paths=[tmp_path])
    shutil.rmtree(tmp_path / "live")
    with pytest.raises(ThemeError, match="no theme named 'live'"):
        ops.sync_theme(doc, "live")


def test_replace_swaps_the_whole_resident_set() -> None:
    doc = _doc()
    _load(doc, "house")
    result = ops.replace_theme(doc, "alt", search_paths=[FIXTURES])
    assert result.unloaded == ["house"]
    assert list(doc.theme_css) == ["alt"]
    assert doc.theme_routing == {"shape": "alt", "connector": "alt", "service": "alt"}
    assert ".house-shape" not in _stylesheet(doc)


# --- 3. reading residency ----------------------------------------------------


def test_list_styles_merges_theme_classes_and_doc_local_styles() -> None:
    doc = _doc()
    _load(doc, "house")
    ops.define_style(doc, "callout", {"fill": "#ff00ff"})
    listed = ops.list_styles(doc)
    by_name = {style.name: style for style in listed}
    assert by_name["house-shape"].theme == "house"
    assert by_name["house-shape"].description == "Every shape the theme paints."
    assert by_name["callout"].theme is None
    assert listed[-1].name == "callout"  # themes first, doc-local last


def test_describe_document_shows_residency_and_routing() -> None:
    doc = _doc()
    _load(doc, "house", variant="dark")
    _load(doc, "alt", roles=["connector"])
    info = describe_document(doc)
    house_routes = ["shape", "text", "container", "title", "label"]
    assert info["resident_themes"] == [
        {"name": "house", "variant": "dark", "routes": house_routes},
        {"name": "alt", "variant": None, "routes": ["connector"]},
    ]
    routing = info["theme_routing"]
    assert isinstance(routing, dict)
    assert routing["shape"] == "house" and routing["connector"] == "alt"


# --- 4. auto-apply -----------------------------------------------------------


def test_category_and_type_hooks_follow_the_primitive() -> None:
    doc = _doc()
    _load(doc, "house")
    rect = ops.add_rect(doc, x=0, y=0, width=10, height=10)
    pill = ops.add_pill(doc, x=0, y=0, width=40, height=20)
    assert _classes(doc, rect.id) == ["house-shape"]  # no .shape--rect rule to hook
    assert _classes(doc, pill.id) == ["house-shape", "house-shape--pill"]


def test_hooks_follow_the_routing_table_not_the_load_order() -> None:
    doc = _doc()
    _load(doc, "house")
    _load(doc, "alt")  # takes `shape`
    line = ops.add_line(doc, x1=0, y1=0, x2=10, y2=10)
    rect = ops.add_rect(doc, x=0, y=0, width=10, height=10)
    assert _classes(doc, rect.id) == ["alt-shape"]
    assert _classes(doc, line.id) == ["alt-connector"]


def test_role_hook_comes_from_the_theme_serving_that_role() -> None:
    doc = _doc()
    _load(doc, "house")
    text = ops.add_text(doc, x=0, y=0, content="hi", role="title")
    assert _classes(doc, text.id) == ["house-title"]  # house has no `.text` class hook


def test_an_unknown_role_is_rejected_rather_than_silently_ignored() -> None:
    doc = _doc()
    _load(doc, "house")
    with pytest.raises(InvalidArgument, match="role 'ghost'"):
        ops.add_rect(doc, x=0, y=0, width=10, height=10, role="ghost")


def test_named_styles_resolve_bare_or_at_prefixed_and_land_after_the_hooks() -> None:
    doc = _doc()
    _load(doc, "house")
    ops.define_style(doc, "callout", {"fill": "#ff00ff"})
    rect = ops.add_rect(doc, x=0, y=0, width=10, height=10, styles=["@card", "callout"])
    assert _classes(doc, rect.id) == ["house-shape", "house-card", "callout"]


def test_a_style_two_themes_define_is_ambiguous() -> None:
    doc = _doc()
    _load(doc, "house")
    _load(doc, "alt", roles=["service"])
    with pytest.raises(InvalidArgument, match="ambiguous") as excinfo:
        ops.add_rect(doc, x=0, y=0, width=10, height=10, styles=["card"])
    assert "house-card" in str(excinfo.value) and "alt-card" in str(excinfo.value)


def test_an_unknown_style_names_what_is_available() -> None:
    doc = _doc()
    _load(doc, "house")
    with pytest.raises(InvalidArgument, match="no style named 'ghost'") as excinfo:
        ops.add_rect(doc, x=0, y=0, width=10, height=10, styles=["ghost"])
    assert "house-shape" in str(excinfo.value)


def test_themed_false_leaves_the_node_unhooked() -> None:
    doc = _doc()
    _load(doc, "house")
    pill = ops.add_pill(doc, x=0, y=0, width=40, height=20, themed=False)
    assert _classes(doc, pill.id) == []


def test_groups_and_layers_are_not_themed() -> None:
    doc = _doc()
    _load(doc, "house")
    group = ops.create_group(doc, name="frame")
    layer = ops.create_layer(doc, name="base")
    assert _classes(doc, group.id) == [] and _classes(doc, layer.id) == []


def test_boolean_results_are_themed_as_shapes() -> None:
    doc = _doc()
    _load(doc, "house")
    first = ops.add_rect(doc, x=0, y=0, width=40, height=40, themed=False)
    second = ops.add_rect(doc, x=20, y=20, width=40, height=40, themed=False)
    result = ops.boolean(doc, op="union", targets=[first.id, second.id])
    assert _classes(doc, result.id) == ["house-shape"]


# --- 5. the tool surface -----------------------------------------------------


def _run[T](call: Awaitable[T]) -> T:
    """Drive one tool call — they are async so they can emit resource-change notifications."""

    async def driver() -> T:
        return await call

    return asyncio.run(driver())


def test_tool_response_reports_the_classes_attached() -> None:
    _run(server.create_document(width=100, height=100))
    _run(server.load_theme(name="house", search_paths=[str(FIXTURES)]))
    response = _run(server.add_pill(x=0, y=0, width=40, height=20))
    assert response["auto_styles"] == ["house-shape", "house-shape--pill"]
    plain = _run(server.add_rect(x=0, y=0, width=10, height=10, themed=False))
    assert plain["auto_styles"] == []


# --- 6. render check ---------------------------------------------------------


def test_an_unstyled_rect_renders_in_the_theme_fill() -> None:
    renderer = get_renderer()
    if not renderer.available():
        pytest.skip("no resvg backend available")
    from PIL import Image

    doc = _doc()
    _load(doc, "house")
    ops.add_rect(doc, x=0, y=0, width=200, height=120)

    result = renderer.render(RenderRequest(svg=export_svg(doc)))
    image = Image.open(io.BytesIO(result.png)).convert("RGBA")
    pixel = image.getpixel((100, 60))
    assert isinstance(pixel, tuple)
    assert pixel[:3] == (51, 102, 255)  # --accent, via the auto-applied `.house-shape` hook
