"""Node styling state: the ordered class list (style refs) vs. the inline/explicit style."""

from __future__ import annotations

import io

import pytest

from svg_mcp import ops
from svg_mcp.model.document import Document
from svg_mcp.query import describe_node
from svg_mcp.render import get_renderer
from svg_mcp.render.base import RenderRequest
from svg_mcp.serialize import export_svg
from svg_mcp.session import DocumentStore


def _doc() -> Document:
    return DocumentStore().create(100, 100)[1]


def _classes(doc: Document, target: str) -> list[str]:
    return str(doc.resolve(target).get("class") or "").split()


def test_apply_styles_appends_in_call_order() -> None:
    doc = _doc()
    rect = ops.add_rect(doc, x=0, y=0, width=10, height=10)
    ops.apply_styles(doc, rect.id, ["a", "b"])
    ops.apply_styles(doc, rect.id, ["c"])
    assert _classes(doc, rect.id) == ["a", "b", "c"]


def test_apply_styles_dedupes_and_keeps_position() -> None:
    doc = _doc()
    rect = ops.add_rect(doc, x=0, y=0, width=10, height=10)
    ops.apply_styles(doc, rect.id, ["a", "b", "c"])
    ops.apply_styles(doc, rect.id, ["a"])
    assert _classes(doc, rect.id) == ["a", "b", "c"]


def test_apply_styles_replace_overwrites() -> None:
    doc = _doc()
    rect = ops.add_rect(doc, x=0, y=0, width=10, height=10)
    ops.apply_styles(doc, rect.id, ["a", "b"])
    ops.apply_styles(doc, rect.id, ["c"], replace=True)
    assert _classes(doc, rect.id) == ["c"]


def test_remove_styles_keeps_order_and_ignores_absent() -> None:
    doc = _doc()
    rect = ops.add_rect(doc, x=0, y=0, width=10, height=10)
    ops.apply_styles(doc, rect.id, ["a", "b", "c"])
    ops.remove_styles(doc, rect.id, ["b", "nope"])
    assert _classes(doc, rect.id) == ["a", "c"]
    ops.remove_styles(doc, rect.id, ["b"])  # idempotent
    assert _classes(doc, rect.id) == ["a", "c"]


def test_remove_styles_drops_empty_class_attribute() -> None:
    doc = _doc()
    rect = ops.add_rect(doc, x=0, y=0, width=10, height=10)
    ops.apply_styles(doc, rect.id, ["a"])
    ops.remove_styles(doc, rect.id, ["a"])
    assert doc.resolve(rect.id).get("class") is None
    assert 'class=""' not in export_svg(doc)


def test_describe_node_surfaces_both_styling_facets() -> None:
    doc = _doc()
    ops.define_style(doc, "card", {"fill": "#00ff00"})
    rect = ops.add_rect(doc, x=0, y=0, width=10, height=10, style={"fill": "#ff0000"})
    ops.apply_styles(doc, rect.id, ["card", "chip"])
    info = describe_node(doc, rect.id)
    assert info["style_refs"] == ["card", "chip"]
    assert info["explicit_style"] == {"fill": "#ff0000"}


def test_describe_node_styling_facets_empty_by_default() -> None:
    doc = _doc()
    rect = ops.add_rect(doc, x=0, y=0, width=10, height=10)
    info = describe_node(doc, rect.id)
    assert info["style_refs"] == [] and info["explicit_style"] == {}


def test_explicit_inline_style_beats_linked_class_rule() -> None:
    """The contract: "if you typed it, it sticks" — inline wins the cascade over a class rule."""
    renderer = get_renderer()
    if not renderer.available():
        pytest.skip("no resvg backend available")
    from PIL import Image

    doc = _doc()
    ops.define_style(doc, "card", {"fill": "#00ff00"})
    rect = ops.add_rect(doc, x=0, y=0, width=100, height=100, style={"fill": "#ff0000"})
    ops.apply_styles(doc, rect.id, ["card"])

    svg = export_svg(doc)
    assert 'class="card"' in svg and "#ff0000" in svg

    result = renderer.render(RenderRequest(svg=svg))
    image = Image.open(io.BytesIO(result.png)).convert("RGBA")
    pixel = image.getpixel((50, 50))
    assert isinstance(pixel, tuple)
    assert pixel[:3] == (255, 0, 0)  # inline fill, not the class's green
