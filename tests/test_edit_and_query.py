"""In-place geometry editing, coordinate-frame queries, transform stacks, and fill-rule."""

from __future__ import annotations

import pytest

from svg_mcp import ops
from svg_mcp.model.document import Document
from svg_mcp.model.errors import InvalidArgument
from svg_mcp.query import get_geometry, get_params, get_transform
from svg_mcp.schemas.style import ShapeStyle
from svg_mcp.session import DocumentStore

# A rect (with its own scale) inside a translated group — exercises multi-level transforms.
_DOC = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="200" height="200" viewBox="0 0 200 200">'
    '<g id="grp" transform="translate(50,50)">'
    '<rect id="bx" x="10" y="10" width="30" height="20" transform="scale(2)"/>'
    '<path id="pa" d="M0,0 L10,0 L10,10 Z"/>'
    "</g></svg>"
)


def _doc() -> Document:
    return Document.from_svg(_DOC)


def test_fill_rule_and_clip_rule_flatten() -> None:
    sd = ShapeStyle(fill_rule="evenodd", clip_rule="nonzero").to_style_dict()
    assert sd == {"fill-rule": "evenodd", "clip-rule": "nonzero"}


def test_edit_shape_in_place_keeps_identity_and_merges_style() -> None:
    doc = _doc()
    ref = ops.edit_shape(
        doc,
        "bx",
        expect_tag="rect",
        attrs={"width": 80, "rx": 4},
        style={"fill": "#f00"},
        transform="rotate(10)",
    )
    assert ref.id == "bx"  # same node — id/clip/filter/z-order preserved
    el = doc.resolve("bx")
    assert el.get("width") == "80" and el.get("rx") == "4"
    assert el.get("x") == "10"  # untouched geometry stays
    assert el.style.get("fill") == "#f00"  # inline style merged
    assert "rotate(10)" in str(el.transform) or "matrix(" in str(el.transform)


def test_edit_shape_rejects_wrong_tag() -> None:
    doc = _doc()
    with pytest.raises(InvalidArgument):
        ops.edit_shape(doc, "bx", expect_tag="circle", attrs={"r": 5})  # bx is a rect


def test_edit_path_edits_path_and_validates() -> None:
    doc = _doc()
    ops.edit_path(doc, "pa", "M0,0 L20,0 L20,20 Z")
    assert doc.resolve("pa").get("d").startswith("M0,0 L20,0")
    with pytest.raises(InvalidArgument):
        ops.edit_path(doc, "bx", "M0 0")  # rect has no path data


def test_get_geometry_frames() -> None:
    doc = _doc()
    local = get_geometry(doc, "bx", "local")
    parent = get_geometry(doc, "bx", "parent")
    world = get_geometry(doc, "bx", "world")
    assert local is not None and parent is not None and world is not None
    # local = raw geometry; parent applies the rect's scale(2); world adds the group translate.
    assert (local["x"], local["width"]) == (10.0, 30.0)
    assert (parent["x"], parent["width"]) == (20.0, 60.0)
    assert (world["x"], world["width"]) == (70.0, 60.0)
    assert world["frame"] == "world"
    assert world["local"] == {"x": "10", "y": "10", "width": "30", "height": "20"}


def test_get_geometry_relative_to_another_node() -> None:
    doc = _doc()
    # bx measured in pa's coordinate frame (pa has no transform, so same as the group frame).
    rel = get_geometry(doc, "bx", "pa")
    assert rel is not None and rel["frame"] == "pa"
    assert (rel["x"], rel["width"]) == (20.0, 60.0)


def test_get_transform_stack() -> None:
    doc = _doc()
    tr = get_transform(doc, "bx")
    raw = tr["stack"]
    assert isinstance(raw, list)
    stack = [e for e in raw if isinstance(e, dict)]
    assert [e["tag"] for e in stack[:3]] == ["rect", "g", "svg"]
    assert stack[0]["matrix"] == [2.0, 0.0, 0.0, 2.0, 0.0, 0.0]  # rect scale(2)
    assert stack[1]["matrix"] == [1.0, 0.0, 0.0, 1.0, 50.0, 50.0]  # group translate(50,50)


