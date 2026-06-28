"""edit_text, set_transform, delete_nodes, and url(#name) paint resolution."""

from __future__ import annotations

import inkex
import pytest

from svg_mcp import ops
from svg_mcp.model.errors import InvalidArgument, NodeNotFound
from svg_mcp.session import DocumentStore

STOPS = [(0.0, "#fff", 1.0), (1.0, "#000", 1.0)]


def test_edit_text_changes_content_in_place() -> None:
    _, doc = DocumentStore().create(100, 100)
    t = ops.add_text(doc, x=10, y=20, content="hello", name="label")
    ops.edit_text(doc, "label", content="goodbye", x=15, style={"fill": "#f00"})
    el = doc.resolve(t.id)
    assert el.get_id() == t.id  # same node
    assert el.text == "goodbye"
    assert el.get("x") == "15" and el.get("y") == "20"  # y untouched
    assert el.style.get("fill") == "#f00"


def test_edit_text_rejects_non_text() -> None:
    _, doc = DocumentStore().create(100, 100)
    r = ops.add_rect(doc, x=0, y=0, width=10, height=10)
    with pytest.raises(InvalidArgument):
        ops.edit_text(doc, r.id, content="nope")


def test_set_transform_replaces_not_composes() -> None:
    _, doc = DocumentStore().create(100, 100)
    ops.create_group(doc, name="grp", transform="translate(5,5)")
    ops.apply_transform(doc, "grp", "scale(2)")  # composes -> translate then scale
    composed = str(doc.resolve("grp").transform)
    ops.set_transform(doc, "grp", "rotate(90)")  # REPLACES
    after = str(doc.resolve("grp").transform)
    assert after != composed
    assert "5" not in after  # the translate(5,5) is gone, not composed
    ops.set_transform(doc, "grp", "")  # clear
    assert str(doc.resolve("grp").transform) in ("", "matrix(1 0 0 1 0 0)")


def test_delete_nodes_bulk_is_atomic() -> None:
    _, doc = DocumentStore().create(100, 100)
    a = ops.add_rect(doc, x=0, y=0, width=5, height=5, name="a")
    b = ops.add_rect(doc, x=5, y=0, width=5, height=5, name="b")
    ids = ops.delete_nodes(doc, ["a", "b"])
    assert set(ids) == {a.id, b.id}
    for name in ("a", "b"):
        with pytest.raises(NodeNotFound):
            doc.resolve(name)
    # a bad target aborts the whole call — nothing deleted
    c = ops.add_rect(doc, x=0, y=0, width=5, height=5, name="c")
    with pytest.raises(NodeNotFound):
        ops.delete_nodes(doc, ["c", "does-not-exist"])
    assert doc.resolve("c").get_id() == c.id  # still present


def test_line_gets_default_stroke_so_its_visible() -> None:
    _, doc = DocumentStore().create(100, 100)
    line = ops.add_line(doc, x1=0, y1=0, x2=50, y2=50)  # no style
    st = doc.resolve(line.id).style
    assert st.get("stroke") == "#000000" and st.get("stroke-width") == "1"
    poly = ops.add_polyline(doc, points=[(0, 0), (10, 10)])  # no style
    assert doc.resolve(poly.id).style.get("stroke") == "#000000"
    # explicit stroke is respected, including "none"
    invisible = ops.add_line(doc, x1=0, y1=0, x2=5, y2=5, style={"stroke": "none"})
    assert doc.resolve(invisible.id).style.get("stroke") == "none"
    red = ops.add_line(doc, x1=0, y1=0, x2=5, y2=5, style={"stroke": "red", "stroke-width": "3"})
    assert doc.resolve(red.id).style.get("stroke") == "red"


def _tspans(element: inkex.BaseElement) -> list[inkex.Tspan]:
    return [c for c in element if isinstance(c, inkex.Tspan)]


def test_add_text_block_lays_out_lines() -> None:
    _, doc = DocumentStore().create(200, 200)
    ops.add_text_block(doc, x=10, y=20, content="one\ntwo\nthree", line_height=1.5, name="tb")
    spans = _tspans(doc.resolve("tb"))
    assert [s.text for s in spans] == ["one", "two", "three"]
    assert spans[0].get("dy") == "0" and spans[1].get("dy") == "1.5em"
    assert all(s.get("x") == "10" for s in spans)  # all share the anchor x


def test_edit_text_block_reflows_on_new_content() -> None:
    _, doc = DocumentStore().create(200, 200)
    tb = ops.add_text_block(doc, x=10, y=20, content="a\nb", name="tb")
    ops.edit_text_block(doc, "tb", content="a\nb\nc\nd")  # added lines — auto re-spaced
    spans = _tspans(doc.resolve(tb.id))
    assert [s.text for s in spans] == ["a", "b", "c", "d"]
    assert spans[3].get("dy") == "1.2em"  # default line_height preserved across the edit


def test_edit_text_block_respaces_via_line_height() -> None:
    _, doc = DocumentStore().create(200, 200)
    ops.add_text_block(doc, x=0, y=10, content="a\nb", line_height=1.2, name="tb")
    ops.edit_text_block(doc, "tb", line_height=2.0)  # re-space without touching content
    spans = _tspans(doc.resolve("tb"))
    assert [s.text for s in spans] == ["a", "b"]
    assert spans[1].get("dy") == "2.0em"


def test_edit_text_block_rejects_non_text() -> None:
    _, doc = DocumentStore().create(100, 100)
    r = ops.add_rect(doc, x=0, y=0, width=10, height=10)
    with pytest.raises(InvalidArgument):
        ops.edit_text_block(doc, r.id, content="nope")


def test_preview_set_backdrop_updates_state() -> None:
    from svg_mcp import preview

    srv = preview.PreviewServer()
    assert srv.state("tok")[3] == "checker"  # default
    srv.set_backdrop("tok", "#1e293b")
    gen, _active, _idx, backdrop = srv.state("tok")
    assert backdrop == "#1e293b" and gen == 1  # set + generation bumped (pushes via SSE)


def test_url_with_friendly_name_resolves() -> None:
    # The reported trap: define_* returns an id, but url(#<name>) is natural to write.
    _, doc = DocumentStore().create(100, 100)
    grad = ops.define_linear_gradient(doc, x1=0, y1=0, x2=0, y2=1, stops=STOPS, name="pageGrad")
    rect = ops.add_rect(doc, x=0, y=0, width=10, height=10, style={"fill": "url(#pageGrad)"})
    fill = doc.resolve(rect.id).style.get("fill")
    assert fill == f"url(#{grad})"  # rewritten to the real id, not left as the name
    # a real id passes through untouched
    rect2 = ops.add_rect(doc, x=0, y=0, width=10, height=10, style={"fill": f"url(#{grad})"})
    assert doc.resolve(rect2.id).style.get("fill") == f"url(#{grad})"
