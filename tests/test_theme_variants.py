"""Theme variants, scoped apply/clear, and the bundled default theme."""

from __future__ import annotations

import io
from pathlib import Path
from typing import Literal

import pytest

from svg_mcp import ops
from svg_mcp.model.document import Document
from svg_mcp.model.errors import InvalidArgument
from svg_mcp.render import get_renderer
from svg_mcp.render.base import RenderRequest
from svg_mcp.serialize import export_svg
from svg_mcp.session import DocumentStore
from svg_mcp.theme.loader import builtin_themes_path

FIXTURES = Path(__file__).parent / "fixtures" / "themes"


def _doc() -> Document:
    return DocumentStore().create(200, 120)[1]


def _classes(doc: Document, target: str) -> list[str]:
    return str(doc.resolve(target).get("class") or "").split()


def _load(doc: Document, name: str, *, variant: str | None = None) -> ops.ThemeResidency:
    return ops.load_theme(doc, name, variant=variant, search_paths=[FIXTURES])


def _stylesheet(doc: Document) -> str:
    svg = export_svg(doc)
    start = svg.index("<style")
    return svg[start : svg.index("</style>", start)]


# --- 1. variants -------------------------------------------------------------


def test_set_variant_switches_a_resident_theme() -> None:
    doc = _doc()
    _load(doc, "house")
    ops.add_rect(doc, x=0, y=0, width=10, height=10)
    assert "#101820" in _stylesheet(doc)  # base --ink

    result = ops.set_theme_variant(doc, "dark")
    assert result.variant == "dark"
    assert result.themes == [ops.VariantOutcome(theme="house", variant_used="dark")]
    assert "#f5f7fa" in _stylesheet(doc) and "#101820" not in _stylesheet(doc)
    assert doc.theme_meta["house"].variant == "dark"


def test_a_theme_without_that_variant_falls_back_to_base_silently() -> None:
    doc = _doc()
    _load(doc, "house")
    _load(doc, "alt")  # no variants at all
    before = _stylesheet(doc)

    result = ops.set_theme_variant(doc, "dark")
    assert result.themes == [
        ops.VariantOutcome(theme="house", variant_used="dark"),
        ops.VariantOutcome(theme="alt", variant_used=None),
    ]
    assert doc.theme_meta["alt"].variant is None
    assert ".alt-shape { fill:#0a7d33 }" in _stylesheet(doc)
    assert before != _stylesheet(doc)  # house did move


def test_variant_none_returns_every_resident_to_base() -> None:
    doc = _doc()
    _load(doc, "house", variant="dark")
    result = ops.set_theme_variant(doc, None)
    assert result.variant is None
    assert result.themes == [ops.VariantOutcome(theme="house", variant_used=None)]
    assert "#101820" in _stylesheet(doc)


def test_a_variant_switch_keeps_the_routing_table() -> None:
    doc = _doc()
    _load(doc, "house")
    routes = dict(doc.theme_routing)
    ops.set_theme_variant(doc, "dark")
    assert doc.theme_routing == routes
    assert doc.theme_meta["house"].routes == ["shape", "text", "container", "title", "label"]


def test_pinned_nodes_counts_the_inline_styles_a_variant_cannot_move() -> None:
    doc = _doc()
    _load(doc, "house")
    ops.add_rect(doc, x=0, y=0, width=10, height=10)  # follows the theme
    ops.add_rect(doc, x=0, y=0, width=10, height=10, style={"fill": "#ff0000"})
    ops.add_text(doc, x=1, y=1, content="hi", style={"fill": "#00ff00"})
    assert ops.set_theme_variant(doc, "dark").pinned_nodes == 2


def test_load_records_the_variant_it_materialized() -> None:
    doc = _doc()
    assert _load(doc, "house", variant="dark").variant == "dark"
    assert doc.theme_meta["house"].variant == "dark"
    assert "#204020" in _stylesheet(doc)


# --- 2. scoped apply ---------------------------------------------------------


