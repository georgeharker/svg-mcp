"""Tier-3 ops: flowed text, mesh gradient, anchor, displacement filter, guides, pages."""

from __future__ import annotations

from svg_mcp import ops
from svg_mcp.model import Document
from svg_mcp.serialize import export_svg
from svg_mcp.session import DocumentStore


def _doc() -> Document:
    return DocumentStore().create(200, 120)[1]


def test_flowed_text() -> None:
    doc = _doc()
    fr = ops.add_flowed_text(doc, x=10, y=10, width=180, height=100, paragraphs=["Hello", "World"])
    svg = export_svg(doc)
    assert "flowRoot" in svg and "flowRegion" in svg and "flowPara" in svg
    assert fr.tag == "flowRoot"


def test_mesh_gradient_defines() -> None:
    doc = _doc()
    mid = ops.define_mesh_gradient(doc, x=0, y=0, rows=2, cols=2, name="mesh")
    svg = export_svg(doc)
    assert "meshgradient" in svg and mid in svg


def test_wrap_in_link() -> None:
    doc = _doc()
    rect = ops.add_rect(doc, x=0, y=0, width=10, height=10)
    link = ops.wrap_in_link(doc, href="https://example.com", children=[rect.id])
    svg = export_svg(doc)
    assert "<a" in svg and "example.com" in svg
    assert link.tag == "a"


def test_displacement_map_filter() -> None:
    doc = _doc()
    rect = ops.add_rect(doc, x=0, y=0, width=50, height=50, style={"fill": "purple"})
    ops.apply_displacement_map(doc, rect.id, scale=8)
    svg = export_svg(doc)
    assert "feDisplacementMap" in svg and "feTurbulence" in svg


def test_guides_and_pages() -> None:
    doc = _doc()
    g = ops.add_guide(doc, position=(100, 0))
    assert g["id"]
    guides = ops.list_guides(doc)
    assert len(guides) == 1
    p = ops.add_page(doc, x=0, y=0, width=200, height=120, label="Page 1")
    assert p["label"] == "Page 1"
    assert len(ops.list_pages(doc)) >= 1
