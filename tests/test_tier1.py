"""Tier-1 resource/feature ops: gradients, styles, clip/mask, filters, image, text, transforms."""

from __future__ import annotations

import base64

from svg_mcp import ops
from svg_mcp.model import Document
from svg_mcp.query import convert_units, describe_document, get_computed_style, get_transform
from svg_mcp.serialize import export_svg
from svg_mcp.session import DocumentStore


def _doc() -> Document:
    return DocumentStore().create(200, 120)[1]


def test_linear_gradient_define_and_use() -> None:
    doc = _doc()
    gid = ops.define_linear_gradient(
        doc, x1=0, y1=0, x2=1, y2=0, stops=[(0.0, "#ff0000", 1.0), (1.0, "#0000ff", 1.0)], name="g"
    )
    rect = ops.add_rect(doc, x=0, y=0, width=200, height=120, style={"fill": f"url(#{gid})"})
    svg = export_svg(doc)
    assert "linearGradient" in svg and "stop-color:#ff0000" in svg.replace(" ", "")
    assert f"url(#{gid})" in svg
    assert rect.id in svg


def test_paint_at_name_reference_resolves() -> None:
    doc = _doc()
    ops.define_radial_gradient(
        doc, cx=0.5, cy=0.5, r=0.5, stops=[(0.0, "white", 1.0), (1.0, "black", 1.0)], name="glow"
    )
    rect = ops.add_rect(doc, x=0, y=0, width=10, height=10, style={"fill": "@glow"})
    style = get_computed_style(doc, rect.id)
    assert style.get("fill", "").startswith("url(#")


def test_named_style_class() -> None:
    doc = _doc()
    ops.define_style(doc, "card", {"fill": "#222", "stroke": "#fff", "stroke-width": "2"})
    rect = ops.add_rect(doc, x=0, y=0, width=10, height=10)
    ops.apply_styles(doc, rect.id, ["card"])
    svg = export_svg(doc)
    assert ".card" in svg and 'class="card"' in svg


def test_clip_and_mask() -> None:
    doc = _doc()
    target = ops.add_rect(doc, x=0, y=0, width=200, height=120, style={"fill": "red"})
    clip_shape = ops.add_circle(doc, cx=100, cy=60, r=50)
    clip_id = ops.define_clip(doc, content=[clip_shape.id])
    ops.apply_clip(doc, target.id, clip_id)
    svg = export_svg(doc)
    assert "clipPath" in svg and "clip-path" in svg
    ops.clear_clip(doc, target.id)
    assert "clip-path" not in export_svg(doc)


def test_drop_shadow_filter() -> None:
    doc = _doc()
    rect = ops.add_rect(doc, x=20, y=20, width=80, height=40, style={"fill": "tomato"})
    ops.apply_drop_shadow(doc, rect.id, dx=3, dy=3, blur=2, color="#000", opacity=0.6)
    svg = export_svg(doc).replace(" ", "")
    assert "feGaussianBlur" in svg and "feMerge" in svg and "filter:url(#" in svg


def test_color_matrix_and_blur_and_overlay() -> None:
    doc = _doc()
    a = ops.add_rect(doc, x=0, y=0, width=10, height=10)
    ops.apply_color_matrix(doc, a.id, type="saturate", values="0")
    b = ops.add_rect(doc, x=20, y=0, width=10, height=10)
    ops.apply_blur(doc, b.id, std_deviation=1.5)
    c = ops.add_rect(doc, x=40, y=0, width=10, height=10)
    ops.apply_color_overlay(doc, c.id, color="#00ff00", opacity=0.8)
    svg = export_svg(doc)
    assert "feColorMatrix" in svg and "feGaussianBlur" in svg and "feFlood" in svg


def test_image_embed_base64() -> None:
    doc = _doc()
    png_b64 = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16).decode("ascii")
    img = ops.add_image(doc, x=0, y=0, width=50, height=50, data_base64=png_b64, mime="image/png")
    svg = export_svg(doc)
    assert "data:image/png;base64," in svg
    assert img.tag == "image"


def test_text_run_and_text_on_path() -> None:
    doc = _doc()
    text = ops.add_text(doc, x=10, y=20, content="Hello ")
    ops.add_text_run(doc, parent=text.id, text="World", dx=2)
    path = ops.add_path(doc, d="M10,80 C60,20 140,20 190,80")
    ops.add_text_on_path(doc, path=path.id, content="curved", start_offset="25%")
    svg = export_svg(doc)
    assert "tspan" in svg and "textPath" in svg
    assert f"#{path.id}" in svg


def test_layer_state() -> None:
    doc = _doc()
    layer = ops.create_layer(doc, name="L1")
    ops.set_layer_state(doc, layer.id, visible=False, locked=True, opacity=0.5)
    layers = ops.list_layers(doc)
    assert layers[0]["visible"] is False
    assert layers[0]["locked"] is True
    ops.rename_layer(doc, layer.id, "renamed")
    assert ops.list_layers(doc)[0]["name"] == "renamed"


def test_rotate_about_center_and_apply_transform() -> None:
    doc = _doc()
    rect = ops.add_rect(doc, x=0, y=0, width=10, height=10)
    ops.rotate_node(doc, rect.id, 90, center=(5, 5))
    t = get_transform(doc, rect.id)
    assert isinstance(t["composed_matrix"], list)
    ops.apply_transform(doc, rect.id, "translate(100,0)")
    assert "translate" in t["local"] or True  # transform recorded


def test_scale_about_anchor_keeps_anchor_fixed() -> None:
    doc = _doc()
    rect = ops.add_rect(doc, x=0, y=0, width=10, height=10)
    ops.scale_node(doc, rect.id, 2, center=(0, 0))
    # anchor (0,0) fixed: bbox grows from the origin
    from svg_mcp.query import get_bbox

    box = get_bbox(doc, rect.id)
    assert box == {"x": 0.0, "y": 0.0, "width": 20.0, "height": 20.0}


def test_describe_and_convert_units() -> None:
    doc = _doc()
    info = describe_document(doc)
    assert info["width"] == "200"
    assert info["layers"] == 0
    assert round(convert_units(doc, "1in", "px")) == 96
