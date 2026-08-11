"""Theme engine: loading, the CSS pipeline, document integration, and a render check."""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from svg_mcp import ops
from svg_mcp.model import Document
from svg_mcp.model.errors import ThemeError
from svg_mcp.render import get_renderer
from svg_mcp.render.base import RenderRequest
from svg_mcp.serialize import export_svg
from svg_mcp.session import DocumentStore
from svg_mcp.theme import (
    default_search_paths,
    load_theme,
    materialize,
    namespace_rules,
    parse_css,
    parse_variant_css,
    resolve_vars,
    serialize_rules,
    tier_sort,
)

FIXTURES = Path(__file__).parent / "fixtures" / "themes"


def _doc() -> Document:
    return DocumentStore().create(200, 120)[1]


def _house() -> object:
    return load_theme("house", [FIXTURES])


def _rules(css: str) -> tuple[str, ...]:
    parsed = parse_css(css, origin="<test>")
    return tuple(rule.selector_text() for rule in parsed.rules)


# --- 1. loader happy path ---------------------------------------------------


def test_load_directory_theme_populates_every_field() -> None:
    theme = load_theme("house", [FIXTURES])
    assert theme.name == "house"
    assert theme.source == FIXTURES / "house"
    assert theme.manifest.serves.categories == ["shape", "text", "container"]
    assert theme.manifest.serves.roles == ["title", "label"]
    assert theme.manifest.kinds == {"service": "squircle", "datastore": "cylinder"}
    assert theme.manifest.variants == {"dark": "variants/dark.css"}
    assert ".shape {" in theme.styles_css
    assert set(theme.variants) == {"dark"}
    assert theme.guidance is not None and "House style" in theme.guidance
    assert theme.tokens["--accent"] == "#3366ff"
    assert theme.variant_tokens["dark"]["--ink"] == "#f5f7fa"
    assert theme.rules  # parsed once at load


def test_bare_css_theme_loads_with_defaults() -> None:
    theme = load_theme("mini", [FIXTURES])
    assert theme.name == "mini"
    assert theme.source == FIXTURES / "mini.css"
    assert theme.manifest.serves.categories == []
    assert theme.variants == {}
    assert theme.guidance is None
    assert materialize(theme).css == ".mini-badge { fill:#ff0088 }"


def test_declared_role_without_a_rule_is_a_diagnostic() -> None:
    theme = load_theme("house", [FIXTURES])
    assert any("role 'label'" in note for note in theme.diagnostics)


def test_unknown_theme_names_the_search_paths() -> None:
    with pytest.raises(ThemeError, match="no theme named 'ghost'"):
        load_theme("ghost", [FIXTURES])


def test_default_search_paths_put_the_project_first() -> None:
    paths = default_search_paths(Path("/proj"))
    assert paths[0] == Path("/proj/.svg-mcp/themes")
    assert paths[1] == Path.home() / ".config" / "svg-mcp" / "themes"


# --- 2. search-path precedence ---------------------------------------------


def test_first_search_path_wins_on_name_clash(tmp_path: Path) -> None:
    first, second = tmp_path / "a", tmp_path / "b"
    for base, color in ((first, "#111111"), (second, "#222222")):
        (base / "clash").mkdir(parents=True)
        (base / "clash" / "styles.css").write_text(f".box {{ fill: {color} }}", encoding="utf-8")
    theme = load_theme("clash", [first, second])
    assert theme.source == first / "clash"
    assert "#111111" in theme.styles_css


# --- 3. lint rejections -----------------------------------------------------


def test_at_rule_is_rejected() -> None:
    with pytest.raises(ThemeError, match="@media"):
        parse_css("@media screen { .a { fill: red } }", origin="<test>")


def test_attribute_selector_is_rejected() -> None:
    with pytest.raises(ThemeError, match="attribute selector") as excinfo:
        parse_css("rect[width] { fill: red }", origin="<test>")
    assert "rect[width]" in str(excinfo.value)


def test_pseudo_class_is_rejected() -> None:
    with pytest.raises(ThemeError, match="pseudo-class") as excinfo:
        parse_css(".a:hover { fill: red }", origin="<test>")
    assert ".a:hover" in str(excinfo.value)


def test_id_selector_is_rejected() -> None:
    with pytest.raises(ThemeError, match="id selector") as excinfo:
        parse_css("#box { fill: red }", origin="<test>")
    assert "#box" in str(excinfo.value)


def test_universal_selector_is_rejected() -> None:
    with pytest.raises(ThemeError, match="universal selector") as excinfo:
        parse_css("* { fill: red }", origin="<test>")
    assert "*" in str(excinfo.value)


