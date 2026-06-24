"""Model + ops + query vertical-slice tests (no MCP server needed)."""

from __future__ import annotations

import pytest

from svg_mcp import ops
from svg_mcp.model import Document
from svg_mcp.query import get_bbox, outline
from svg_mcp.query.outline import OutlineNode
from svg_mcp.schemas import ShapeStyle
from svg_mcp.serialize import export_svg
from svg_mcp.session import DocumentStore


def _doc() -> Document:
    store = DocumentStore()
    _, doc = store.create(200, 120)
    return doc


def _children(node: OutlineNode) -> list[OutlineNode]:
    """Narrow an outline node's children to a typed list (asserts they are present)."""
    kids = node["children"]
    assert isinstance(kids, list)
    return [child for child in kids if isinstance(child, dict)]


def test_create_and_export_roundtrip() -> None:
    doc = _doc()
    svg = export_svg(doc)
    assert svg.startswith("<svg")
    assert 'viewBox="0 0 200 120"' in svg


def test_add_shapes_get_handles_and_ids() -> None:
    doc = _doc()
    rect = ops.add_rect(doc, x=10, y=10, width=80, height=40, name="box")
    circle = ops.add_circle(doc, cx=150, cy=60, r=30)
    assert rect.tag == "rect"
    assert rect.name == "box"
    assert circle.tag == "circle"
    assert rect.id != circle.id  # unique ids


def test_resolve_by_id_and_by_name() -> None:
    doc = _doc()
    rect = ops.add_rect(doc, x=0, y=0, width=10, height=10, name="mybox")
    assert doc.resolve(rect.id) is doc.resolve("mybox")


def test_style_schema_validation_and_application() -> None:
    doc = _doc()
    style = ShapeStyle(fill="#ff0000", stroke="navy", stroke_width=2).to_style_dict()
    rect = ops.add_rect(doc, x=0, y=0, width=10, height=10, style=style)
    svg = export_svg(doc)
    assert rect.id in svg
    assert "fill:#ff0000" in svg.replace(" ", "")

    with pytest.raises(ValueError):
        ShapeStyle(fill="not-a-color")


def test_layers_groups_and_outline() -> None:
    doc = _doc()
    layer = ops.create_layer(doc, name="art")
    group = ops.create_group(doc, name="cluster", parent=layer.id)
    ops.add_rect(doc, x=0, y=0, width=10, height=10, parent=group.id)
    ops.add_circle(doc, cx=5, cy=5, r=3, parent=group.id)

    tree = outline(doc)
    assert tree["kind"] == "document"
    layer_node = _children(tree)[0]
    assert layer_node["kind"] == "layer"
    assert layer_node["name"] == "art"
    group_node = _children(layer_node)[0]
    assert group_node["kind"] == "group"
    assert _children(group_node)  # the two shapes


def test_outline_depth_limit() -> None:
    doc = _doc()
    layer = ops.create_layer(doc, name="L")
    ops.add_rect(doc, x=0, y=0, width=1, height=1, parent=layer.id)
    shallow = outline(doc, depth=1)
    layer_node = _children(shallow)[0]
    assert layer_node.get("children_count") == 1
    assert "children" not in layer_node


def test_transform_and_bbox() -> None:
    doc = _doc()
    rect = ops.add_rect(doc, x=0, y=0, width=10, height=10)
    box0 = get_bbox(doc, rect.id)
    assert box0 == {"x": 0.0, "y": 0.0, "width": 10.0, "height": 10.0}
    ops.translate_node(doc, rect.id, 5, 7)
    box1 = get_bbox(doc, rect.id)
    assert box1 is not None
    assert (box1["x"], box1["y"]) == (5.0, 7.0)


def test_restyle_merge_and_delete() -> None:
    doc = _doc()
    rect = ops.add_rect(doc, x=0, y=0, width=10, height=10, style={"fill": "red"})
    ops.restyle(doc, rect.id, {"stroke": "black"})
    svg = export_svg(doc).replace(" ", "")
    assert "fill:red" in svg and "stroke:black" in svg
    removed = ops.delete_node(doc, rect.id)
    assert removed == rect.id
    assert rect.id not in export_svg(doc)


def test_render_document_end_to_end() -> None:
    from svg_mcp.render import get_renderer
    from svg_mcp.render.base import RenderRequest

    renderer = get_renderer("resvg")
    if not renderer.available():
        pytest.skip("resvg not installed")
    doc = _doc()
    ops.add_rect(doc, x=10, y=10, width=180, height=100, style={"fill": "#4263eb"})
    ops.add_text(doc, x=100, y=70, content="hi", style={"fill": "white", "font-size": "24px"})
    result = renderer.render(RenderRequest(svg=export_svg(doc)))
    assert result.png[:8] == b"\x89PNG\r\n\x1a\n"
    assert (result.width, result.height) == (200, 120)