def _apply(
    doc: Document, target: str, theme: str, *, mode: Literal["paste", "replace"] = "paste"
) -> ops.ThemeScopeChange:
    return ops.apply_theme(doc, target, theme, mode=mode, search_paths=[FIXTURES])


def test_paste_translates_a_matching_suffix_and_keeps_the_original() -> None:
    doc = _doc()
    _load(doc, "house")
    rect = ops.add_rect(doc, x=0, y=0, width=10, height=10)
    assert _classes(doc, rect.id) == ["house-shape"]

    change = _apply(doc, rect.id, "alt")
    assert _classes(doc, rect.id) == ["house-shape", "alt-shape"]
    assert (change.nodes_touched, change.classes_added, change.classes_removed) == (1, 1, 0)
    assert ".alt-shape" in _stylesheet(doc)


def test_paste_derives_hooks_for_a_bare_node_from_its_recorded_category() -> None:
    doc = _doc()
    rect = ops.add_rect(doc, x=0, y=0, width=10, height=10)
    line = ops.add_line(doc, x1=0, y1=0, x2=10, y2=10)
    group = ops.create_group(doc, name="frame")
    assert _classes(doc, rect.id) == []

    _apply(doc, rect.id, "alt")
    _apply(doc, line.id, "alt")
    _apply(doc, group.id, "alt")
    assert _classes(doc, rect.id) == ["alt-shape"]
    assert _classes(doc, line.id) == ["alt-connector"]
    assert _classes(doc, group.id) == []  # a group has no category, so nothing is derived


def test_derivation_survives_a_rename() -> None:
    doc = _doc()
    rect = ops.add_rect(doc, x=0, y=0, width=10, height=10, name="inlet")
    ops.set_name(doc, rect.id, "outlet")
    assert doc.resolve("outlet").get("data-category") == "shape"

    _apply(doc, "outlet", "alt")
    assert _classes(doc, "outlet") == ["alt-shape"]


def test_imported_nodes_are_translated_but_never_guessed_at() -> None:
    doc = ops.load_svg_document(
        svg=(
            '<svg xmlns="http://www.w3.org/2000/svg" width="120" height="80">'
            '<rect id="bare" x="0" y="0" width="10" height="10"/>'
            '<rect id="dressed" class="house-shape" x="0" y="0" width="10" height="10"/>'
            "</svg>"
        )
    )
    ops.load_theme(doc, "house", search_paths=[FIXTURES])

    change = _apply(doc, "bare", "alt")
    assert (_classes(doc, "bare"), change.nodes_touched) == ([], 0)  # nothing known about it

    _apply(doc, "dressed", "alt")
    assert _classes(doc, "dressed") == ["house-shape", "alt-shape"]  # its class says what it is


def test_paste_covers_the_whole_subtree() -> None:
    doc = _doc()
    group = ops.create_group(doc, name="frame")
    rect = ops.add_rect(doc, x=0, y=0, width=10, height=10, parent=group.id)
    line = ops.add_line(doc, x1=0, y1=0, x2=10, y2=10, parent=group.id)

    change = _apply(doc, group.id, "alt")
    assert _classes(doc, rect.id) == ["alt-shape"]
    assert _classes(doc, line.id) == ["alt-connector"]
    assert (change.nodes_touched, change.classes_added) == (2, 2)


def test_a_scoped_apply_materializes_without_routing_anything() -> None:
    doc = _doc()
    _load(doc, "house")
    rect = ops.add_rect(doc, x=0, y=0, width=10, height=10)
    routing = dict(doc.theme_routing)

    _apply(doc, rect.id, "alt")
    assert doc.theme_routing == routing  # `alt` took nothing
    assert doc.theme_meta["alt"].routes == []
    later = ops.add_rect(doc, x=0, y=0, width=10, height=10)
    assert _classes(doc, later.id) == ["house-shape"]  # new nodes still hook into house