def test_sibling_combinator_is_rejected() -> None:
    with pytest.raises(ThemeError, match="sibling combinator"):
        parse_css("rect + text { fill: red }", origin="<test>")


def test_custom_property_outside_root_is_rejected() -> None:
    with pytest.raises(ThemeError, match="--brand") as excinfo:
        parse_css(".a { --brand: red }", origin="<test>")
    assert "outside ':root'" in str(excinfo.value)


def test_non_root_rule_in_a_variant_is_rejected() -> None:
    with pytest.raises(ThemeError, match="only contain ':root'") as excinfo:
        parse_variant_css(":root { --a: 1 }\n.b { fill: red }", origin="<variant>")
    assert ".b" in str(excinfo.value)


def test_selector_list_lints_each_half() -> None:
    assert _rules(".a, g.b { fill: red }") == (".a, g.b",)
    with pytest.raises(ThemeError, match="id selector"):
        parse_css(".a, #b { fill: red }", origin="<test>")


def test_root_is_only_allowed_alone() -> None:
    with pytest.raises(ThemeError, match="pseudo-class"):
        parse_css(":root, .a { --x: 1 }", origin="<test>")


def test_lint_failure_propagates_through_the_loader(tmp_path: Path) -> None:
    base = tmp_path / "themes"
    (base / "broken").mkdir(parents=True)
    (base / "broken" / "styles.css").write_text("@import url(x);", encoding="utf-8")
    with pytest.raises(ThemeError, match="@import"):
        load_theme("broken", [base])


def test_missing_styles_css_is_rejected(tmp_path: Path) -> None:
    base = tmp_path / "themes"
    (base / "empty").mkdir(parents=True)
    with pytest.raises(ThemeError, match="no styles.css"):
        load_theme("empty", [base])


# --- 4. var() resolution ----------------------------------------------------


def test_var_resolves_plain_and_fallback_and_output_is_var_free() -> None:
    parsed = parse_css(
        ":root { --accent: #ff0000 }\n"
        ".a { fill: var(--accent); stroke: var(--missing, #00ff00); "
        "stroke-width: calc(var(--accent) * 0) }",
        origin="<test>",
    )
    css = serialize_rules(resolve_vars(parsed.rules, parsed.tokens, origin="<test>"))
    assert "var(" not in css
    assert "fill:#ff0000" in css
    assert "stroke:#00ff00" in css
    assert "calc(#ff0000 * 0)" in css


def test_missing_token_without_fallback_names_token_and_rule() -> None:
    parsed = parse_css(".card { fill: var(--nope) }", origin="<test>")
    with pytest.raises(ThemeError) as excinfo:
        resolve_vars(parsed.rules, parsed.tokens, origin="<test>")
    message = str(excinfo.value)
    assert "--nope" in message and ".card" in message and "fill" in message


def test_nested_token_reference_resolves() -> None:
    parsed = parse_css(
        ":root { --base: #123456; --accent: var(--base) }\n.a { fill: var(--accent) }",
        origin="<test>",
    )
    css = serialize_rules(resolve_vars(parsed.rules, parsed.tokens, origin="<test>"))
    assert "fill:#123456" in css


def test_self_referential_token_is_rejected() -> None:
    parsed = parse_css(":root { --a: var(--a) }\n.x { fill: var(--a) }", origin="<test>")
    with pytest.raises(ThemeError, match="cycle"):
        resolve_vars(parsed.rules, parsed.tokens, origin="<test>")


def test_materialized_house_css_has_no_var() -> None:
    assert "var(" not in materialize(load_theme("house", [FIXTURES])).css


# --- 5. namespacing ---------------------------------------------------------


def _namespaced(css: str, name: str = "house") -> tuple[str, ...]:
    parsed = parse_css(css, origin="<test>")
    return tuple(rule.selector_text() for rule in namespace_rules(parsed.rules, name))


def test_class_selectors_are_prefixed() -> None:
    assert _namespaced(".card { fill: red }") == (".house-card",)


def test_descendant_and_child_parts_are_prefixed_too() -> None:
    assert _namespaced(".diagram-box text { fill: red }") == (".house-diagram-box text",)
    assert _namespaced(".box > .label { fill: red }") == (".house-box > .house-label",)


def test_type_selectors_are_untouched() -> None:
    assert _namespaced("text { fill: red }") == ("text",)
    assert _namespaced("g.box rect { fill: red }") == ("g.house-box rect",)