def test_edit_star_reparametrizes_and_get_params_roundtrips() -> None:
    _, doc = DocumentStore().create(100, 100)
    star = ops.add_star(doc, cx=50, cy=50, outer_radius=30, inner_radius=12, sides=5)
    before = doc.resolve(star.id).get("d")
    ops.edit_star(doc, star.id, sides=8, outer_radius=40)
    after = doc.resolve(star.id)
    assert after.get("d") != before  # path re-derived
    assert after.get("sodipodi:sides") == "8" and after.get("sodipodi:r1") == "40"
    p = get_params(doc, star.id)
    assert p["kind"] == "star" and p["parametric"] is True
    params = p["params"]
    assert isinstance(params, dict)
    assert params["sides"] == 8 and params["outer_radius"] == 40.0


def test_get_params_includes_style() -> None:
    _, doc = DocumentStore().create(100, 100)
    rect = ops.add_rect(doc, x=0, y=0, width=10, height=10, style={"fill": "#abc"})
    p = get_params(doc, rect.id)
    style = p["style"]
    assert isinstance(style, dict) and style.get("fill") == "#abc"


def test_edit_path_demotes_parametric_star_to_plain_path() -> None:
    _, doc = DocumentStore().create(100, 100)
    star = ops.add_star(doc, cx=50, cy=50, outer_radius=30, inner_radius=12, sides=5)
    ops.edit_path(doc, star.id, "M0,0 L10,0 L10,10 Z")  # raw d edit
    el = doc.resolve(star.id)
    assert el.get("sodipodi:type") is None  # parametric markers stripped
    # now it's a plain path — edit_star must refuse, pointing at edit_path
    with pytest.raises(InvalidArgument):
        ops.edit_star(doc, star.id, sides=6)
    p = get_params(doc, star.id)
    assert p["kind"] == "path" and p["parametric"] is False


def test_edit_star_asserts_when_params_incomplete() -> None:
    # A node that claims to be a star but lacks its parameters must NOT be silently defaulted.
    _, doc = DocumentStore().create(100, 100)
    path = ops.add_path(doc, d="M0,0 L10,0 L10,10 Z")
    doc.resolve(path.id).set("sodipodi:type", "star")  # bogus marker, no params
    with pytest.raises(InvalidArgument) as exc:
        ops.edit_star(doc, path.id, sides=6)
    assert "edit_path" in str(exc.value)


def test_variable_width_path_edit_roundtrip() -> None:
    _, doc = DocumentStore().create(100, 100)
    vwp = ops.add_variable_width_path(doc, points=[(0, 50), (50, 50), (100, 50)], widths=[2, 10, 2])
    before = doc.resolve(vwp.id).get("d")
    ops.edit_variable_width_path(doc, vwp.id, widths=20)  # uniform width
    after = doc.resolve(vwp.id)
    assert after.get("d") != before
    p = get_params(doc, vwp.id)
    assert p["kind"] == "variable_width_path" and p["parametric"] is True
    params = p["params"]
    assert isinstance(params, dict) and params["widths"] == [20.0, 20.0, 20.0]


def test_resolve_prefers_shape_over_defs_via_edit_path() -> None:
    # End-to-end: a label shared by a gradient (defs) and a path resolves to the path.
    _, doc = DocumentStore().create(100, 100)
    ops.define_linear_gradient(
        doc, x1=0, y1=0, x2=0, y2=1, stops=[(0.0, "#fff", 1.0)], name="sheen"
    )
    path = ops.add_path(doc, d="M0,0 L10,0 L10,10 Z", name="sheen")
    ref = ops.edit_path(doc, "sheen", "M0,0 L20,0 Z")
    assert ref.id == path.id
