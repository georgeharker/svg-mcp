"""In-place geometry editing, coordinate-frame queries, transform stacks, and fill-rule."""

from __future__ import annotations

import pytest

from svg_mcp import ops
from svg_mcp.model.document import Document
from svg_mcp.model.errors import InvalidArgument, NodeNotFound
from svg_mcp.query import get_geometry, get_params, get_transform
from svg_mcp.schemas.style import ShapeStyle
from svg_mcp.serialize import export_svg
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


def test_edit_style_merges_and_replaces() -> None:
    _, doc = DocumentStore().create(100, 100)
    ops.define_style(doc, "card", {"fill": "#ff0000", "stroke": "#000000"})
    ops.edit_style(doc, "card", {"stroke-width": "2"})  # merge: keeps fill+stroke
    assert doc.styles["card"] == {"fill": "#ff0000", "stroke": "#000000", "stroke-width": "2"}
    ops.edit_style(doc, "card", {"fill": "#0000ff"}, replace=True)  # wholesale
    assert doc.styles["card"] == {"fill": "#0000ff"}


def test_edit_style_requires_existing() -> None:
    _, doc = DocumentStore().create(100, 100)
    with pytest.raises(InvalidArgument):
        ops.edit_style(doc, "ghost", {"fill": "#fff"})


def test_delete_style_removes_it() -> None:
    _, doc = DocumentStore().create(100, 100)
    ops.define_style(doc, "card", {"fill": "#ff0000"})
    ops.delete_style(doc, "card")
    assert "card" not in doc.styles
    with pytest.raises(InvalidArgument):
        ops.delete_style(doc, "card")


def test_restyle_many_applies_per_node_styles() -> None:
    _, doc = DocumentStore().create(100, 100)
    a = ops.add_rect(doc, x=0, y=0, width=10, height=10, name="a")
    b = ops.add_rect(doc, x=20, y=20, width=10, height=10, name="b", style={"fill": "#fff"})
    refs = ops.restyle_many(
        doc, [("a", {"fill": "#ff0000"}, False), ("b", {"stroke": "#00ff00"}, True)]
    )
    assert [r.id for r in refs] == [a.id, b.id]
    assert "ff0000" in str(doc.resolve("a").style)
    assert "00ff00" in str(doc.resolve("b").style) and "fff" not in str(doc.resolve("b").style)


def test_document_store_replace_keeps_id() -> None:
    store = DocumentStore()
    did, doc1 = store.create(100, 100)
    ops.add_rect(doc1, x=0, y=0, width=5, height=5, name="orig")
    new_doc = ops.load_svg_document(
        svg='<svg xmlns="http://www.w3.org/2000/svg" width="50" height="50"></svg>'
    )
    rid = store.replace(None, new_doc)  # import-into-active: same id, new content
    assert rid == did and store.active_id == did
    assert store.get(did) is new_doc  # the document behind the id was swapped


def test_handles_survive_export_reimport() -> None:
    # Author with a name + parametric spec, serialize, reimport: id/name/spec all persist.
    _, doc = DocumentStore().create(200, 200)
    sq = ops.add_squircle(doc, x=20, y=20, width=120, height=120, radius=30, name="card")
    svg = export_svg(doc)
    assert f'id="{sq.id}"' in svg and 'inkscape:label="card"' in svg and "data-squircle" in svg
    doc2 = ops.load_svg_document(svg=svg)
    assert doc2.resolve("card").get_id() == sq.id  # resolve by name survives
    p = get_params(doc2, "card")
    assert p["kind"] == "squircle" and p["parametric"] is True  # parametric spec survives
    ops.edit_squircle(doc2, "card", radius=45)  # still editable in place
    params = get_params(doc2, "card")["params"]
    assert isinstance(params, dict) and params["radius"] == 45.0


