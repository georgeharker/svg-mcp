"""Structural ops: ungroup, z-order, world-preserving reparent, duplicate."""

from __future__ import annotations

from svg_mcp import ops
from svg_mcp.model import Document
from svg_mcp.query import get_bbox, outline
from svg_mcp.query.outline import OutlineNode
from svg_mcp.session import DocumentStore


def _doc() -> Document:
    return DocumentStore().create(200, 120)[1]


def _child_ids(node: OutlineNode) -> list[str]:
    kids = node.get("children")
    assert isinstance(kids, list)
    return [c["id"] for c in kids if isinstance(c, dict) and isinstance(c.get("id"), str)]


def test_ungroup_preserves_world_position() -> None:
    doc = _doc()
    group = ops.create_group(doc, name="g", transform="translate(50,30)")
    rect = ops.add_rect(doc, x=0, y=0, width=10, height=10, parent=group.id)
    before = get_bbox(doc, rect.id)
    assert before == {"x": 50.0, "y": 30.0, "width": 10.0, "height": 10.0}
    freed = ops.ungroup(doc, group.id)
    assert rect.id in freed
    after = get_bbox(doc, rect.id)
    assert after == before  # baked transform keeps it in place


def test_zorder() -> None:
    doc = _doc()
    a = ops.add_rect(doc, x=0, y=0, width=10, height=10, name="a")
    b = ops.add_rect(doc, x=0, y=0, width=10, height=10, name="b")
    ops.add_rect(doc, x=0, y=0, width=10, height=10, name="c")
    # order is a, b, c (c on top)
    ops.to_front(doc, a.id)
    order = _child_ids(outline(doc))
    assert order[-1] == a.id
    ops.to_back(doc, b.id)
    assert _child_ids(outline(doc))[0] == b.id
    ops.raise_node(doc, b.id)
    assert _child_ids(outline(doc))[1] == b.id


def test_reparent_keep_world_position() -> None:
    doc = _doc()
    group = ops.create_group(doc, name="g", transform="translate(40,20)")
    rect = ops.add_rect(doc, x=0, y=0, width=10, height=10)  # at root, world (0,0)
    ops.reparent(doc, rect.id, group.id, keep_world_position=True)
    after = get_bbox(doc, rect.id)
    assert after == {"x": 0.0, "y": 0.0, "width": 10.0, "height": 10.0}  # unchanged in world


def test_duplicate() -> None:
    doc = _doc()
    rect = ops.add_rect(doc, x=5, y=5, width=10, height=10)
    copy = ops.duplicate(doc, rect.id)
    assert copy.id != rect.id
    assert get_bbox(doc, copy.id) == get_bbox(doc, rect.id)
