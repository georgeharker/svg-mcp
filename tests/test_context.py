"""Active-document default + contextualize query set."""

from __future__ import annotations

from svg_mcp import ops
from svg_mcp.query import describe_node, list_resources
from svg_mcp.session import DocumentStore


def test_active_document_default() -> None:
    store = DocumentStore()
    assert store.active_id is None
    did, doc = store.create(100, 80)
    assert store.active_id == did
    assert store.get() is doc  # get() with no id returns the active document
    did2, doc2 = store.create(50, 50)
    assert store.active_id == did2  # newly created becomes active
    store.get(did)  # touching an explicit doc makes it active
    assert store.active_id == did
    store.set_active(did2)
    assert store.active_id == did2
    store.peek(did)  # peek does NOT change the active pointer
    assert store.active_id == did2


def test_delete_active_reassigns() -> None:
    store = DocumentStore()
    d1, _ = store.create(10, 10)
    store.create(10, 10)  # d2 active
    deleted = store.delete()  # delete the active one
    assert deleted is not None
    assert store.active_id == d1  # falls back to a remaining doc
    store.delete()
    assert store.active_id is None


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