def test_resize_document_modes() -> None:
    def fresh() -> Document:
        _, d = DocumentStore().create(400, 400)
        ops.add_circle(d, cx=120, cy=120, r=40)
        ops.add_rect(d, x=170, y=170, width=60, height=60)
        return d

    plain = ops.resize_document(fresh(), width=200, height=200, mode="plain")
    assert plain == {"width": "200", "height": "200", "viewBox": "0 0 200 200"}

    scaled = ops.resize_document(fresh(), width=800, height=800, mode="scale")
    assert scaled["width"] == "800" and scaled["viewBox"] == "0 0 400 400"  # viewBox kept → scales

    fit = ops.resize_document(fresh(), mode="fit", margin=10)
    # content spans (80,80)-(230,230); +10 margin → viewBox 70 70 170 170
    assert fit["viewBox"] is not None
    vx, vy, vw, vh = (float(t) for t in fit["viewBox"].split())
    assert abs(vx - 70) < 1 and abs(vy - 70) < 1 and abs(vw - 170) < 1 and abs(vh - 170) < 1


def test_resize_document_validation() -> None:
    _, doc = DocumentStore().create(100, 100)
    with pytest.raises(InvalidArgument):
        ops.resize_document(doc, mode="plain")  # missing width/height
    with pytest.raises(InvalidArgument):
        ops.resize_document(doc, width=50, height=50, mode="bogus")


def test_define_arrow_marker_presets_and_apply() -> None:
    _, doc = DocumentStore().create(120, 60)
    for preset in ("triangle", "barbed", "stealth", "diamond", "open", "dot"):
        mk = ops.define_arrow_marker(doc, preset=preset, color="#123456")
        assert mk  # returns a marker id
    line = ops.add_path(doc, d="M10,30 L110,30", style={"fill": "none", "stroke": "#000"})
    ops.apply_marker(doc, line.id, mk, position="end")
    svg = export_svg(doc)
    assert "<marker" in svg and "orient" in svg and "marker-end" in svg


def test_define_arrow_marker_rejects_unknown_preset() -> None:
    _, doc = DocumentStore().create(50, 50)
    with pytest.raises(InvalidArgument):
        ops.define_arrow_marker(doc, preset="zigzag")


def test_name_clash_warns_on_create() -> None:
    _, doc = DocumentStore().create(100, 100)
    a = ops.add_rect(doc, x=0, y=0, width=10, height=10, name="box")
    b = ops.add_rect(doc, x=20, y=20, width=10, height=10, name="box")
    assert a.warning is None  # first use is clean
    assert b.warning is not None and "box" in b.warning  # the collision is flagged
    assert "warning" in b.as_dict() and "warning" not in a.as_dict()


def test_name_clash_warns_across_shape_and_resource() -> None:
    # The @name footgun: a gradient and a shape sharing a name make `@name` ambiguous.
    _, doc = DocumentStore().create(100, 100)
    ops.define_linear_gradient(doc, x1=0, y1=0, x2=0, y2=1, stops=[(0.0, "#fff", 1.0)], name="body")
    sq = ops.add_squircle(doc, x=0, y=0, width=50, height=50, radius=8, name="body")
    assert sq.warning is not None and "body" in sq.warning


def test_unique_name_has_no_warning() -> None:
    _, doc = DocumentStore().create(100, 100)
    r = ops.add_rect(doc, x=0, y=0, width=10, height=10, name="solo")
    assert r.warning is None and "warning" not in r.as_dict()


def test_name_warning_self_heals_after_delete() -> None:
    # A reused name is clean again once the prior holder is gone (index prunes stale ids).
    _, doc = DocumentStore().create(100, 100)
    ghost = ops.add_rect(doc, x=0, y=0, width=10, height=10, name="ghost")
    ops.delete_node(doc, ghost.id)
    again = ops.add_rect(doc, x=20, y=20, width=10, height=10, name="ghost")
    assert again.warning is None


def test_name_equal_to_existing_id_warns() -> None:
    _, doc = DocumentStore().create(100, 100)
    a = ops.add_rect(doc, x=0, y=0, width=10, height=10)
    b = ops.add_rect(doc, x=20, y=20, width=10, height=10, name=a.id)  # name collides with an id
    assert b.warning is not None


def test_set_name_clash_warns() -> None:
    _, doc = DocumentStore().create(100, 100)
    ops.add_rect(doc, x=0, y=0, width=10, height=10, name="taken")
    other = ops.add_rect(doc, x=20, y=20, width=10, height=10, name="free")
    renamed = ops.set_name(doc, other.id, "taken")
    assert renamed.warning is not None


