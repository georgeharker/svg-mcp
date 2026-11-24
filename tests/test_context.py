"""Explicitly addressed documents + contextualize query set."""

from __future__ import annotations

import pytest

from svg_mcp import ops
from svg_mcp.model.errors import DocumentNotFound
from svg_mcp.query import describe_node, list_resources
from svg_mcp.session import DocumentStore


def test_documents_are_always_addressed_by_id() -> None:
    # There is no active document: every lookup names its document.
    store = DocumentStore()
    did, doc = store.create(100, 80)
    did2, doc2 = store.create(50, 50)
    assert store.get(did) is doc and store.get(did2) is doc2
    assert not hasattr(store, "active_id")
    with pytest.raises(DocumentNotFound, match="open: doc1, doc2"):
        store.get("doc9")


def test_delete_needs_an_id() -> None:
    store = DocumentStore()
    d1, _ = store.create(10, 10)
    d2, _ = store.create(10, 10)
    assert store.delete(d2) == d2
    assert store.list_ids() == [d1]
    with pytest.raises(DocumentNotFound):
        store.delete(d2)


def test_describe_node() -> None:
    _, doc = DocumentStore().create(200, 120)
    ops.add_rect(doc, x=10, y=20, width=30, height=40, name="r", style={"fill": "red"})
    info = describe_node(doc, "r")
    assert info["tag"] == "rect" and info["name"] == "r" and info["kind"] == "shape"
    assert info["world_bbox"] == [10.0, 20.0, 30.0, 40.0]
    assert isinstance(info["computed_style"], dict) and isinstance(info["transform"], dict)


def test_list_resources() -> None:
    _, doc = DocumentStore().create(200, 120)
    gid = ops.define_linear_gradient(
        doc, x1=0, y1=0, x2=1, y2=0, stops=[(0.0, "#f00", 1.0), (1.0, "#00f", 1.0)], name="g"
    )
    ops.define_style(doc, "card", {"fill": "#222"})
    shape = ops.add_circle(doc, cx=5, cy=5, r=3)
    ops.define_clip(doc, content=[shape.id])
    res = list_resources(doc)
    assert any(item["id"] == gid for item in res["gradients"])
    assert res["styles"] == [{"name": "card"}]
    assert len(res["clips"]) == 1