def test_already_prefixed_classes_are_not_double_prefixed() -> None:
    assert _namespaced(".house-card { fill: red }") == (".house-card",)


# --- 6. tier sort -----------------------------------------------------------


def test_tier_sort_orders_category_then_type_then_role_then_parts() -> None:
    theme = load_theme("house", [FIXTURES])
    selectors = [line.split(" {", 1)[0] for line in materialize(theme).css.splitlines()]
    assert selectors[:3] == [".house-shape", ".house-shape--pill", ".house-title"]
    assert selectors[3:] == [
        ".house-diagram-box rect",
        ".house-diagram-box text",
        ".house-container > .house-label",
        ".house-card",
        ".house-card-strong",
        "text",
    ]


def test_tier_sort_is_stable_within_a_tier() -> None:
    css = ".b { fill: red }\n.a { fill: red }\n.shape { fill: red }"
    parsed = parse_css(css, origin="<test>")
    ordered = tier_sort(namespace_rules(parsed.rules, "t"), "t", roles=())
    assert [rule.selector_text() for rule in ordered] == [".t-shape", ".t-b", ".t-a"]


# --- 7. variants ------------------------------------------------------------


def test_variant_overlays_tokens() -> None:
    theme = load_theme("house", [FIXTURES])
    base = materialize(theme)
    dark = materialize(theme, "dark")
    assert base.variant is None and dark.variant == "dark"
    assert base.tokens["--ink"] == "#101820"
    assert dark.tokens["--ink"] == "#f5f7fa"
    assert "fill:#ffcc00" in base.css and "fill:#204020" in dark.css
    assert dark.tokens["--accent"] == "#3366ff"  # untouched base token survives


def test_unknown_variant_is_rejected() -> None:
    with pytest.raises(ThemeError, match="no variant 'neon'"):
        materialize(load_theme("house", [FIXTURES]), "neon")


# --- 8. descriptions --------------------------------------------------------


def test_doc_comments_become_descriptions() -> None:
    result = materialize(load_theme("house", [FIXTURES]))
    assert result.descriptions["house-shape"] == "Every shape the theme paints."
    assert result.descriptions["house-title"] == "Page and section titles."
    assert result.descriptions["house-diagram-box"] == "A boxed diagram node."
    assert "house-card" not in result.descriptions  # undocumented rule


# --- 9. document integration ------------------------------------------------


def _stylesheet_text(doc: Document) -> str:
    svg = export_svg(doc)
    start = svg.index("<style", 0)
    return svg[start : svg.index("</style>", start)]


def test_materialize_into_puts_theme_rules_before_named_styles() -> None:
    doc = _doc()
    ops.materialize_into(doc, load_theme("house", [FIXTURES]))
    ops.define_style(doc, "callout", {"fill": "#ff00ff"})
    text = _stylesheet_text(doc)
    assert ".house-shape" in text and ".callout" in text
    assert text.index(".house-shape") < text.index(".callout")
    assert doc.theme_meta["house"].variant is None
    assert doc.theme_meta["house"].tokens["--accent"] == "#3366ff"
    assert doc.theme_meta["house"].descriptions["house-shape"]


def test_reapplying_a_theme_replaces_its_block() -> None:
    doc = _doc()
    theme = load_theme("house", [FIXTURES])
    ops.materialize_into(doc, theme)
    ops.materialize_into(doc, theme, "dark")
    text = _stylesheet_text(doc)
    assert text.count(".house-shape {") == 1
    assert "#204020" in text and "#ffcc00" not in text
    assert doc.theme_meta["house"].variant == "dark"
    assert list(doc.theme_css) == ["house"]


# --- 10. render smoke test --------------------------------------------------


def test_themed_descendant_rule_renders(tmp_path: Path) -> None:
    renderer = get_renderer()
    if not renderer.available():
        pytest.skip("no resvg backend available")
    from PIL import Image

    doc = _doc()
    ops.materialize_into(doc, load_theme("house", [FIXTURES]))
    group = ops.create_group(doc)
    ops.apply_styles(doc, group.id, ["house-diagram-box"])
    ops.add_rect(doc, x=0, y=0, width=200, height=120, parent=group.id)
    ops.add_text(doc, x=10, y=60, content="hi", parent=group.id)

    result = renderer.render(RenderRequest(svg=export_svg(doc)))
    image = Image.open(io.BytesIO(result.png)).convert("RGBA")
    pixel = image.getpixel((100, 100))
    assert isinstance(pixel, tuple)
    assert pixel[:3] == (255, 204, 0)  # --box-fill via `.diagram-box rect`