def test_duplicate_with_style_replaces_only_the_copy() -> None:
    _, doc = DocumentStore().create(100, 100)
    sq = ops.add_squircle(doc, x=10, y=10, width=40, height=40, radius=8, style={"fill": "#ff0000"})
    copy = ops.duplicate(doc, sq.id, style={"fill": "#0000ff"})
    assert copy.id != sq.id
    assert "0000ff" in str(doc.resolve(copy.id).style)  # copy recolored
    assert "ff0000" in str(doc.resolve(sq.id).style)  # original untouched
    assert get_params(doc, copy.id)["kind"] == "squircle"  # still a parametric copy


def test_offset_path_squircle_stays_parametric_and_grows() -> None:
    # Tier A: offsetting a squircle regenerates its params exactly and stays a re-editable squircle.
    _, doc = DocumentStore().create(300, 300)
    sq = ops.add_squircle(doc, x=80, y=80, width=140, height=140, radius=30, smoothness=0.6)
    out = ops.offset_path(doc, sq.id, 20, name="ring")
    p = get_params(doc, out.id)
    assert p["kind"] == "squircle" and p["parametric"] is True
    params = p["params"]
    assert isinstance(params, dict)
    assert params["x"] == 60.0 and params["width"] == 180.0 and params["radius"] == 50.0
    assert out.id != sq.id  # a new node beside the original
    orig = get_params(doc, sq.id)["params"]
    assert isinstance(orig, dict) and orig["width"] == 140.0  # original untouched


def test_offset_path_pill_and_rounded_polygon_parametric() -> None:
    _, doc = DocumentStore().create(300, 300)
    pill = ops.add_pill(doc, x=40, y=120, width=200, height=60)
    pout = ops.offset_path(doc, pill.id, 10)
    pp = get_params(doc, pout.id)
    assert pp["kind"] == "pill"
    assert isinstance(pp["params"], dict) and pp["params"]["width"] == 220.0
    rp = ops.add_rounded_polygon(doc, cx=150, cy=150, radius=80, corner_radius=20, sides=6)
    rout = ops.offset_path(doc, rp.id, 12)
    rpp = get_params(doc, rout.id)
    assert rpp["kind"] == "rounded_polygon"
    assert isinstance(rpp["params"], dict) and rpp["params"]["corner_radius"] == 32.0


def test_offset_path_plain_shape_returns_offset_path() -> None:
    # Tier B: a non-parametric shape is offset into a new plain path (analytic Bézier offset).
    _, doc = DocumentStore().create(300, 300)
    circle = ops.add_circle(doc, cx=150, cy=150, r=80)
    out = ops.offset_path(doc, circle.id, 20)
    el = doc.resolve(out.id)
    assert el.TAG == "path"
    bbox = el.bounding_box()
    assert bbox is not None
    assert abs(bbox.width - 200) < 8 and abs(bbox.height - 200) < 8  # r≈100 → ~200 across


def test_offset_path_inset_shrinks() -> None:
    _, doc = DocumentStore().create(300, 300)
    circle = ops.add_circle(doc, cx=150, cy=150, r=80)
    out = ops.offset_path(doc, circle.id, -20)
    bbox = doc.resolve(out.id).bounding_box()
    assert bbox is not None
    assert abs(bbox.width - 120) < 8  # r≈60 → ~120 across


def test_squircle_edit_reparametrizes_and_get_params_roundtrips() -> None:
    _, doc = DocumentStore().create(200, 200)
    sq = ops.add_squircle(doc, x=10, y=10, width=120, height=80, radius=24, smoothness=0.6)
    el = doc.resolve(sq.id)
    assert el.TAG == "path" and (el.get("d") or "").startswith("M")
    before = el.get("d")
    ops.edit_squircle(doc, sq.id, radius=40, smoothness=1.0)  # partial edit
    after = doc.resolve(sq.id)
    assert after.get("d") != before  # path re-derived
    p = get_params(doc, sq.id)
    assert p["kind"] == "squircle" and p["parametric"] is True
    params = p["params"]
    assert isinstance(params, dict)
    # unchanged params carry over; changed ones take effect
    assert params["width"] == 120.0 and params["x"] == 10.0
    assert params["radius"] == 40.0 and params["smoothness"] == 1.0


