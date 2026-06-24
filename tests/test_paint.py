"""@name paint-reference resolution in restyle and define_style (not just at creation)."""

from __future__ import annotations

from svg_mcp import ops
from svg_mcp.model.document import Document
from svg_mcp.serialize import export_svg
from svg_mcp.session import DocumentStore

STOPS = [(0.0, "#3b82f6", 1.0), (1.0, "#0b1736", 1.0)]


def _doc_with_gradient() -> tuple[Document, str]:
    _, doc = DocumentStore().create(100, 100)
    grad_id = ops.define_linear_gradient(doc, x1=0, y1=0, x2=0, y2=1, stops=STOPS, name="g")
    return doc, grad_id


def test_restyle_resolves_at_name() -> None:
    doc, grad_id = _doc_with_gradient()
    rect = ops.add_rect(doc, x=0, y=0, width=50, height=50)
    ops.restyle(doc, rect.id, {"fill": "@g"})
    svg = export_svg(doc)
    assert f"url(#{grad_id})" in svg
    assert "@g" not in svg  # the shorthand must not leak through


def test_define_style_resolves_at_name() -> None:
    doc, grad_id = _doc_with_gradient()
    ops.define_style(doc, "brand", {"fill": "@g"})
    rect = ops.add_rect(doc, x=0, y=0, width=50, height=50)
    ops.apply_styles(doc, rect.id, ["brand"])
    svg = export_svg(doc)
    assert f"url(#{grad_id})" in svg  # the CSS class references the gradient by url
    assert "fill:@g" not in svg
