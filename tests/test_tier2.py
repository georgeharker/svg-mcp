"""Tier-2 ops: path factories/ops, symbol/use, pattern, marker, filters, metadata, selectors."""

from __future__ import annotations

from svg_mcp import ops
from svg_mcp.model import Document
from svg_mcp.ops.resources import FePrimitive
from svg_mcp.query import extract_image, find, get_subtree
from svg_mcp.serialize import export_svg
from svg_mcp.session import DocumentStore


def _doc() -> Document:
    return DocumentStore().create(200, 120)[1]


def test_add_arc_and_star() -> None:
    doc = _doc()
    arc = ops.add_arc(doc, cx=50, cy=50, rx=30, ry=20, arctype="slice")
    star = ops.add_star(doc, cx=120, cy=60, outer_radius=30, inner_radius=14, sides=5)
    svg = export_svg(doc)
    assert arc.tag == "path" and star.tag == "path"
    assert svg.count("<path") == 2 or "path" in svg


def test_path_ops() -> None:
    doc = _doc()
    p = ops.add_path(doc, d="m0,0 l10,0 l0,10 z")
    ops.path_to_absolute(doc, p.id)
    box = ops.path_bbox(doc, p.id)
    assert box == {"x": 0.0, "y": 0.0, "width": 10.0, "height": 10.0}
    ops.path_transform(doc, p.id, "translate(5,5)")
    box2 = ops.path_bbox(doc, p.id)
    assert box2 is not None and (box2["x"], box2["y"]) == (5.0, 5.0)


def test_symbol_use_unlink() -> None:
    doc = _doc()
    shape = ops.add_circle(doc, cx=10, cy=10, r=8)
    sym = ops.define_symbol(doc, content=[shape.id], name="dot")
    use = ops.add_use(doc, target=sym, x=40, y=40)
    svg = export_svg(doc)
    assert "<symbol" in svg and "<use" in svg and f"#{sym}" in svg
    expanded = ops.unlink_use(doc, use.id)
    assert expanded.id


def test_pattern_and_marker() -> None:
    doc = _doc()
    tile = ops.add_circle(doc, cx=5, cy=5, r=3, style={"fill": "red"})
    pat = ops.define_pattern(doc, content=[tile.id], width=10, height=10, units="userSpaceOnUse")
    rect = ops.add_rect(doc, x=0, y=0, width=100, height=100, style={"fill": f"url(#{pat})"})
    arrow = ops.add_path(doc, d="M0,0 L10,5 L0,10 z", style={"fill": "black"})
    mk = ops.define_marker(doc, content=[arrow.id], ref_x=5, ref_y=5)
    line = ops.add_line(doc, x1=0, y1=0, x2=50, y2=50, style={"stroke": "black"})
    ops.apply_marker(doc, line.id, mk, position="end")
    svg = export_svg(doc)
    assert "<pattern" in svg and "<marker" in svg and "marker-end" in svg
    assert rect.id in svg


def test_raw_define_filter_graph() -> None:
    doc = _doc()
    rect = ops.add_rect(doc, x=0, y=0, width=50, height=50, style={"fill": "teal"})
    fid = ops.define_filter(
        doc,
        primitives=[
            FePrimitive(
                "feGaussianBlur", {"in": "SourceGraphic", "stdDeviation": "2", "result": "b"}
            ),
            FePrimitive("feMerge", {}, [FePrimitive("feMergeNode", {"in": "b"})]),
        ],
        name="myfilter",
    )
    ops.apply_filter(doc, rect.id, fid)
    svg = export_svg(doc).replace(" ", "")
    assert "feGaussianBlur" in svg and "feMergeNode" in svg and "filter:url(#" in svg


def test_morphology_turbulence_component_transfer() -> None:
    doc = _doc()
    a = ops.add_rect(doc, x=0, y=0, width=10, height=10)
    ops.apply_morphology(doc, a.id, operator="dilate", radius=2)
    b = ops.add_rect(doc, x=20, y=0, width=10, height=10)
    ops.apply_turbulence(doc, b.id, base_frequency=0.1, num_octaves=2)
    c = ops.add_rect(doc, x=40, y=0, width=10, height=10)
    ops.apply_component_transfer(doc, c.id, func_type="table", table_values="0 1")
    svg = export_svg(doc)
    assert "feMorphology" in svg and "feTurbulence" in svg and "feComponentTransfer" in svg


def test_metadata_title_desc() -> None:
    doc = _doc()
    rect = ops.add_rect(doc, x=0, y=0, width=10, height=10)
    ops.set_title(doc, rect.id, "A box")
    ops.set_description(doc, rect.id, "A teal box for testing")
    svg = export_svg(doc)
    assert "<title>A box</title>" in svg and "A teal box" in svg


def test_find_and_get_subtree() -> None:
    doc = _doc()
    layer = ops.create_layer(doc, name="L")
    r1 = ops.add_rect(doc, x=0, y=0, width=10, height=10, parent=layer.id, name="box1")
    ops.add_circle(doc, cx=5, cy=5, r=3, parent=layer.id)
    rects = find(doc, types=["rect"])
    assert len(rects) == 1 and rects[0]["id"] == r1.id
    by_name = find(doc, name="box1")
    assert by_name[0]["id"] == r1.id
    sub = get_subtree(doc, layer.id)
    assert sub["svg"].startswith("<") and isinstance(sub["outline"], dict)


def test_extract_image() -> None:
    doc = _doc()
    img = ops.add_image(doc, x=0, y=0, width=10, height=10, data_base64="QUJD", mime="image/png")
    extracted = extract_image(doc, img.id)
    assert extracted == {"mime": "image/png", "data_base64": "QUJD"}