def test_edit_squircle_asserts_when_not_a_squircle() -> None:
    _, doc = DocumentStore().create(100, 100)
    path = ops.add_path(doc, d="M0,0 L10,0 L10,10 Z")
    with pytest.raises(InvalidArgument):
        ops.edit_squircle(doc, path.id, radius=5)


def test_edit_path_demotes_squircle_to_plain_path() -> None:
    _, doc = DocumentStore().create(100, 100)
    sq = ops.add_squircle(doc, x=0, y=0, width=60, height=60, radius=12)
    ops.edit_path(doc, sq.id, "M0,0 L10,0 L10,10 Z")  # raw d edit strips the spec
    assert doc.resolve(sq.id).get("data-squircle") is None
    with pytest.raises(InvalidArgument):
        ops.edit_squircle(doc, sq.id, radius=6)
    p = get_params(doc, sq.id)
    assert p["kind"] == "path" and p["parametric"] is False


def test_rounded_polygon_edit_roundtrip_and_demotion() -> None:
    _, doc = DocumentStore().create(200, 200)
    rp = ops.add_rounded_polygon(doc, cx=100, cy=100, radius=70, corner_radius=18, sides=6)
    before = doc.resolve(rp.id).get("d")
    ops.edit_rounded_polygon(doc, rp.id, sides=8, smoothness=1.0)
    after = doc.resolve(rp.id)
    assert after.get("d") != before
    p = get_params(doc, rp.id)
    assert p["kind"] == "rounded_polygon" and p["parametric"] is True
    params = p["params"]
    assert isinstance(params, dict) and params["sides"] == 8.0 and params["smoothness"] == 1.0
    # raw d edit demotes it
    ops.edit_path(doc, rp.id, "M0,0 L10,0 L10,10 Z")
    assert doc.resolve(rp.id).get("data-rounded-polygon") is None
    with pytest.raises(InvalidArgument):
        ops.edit_rounded_polygon(doc, rp.id, sides=5)


def test_superellipse_edit_roundtrip() -> None:
    _, doc = DocumentStore().create(200, 200)
    se = ops.add_superellipse(doc, cx=100, cy=100, rx=70, ry=50, exponent=4)
    before = doc.resolve(se.id).get("d")
    ops.edit_superellipse(doc, se.id, exponent=8)
    after = doc.resolve(se.id)
    assert after.get("d") != before
    p = get_params(doc, se.id)
    assert p["kind"] == "superellipse" and p["parametric"] is True
    params = p["params"]
    assert isinstance(params, dict) and params["exponent"] == 8.0 and params["rx"] == 70.0


def test_pill_edit_roundtrip() -> None:
    _, doc = DocumentStore().create(200, 120)
    pill = ops.add_pill(doc, x=10, y=30, width=160, height=60)
    p = get_params(doc, pill.id)
    assert p["kind"] == "pill" and p["parametric"] is True
    before = doc.resolve(pill.id).get("d")
    ops.edit_pill(doc, pill.id, smoothness=0.6)
    after = doc.resolve(pill.id)
    assert after.get("d") != before
    params = get_params(doc, pill.id)["params"]
    assert isinstance(params, dict) and params["smoothness"] == 0.6 and params["width"] == 160.0


def test_boolean_difference_masks_the_subject() -> None:
    _, doc = DocumentStore().create(120, 120)
    a = ops.add_rect(doc, x=10, y=10, width=80, height=80)
    b = ops.add_circle(doc, cx=90, cy=90, r=30)
    res = ops.boolean(doc, op="difference", targets=[a.id, b.id])
    assert res.id == a.id  # subject is kept, masked
    assert doc.resolve(a.id).get("mask") is not None
    assert "mask" in export_svg(doc)


def test_boolean_intersection_clips_the_subject() -> None:
    _, doc = DocumentStore().create(120, 120)
    a = ops.add_rect(doc, x=10, y=10, width=80, height=80)
    b = ops.add_circle(doc, cx=60, cy=60, r=40)
    ops.boolean(doc, op="intersection", targets=[a.id, b.id])
    assert doc.resolve(a.id).get("clip-path") is not None
    # the clipPath must actually CONTAIN the operand — a single-shape operand must not be deleted
    # out of it (regression: it clipped to an empty path → blank).
    svg = export_svg(doc)
    assert "<clipPath" in svg and "<circle" in svg.split("<clipPath", 1)[1]