def test_replace_strips_other_themes_but_keeps_doc_local_styles() -> None:
    doc = _doc()
    _load(doc, "house")
    ops.define_style(doc, "callout", {"fill": "#ff00ff"})
    rect = ops.add_rect(doc, x=0, y=0, width=10, height=10, styles=["callout"])
    assert _classes(doc, rect.id) == ["house-shape", "callout"]

    change = _apply(doc, rect.id, "alt", mode="replace")
    assert _classes(doc, rect.id) == ["callout", "alt-shape"]
    assert (change.nodes_touched, change.classes_added, change.classes_removed) == (1, 1, 1)


def test_replace_carries_a_role_across_and_falls_back_to_the_category() -> None:
    doc = _doc()
    _load(doc, "house")
    titled = ops.add_text(doc, x=0, y=0, content="hi", role="title")
    boxed = ops.add_pill(doc, x=0, y=0, width=40, height=20)
    assert _classes(doc, boxed.id) == ["house-shape", "house-shape--pill"]

    _apply(doc, titled.id, "alt", mode="replace")
    _apply(doc, boxed.id, "alt", mode="replace")
    # `alt` serves neither the title role nor the text category, so the title is left bare.
    assert _classes(doc, titled.id) == []
    assert _classes(doc, boxed.id) == ["alt-shape"]  # `shape` translates, `shape--pill` does not


def test_applying_a_theme_twice_changes_nothing_the_second_time() -> None:
    doc = _doc()
    rect = ops.add_rect(doc, x=0, y=0, width=10, height=10)
    assert _apply(doc, rect.id, "alt").nodes_touched == 1
    again = _apply(doc, rect.id, "alt")
    assert (again.nodes_touched, again.classes_added, again.classes_removed) == (0, 0, 0)


def test_a_scoped_apply_can_name_a_variant() -> None:
    doc = _doc()
    rect = ops.add_rect(doc, x=0, y=0, width=10, height=10)
    change = ops.apply_theme(doc, rect.id, "house", variant="dark", search_paths=[FIXTURES])
    assert change.variant == "dark"
    assert _classes(doc, rect.id) == ["house-shape"]
    assert "#f5f7fa" in _stylesheet(doc)


# --- 3. scoped clear ---------------------------------------------------------


def _dressed(doc: Document) -> str:
    """A rect wearing two themes' classes plus a doc-local one."""
    _load(doc, "house")
    ops.define_style(doc, "callout", {"fill": "#ff00ff"})
    rect = ops.add_rect(doc, x=0, y=0, width=10, height=10, styles=["callout"])
    _apply(doc, rect.id, "alt")
    assert _classes(doc, rect.id) == ["house-shape", "callout", "alt-shape"]
    return rect.id


def test_clear_removes_only_the_named_themes_classes() -> None:
    doc = _doc()
    target = _dressed(doc)
    change = ops.clear_theme(doc, target, "house")
    assert _classes(doc, target) == ["callout", "alt-shape"]
    assert (change.theme, change.nodes_touched, change.classes_removed) == ("house", 1, 1)


def test_clear_without_a_theme_removes_every_theme_class() -> None:
    doc = _doc()
    target = _dressed(doc)
    change = ops.clear_theme(doc, target)
    assert _classes(doc, target) == ["callout"]
    assert (change.theme, change.classes_removed) == (None, 2)
    assert ".house-shape" in _stylesheet(doc)  # the rules stay; only the links went


def test_clearing_the_last_class_drops_the_attribute() -> None:
    doc = _doc()
    _load(doc, "house")
    rect = ops.add_rect(doc, x=0, y=0, width=10, height=10)
    ops.clear_theme(doc, rect.id)
    assert doc.resolve(rect.id).get("class") is None


def test_clear_counts_the_whole_subtree_and_ignores_untouched_nodes() -> None:
    doc = _doc()
    _load(doc, "house")
    group = ops.create_group(doc, name="frame")
    ops.add_rect(doc, x=0, y=0, width=10, height=10, parent=group.id)
    ops.add_rect(doc, x=0, y=0, width=10, height=10, parent=group.id)
    ops.add_rect(doc, x=0, y=0, width=10, height=10, parent=group.id, themed=False)

    change = ops.clear_theme(doc, group.id, "house")
    assert (change.nodes_touched, change.classes_removed) == (2, 2)