def test_boolean_exclusion_merges_to_evenodd_path() -> None:
    _, doc = DocumentStore().create(120, 120)
    a = ops.add_rect(doc, x=10, y=10, width=60, height=60)
    b = ops.add_rect(doc, x=40, y=40, width=60, height=60)
    res = ops.boolean(doc, op="exclusion", targets=[a.id, b.id], name="xor")
    assert res.tag == "path"
    assert "evenodd" in str(doc.resolve(res.id).style)
    # inputs consumed
    with pytest.raises(NodeNotFound):
        doc.resolve(a.id)


def test_boolean_union_groups_inputs() -> None:
    _, doc = DocumentStore().create(120, 120)
    a = ops.add_rect(doc, x=10, y=10, width=50, height=50)
    b = ops.add_circle(doc, cx=80, cy=80, r=25)
    res = ops.boolean(doc, op="union", targets=[a.id, b.id])
    group = doc.resolve(res.id)
    assert group.tag_name == "g" or str(group.TAG).endswith("g")
    assert {str(c.get_id()) for c in group} >= {a.id, b.id}


def test_boolean_difference_with_composite_group_operand() -> None:
    # A group of two circles subtracts as two real holes (children recolored, not left as overlays).
    _, doc = DocumentStore().create(160, 160)
    sq = ops.add_rect(doc, x=20, y=20, width=110, height=110)
    c1 = ops.add_circle(doc, cx=60, cy=60, r=20, style={"fill": "#ff0000"})
    c2 = ops.add_circle(doc, cx=100, cy=100, r=20, style={"fill": "#00ff00"})
    grp = ops.create_group(doc, children=[c1.id, c2.id])
    ops.boolean(doc, op="difference", targets=[sq.id, grp.id])
    svg = export_svg(doc)
    assert doc.resolve(sq.id).get("mask") is not None
    # both circles were recolored black inside the mask (no surviving red/green)
    assert "#ff0000" not in svg and "#00ff00" not in svg


@pytest.mark.parametrize("transform", ["translate(30,30)", "rotate(45)", "rotate(30) scale(1.5)"])
def test_boolean_exclusion_respects_ancestor_transform(transform: str) -> None:
    # Regression: flattening a group operand must concat the FULL ancestor transform (translate,
    # rotate, scale, …) and divide it back out, not bake it into the result d. The result is a child
    # of the transformed group, so its LOCAL d stays in the un-transformed frame (bbox 0..90);
    # before the fix the ancestor transform was doubly applied, shifting/rotating that local bbox.
    _, doc = DocumentStore().create(200, 200)
    g = ops.create_group(doc, transform=transform)
    a = ops.add_rect(doc, x=0, y=0, width=60, height=60, parent=g.id)
    b = ops.add_rect(doc, x=30, y=30, width=60, height=60, parent=g.id)
    res = ops.boolean(doc, op="exclusion", targets=[a.id, b.id])
    bbox = doc.resolve(res.id).bounding_box()
    assert bbox is not None
    assert abs(bbox.left) < 1.0 and abs(bbox.right - 90) < 1.0
    assert abs(bbox.top) < 1.0 and abs(bbox.bottom - 90) < 1.0


def test_boolean_rejects_single_target_and_bad_op() -> None:
    _, doc = DocumentStore().create(100, 100)
    a = ops.add_rect(doc, x=0, y=0, width=10, height=10)
    with pytest.raises(InvalidArgument):
        ops.boolean(doc, op="difference", targets=[a.id])
    b = ops.add_rect(doc, x=20, y=20, width=10, height=10)
    with pytest.raises(InvalidArgument):
        ops.boolean(doc, op="bogus", targets=[a.id, b.id])


def test_resolve_prefers_shape_over_defs_via_edit_path() -> None:
    # End-to-end: a label shared by a gradient (defs) and a path resolves to the path.
    _, doc = DocumentStore().create(100, 100)
    ops.define_linear_gradient(
        doc, x1=0, y1=0, x2=0, y2=1, stops=[(0.0, "#fff", 1.0)], name="sheen"
    )
    path = ops.add_path(doc, d="M0,0 L10,0 L10,10 Z", name="sheen")
    ref = ops.edit_path(doc, "sheen", "M0,0 L20,0 Z")
    assert ref.id == path.id