def test_clearing_a_theme_that_is_not_resident_is_rejected() -> None:
    doc = _doc()
    rect = ops.add_rect(doc, x=0, y=0, width=10, height=10)
    with pytest.raises(InvalidArgument, match="not loaded"):
        ops.clear_theme(doc, rect.id, "house")


# --- 4. the bundled default theme --------------------------------------------


def test_the_default_theme_ships_with_the_package() -> None:
    assert (builtin_themes_path() / "default" / "styles.css").is_file()


def test_default_loads_by_name_with_no_search_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(Path(__file__).parent)  # nothing project-local to shadow it
    doc = _doc()
    result = ops.load_theme(doc, "default")
    assert result.theme == "default"
    assert result.guidance is not None and "Prefer roles over raw styles" in result.guidance
    assert {"default-service", "default-code"} <= {style.name for style in result.styles}


def test_a_role_with_no_theme_loaded_falls_back_to_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(Path(__file__).parent)
    doc = _doc()
    rect = ops.add_rect(doc, x=0, y=0, width=10, height=10, role="service")
    assert _classes(doc, rect.id) == ["default-service"]
    assert ".default-service" in _stylesheet(doc)
    assert doc.theme_routing == {}  # materialized, deliberately routing nothing
    assert doc.theme_meta["default"].routes == []


def test_a_role_free_document_never_pulls_the_default_theme_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(Path(__file__).parent)
    doc = _doc()
    ops.add_rect(doc, x=0, y=0, width=10, height=10)
    ops.add_text(doc, x=1, y=1, content="hi")
    ops.add_line(doc, x1=0, y1=0, x2=10, y2=10)
    svg = export_svg(doc)
    assert doc.theme_meta == {} and doc.theme_css == {}
    assert "default" not in svg and "<style" not in svg
    assert 'class="' not in svg


def test_a_role_the_fallback_cannot_serve_names_what_it_offers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(Path(__file__).parent)
    doc = _doc()
    with pytest.raises(InvalidArgument, match="role 'ghost'") as excinfo:
        ops.add_rect(doc, x=0, y=0, width=10, height=10, role="ghost")
    assert "'service'" in str(excinfo.value)
    assert doc.theme_meta == {}  # a role it cannot serve installs nothing


def test_a_routed_category_still_refuses_a_role_it_does_not_define() -> None:
    doc = _doc()
    _load(doc, "house")  # house holds `shape`, so this is a routing decision, not a gap
    with pytest.raises(InvalidArgument, match="role 'service'"):
        ops.add_rect(doc, x=0, y=0, width=10, height=10, role="service")
    assert "default" not in doc.theme_meta


def test_the_code_role_resolves_to_the_monospace_family(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(Path(__file__).parent)
    doc = _doc()
    ops.add_text(doc, x=0, y=0, content="svg_mcp", role="code")
    assert "font-family:Meslo LG S DZ, monospace" in _stylesheet(doc)


def test_the_dark_variant_of_default_overrides_its_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(Path(__file__).parent)
    doc = _doc()
    ops.add_rect(doc, x=0, y=0, width=10, height=10, role="service")
    assert "#eceef1" in _stylesheet(doc)

    result = ops.set_theme_variant(doc, "dark")
    assert result.themes == [ops.VariantOutcome(theme="default", variant_used="dark")]
    assert ".default-service { fill:#262b31" in _stylesheet(doc)


def test_a_role_in_a_themeless_document_renders_in_the_default_fill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    renderer = get_renderer()
    if not renderer.available():
        pytest.skip("no resvg backend available")
    from PIL import Image

    monkeypatch.chdir(Path(__file__).parent)
    doc = _doc()
    ops.add_rect(doc, x=0, y=0, width=200, height=120, role="service")

    result = renderer.render(RenderRequest(svg=export_svg(doc)))
    image = Image.open(io.BytesIO(result.png)).convert("RGBA")
    pixel = image.getpixel((100, 60))
    assert isinstance(pixel, tuple)
    assert pixel[:3] == (236, 238, 241)  # --surface-raised, via the default theme's role hook
