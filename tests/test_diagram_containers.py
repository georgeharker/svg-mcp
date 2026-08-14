"""Diagram containers: the box fitted around a set of nodes, and the re-fit that keeps it there."""

from __future__ import annotations

import io

import pytest

from svg_mcp import ops
from svg_mcp.model import Document
from svg_mcp.model.errors import InvalidArgument
from svg_mcp.ops.diagram import read_container_spec
from svg_mcp.query import get_params
from svg_mcp.render import get_renderer
from svg_mcp.render.base import RenderRequest
from svg_mcp.serialize import export_svg
from svg_mcp.session import DocumentStore

TOL = 0.01

Box = tuple[float, float, float, float]


def _doc() -> Document:
    return DocumentStore().create(600, 400)[1]


def _classes(doc: Document, node_id: str) -> list[str]:
    return str(doc.resolve(node_id).get("class") or "").split()


def _rect(doc: Document, container_id: str) -> Box:
    """The box a container is drawn at, read straight off the rect it owns."""
    box = doc.resolve(container_id)[0]
    return tuple(float(str(box.get(key))) for key in ("x", "y", "width", "height"))  # type: ignore[return-value]


def _pair(doc: Document) -> tuple[str, str]:
    a = ops.add_diagram_node(doc, kind="service", label="A", x=40, y=40, width=100, height=60)
    b = ops.add_diagram_node(doc, kind="service", label="B", x=200, y=100, width=80, height=40)
    return a.ref.id, b.ref.id


# --- 1. the fitted box -------------------------------------------------------


def test_an_unlabelled_container_is_the_member_union_padded() -> None:
    doc = _doc()
    a, b = _pair(doc)
    placed = ops.add_diagram_container(doc, members=[a, b])
    # members span (40, 40)-(280, 140); --pad-container is 16 on every side.
    assert (placed.x, placed.y, placed.w, placed.h) == (24.0, 24.0, 272.0, 132.0)
    assert _rect(doc, placed.ref.id) == (24.0, 24.0, 272.0, 132.0)
    assert placed.auto is True


def test_a_label_lifts_the_top_edge_by_its_own_headroom() -> None:
    doc = _doc()
    a, b = _pair(doc)
    placed = ops.add_diagram_container(doc, members=[a, b], label="Core")
    # The same box, with (label size 12 + 8) of clear air added above the members.
    assert (placed.x, placed.y, placed.w, placed.h) == (24.0, 4.0, 272.0, 152.0)


def test_the_label_sits_one_pad_inside_the_top_left_corner() -> None:
    doc = _doc()
    a, b = _pair(doc)
    placed = ops.add_diagram_container(doc, members=[a, b], label="Core")
    text = doc.resolve(placed.ref.id)[1]
    assert text.text == "Core"
    assert float(str(text.get("x"))) == pytest.approx(24.0 + 16.0, abs=TOL)
    assert float(str(text.get("y"))) == pytest.approx(4.0 + 16.0, abs=TOL)


def test_a_container_label_reads_from_its_corner_and_wears_no_halo() -> None:
    doc = _doc()
    a, b = _pair(doc)
    placed = ops.add_diagram_container(doc, members=[a, b], label="Core")
    style = {str(k): str(v) for k, v in doc.resolve(placed.ref.id)[1].style.items()}
    assert style["text-anchor"] == "start"  # it labels a corner, not a center
    assert style["dominant-baseline"] == "central"
    assert "paint-order" not in style and "stroke" not in style  # a halo is for edge labels


def test_a_container_is_drawn_behind_the_members_it_encloses() -> None:
    doc = _doc()
    a, b = _pair(doc)
    placed = ops.add_diagram_container(doc, members=[a, b])
    parent = doc.resolve(placed.ref.id).getparent()
    assert parent is doc.svg
    assert parent.index(doc.resolve(placed.ref.id)) == 0  # first child = drawn first
    svg = export_svg(doc)
    assert svg.index(placed.ref.id) < svg.index(a) < svg.index(b)


def test_a_container_is_a_sibling_and_reparents_nothing() -> None:
    doc = _doc()
    a, b = _pair(doc)
    placed = ops.add_diagram_container(doc, members=[a, b])
    assert doc.resolve(a).getparent() is doc.svg
    assert doc.resolve(placed.ref.id).getparent() is doc.resolve(a).getparent()


def test_an_explicit_box_is_used_verbatim_and_marked_not_auto() -> None:
    doc = _doc()
    a, b = _pair(doc)
    placed = ops.add_diagram_container(doc, members=[a, b], x=0, y=0, width=500, height=300)
    spec = read_container_spec(doc.resolve(placed.ref.id))
    assert (placed.x, placed.y, placed.w, placed.h) == (0.0, 0.0, 500.0, 300.0)
    assert placed.auto is False
    assert spec is not None and spec.auto is False


def test_a_partial_box_is_derived_in_full() -> None:
    doc = _doc()
    a, b = _pair(doc)
    placed = ops.add_diagram_container(doc, members=[a, b], x=0, width=500)
    assert placed.auto is True
    assert (placed.x, placed.w) == (24.0, 272.0)  # the half-given box is not half-honoured


def test_a_memberless_container_needs_a_box_of_its_own() -> None:
    doc = _doc()
    with pytest.raises(InvalidArgument, match="nothing to fit a box around"):
        ops.add_diagram_container(doc, members=[])
    zone = ops.add_diagram_container(doc, members=[], x=10, y=10, width=100, height=80)
    assert (zone.x, zone.w, zone.auto) == (10.0, 100.0, False)


# --- 2. membership -----------------------------------------------------------


def test_a_member_that_does_not_resolve_is_rejected_at_creation() -> None:
    doc = _doc()
    a, _b = _pair(doc)
    with pytest.raises(InvalidArgument, match="does not resolve"):
        ops.add_diagram_container(doc, members=[a, "no-such-node"])


def test_a_member_that_encloses_the_container_is_rejected() -> None:
    doc = _doc()
    group = ops.create_group(doc, name="col")
    inside = ops.add_diagram_node(doc, kind="service", label="A", parent=group.id)
    with pytest.raises(InvalidArgument, match="encloses the container"):
        ops.add_diagram_container(doc, members=[group.id, inside.ref.id], parent=group.id)


def test_replacing_and_adjusting_the_membership_cannot_be_combined() -> None:
    doc = _doc()
    a, b = _pair(doc)
    placed = ops.add_diagram_container(doc, members=[a])
    with pytest.raises(InvalidArgument, match="not both in one call"):
        ops.edit_diagram_container(doc, placed.ref.id, members=[a], add_members=[b])


def test_adding_a_member_refits_the_box_immediately() -> None:
    doc = _doc()
    a, b = _pair(doc)
    placed = ops.add_diagram_container(doc, members=[a])
    assert _rect(doc, placed.ref.id) == (24.0, 24.0, 132.0, 92.0)  # A alone
    result = ops.edit_diagram_container(doc, placed.ref.id, add_members=[b])
    assert result.refit is True
    assert result.members == [a, b]
    assert _rect(doc, placed.ref.id) == (24.0, 24.0, 272.0, 132.0)  # A and B


def test_removing_a_member_refits_the_box_immediately() -> None:
    doc = _doc()
    a, b = _pair(doc)
    placed = ops.add_diagram_container(doc, members=[a, b])
    result = ops.edit_diagram_container(doc, placed.ref.id, remove_members=[b])
    assert result.members == [a]
    assert _rect(doc, placed.ref.id) == (24.0, 24.0, 132.0, 92.0)


def test_replacing_the_membership_refits_the_box() -> None:
    doc = _doc()
    a, b = _pair(doc)
    placed = ops.add_diagram_container(doc, members=[a, b])
    ops.edit_diagram_container(doc, placed.ref.id, members=[b])
    assert _rect(doc, placed.ref.id) == (184.0, 84.0, 112.0, 72.0)  # B alone


def test_an_auto_container_may_not_be_emptied() -> None:
    doc = _doc()
    a, _b = _pair(doc)
    placed = ops.add_diagram_container(doc, members=[a])
    with pytest.raises(InvalidArgument, match="cannot be emptied"):
        ops.edit_diagram_container(doc, placed.ref.id, members=[])


def test_a_label_edit_refits_an_auto_container_and_moves_the_text() -> None:
    doc = _doc()
    a, b = _pair(doc)
    placed = ops.add_diagram_container(doc, members=[a, b])
    ops.edit_diagram_container(doc, placed.ref.id, label="Core")
    assert _rect(doc, placed.ref.id) == (24.0, 4.0, 272.0, 152.0)  # headroom appeared
    assert doc.resolve(placed.ref.id)[1].text == "Core"


def test_editing_something_that_is_not_a_container_is_rejected() -> None:
    doc = _doc()
    rect = ops.add_rect(doc, x=0, y=0, width=10, height=10)
    with pytest.raises(InvalidArgument, match="not a diagram container"):
        ops.edit_diagram_container(doc, rect.id, label="nope")


# --- 3. theming and inspection ----------------------------------------------


def test_the_container_kinds_attach_through_the_bundled_fallback() -> None:
    doc = _doc()
    a, b = _pair(doc)
    for kind, expected in (("cluster", "default-cluster"), ("zone", "default-zone")):
        placed = ops.add_diagram_container(doc, members=[a, b], kind=kind)
        assert _classes(doc, placed.ref.id) == [expected]
    default = ops.add_diagram_container(doc, members=[a, b])
    assert _classes(doc, default.ref.id) == ["default-cluster"]  # cluster is the default kind


def test_a_container_is_stamped_as_a_container_and_not_as_a_primitive() -> None:
    doc = _doc()
    a, b = _pair(doc)
    placed = ops.add_diagram_container(doc, members=[a, b])
    group = doc.resolve(placed.ref.id)
    assert str(group.get("data-category")) == "container"
    assert group.get("data-prim") is None  # a container is only ever a box


def test_changing_the_kind_swaps_the_role_class() -> None:
    doc = _doc()
    a, b = _pair(doc)
    placed = ops.add_diagram_container(doc, members=[a, b], kind="cluster")
    ops.edit_diagram_container(doc, placed.ref.id, kind="swimlane")
    spec = read_container_spec(doc.resolve(placed.ref.id))
    assert _classes(doc, placed.ref.id) == ["default-swimlane"]
    assert spec is not None and spec.kind == "swimlane"


def test_a_container_spec_round_trips_through_get_params() -> None:
    doc = _doc()
    a, b = _pair(doc)
    placed = ops.add_diagram_container(doc, members=[a, b], kind="zone", label="Edge")
    result = get_params(doc, placed.ref.id)
    assert result["kind"] == "diagram_container"
    assert result["parametric"] is True
    assert result["params"] == {
        "kind": "zone",
        "label": "Edge",
        "members": [a, b],
        "themed": True,
        "auto": True,
    }


# --- 4. reflow ---------------------------------------------------------------


def test_reflow_refits_a_container_after_a_member_moves() -> None:
    doc = _doc()
    a, b = _pair(doc)
    placed = ops.add_diagram_container(doc, members=[a, b])
    ops.translate_node(doc, b, 100, 60)
    result = ops.reflow(doc)
    assert result.containers_refit == 1
    assert result.skipped_containers == []
    assert _rect(doc, placed.ref.id) == (24.0, 24.0, 372.0, 192.0)


def test_reflow_never_touches_an_explicitly_boxed_container() -> None:
    doc = _doc()
    a, b = _pair(doc)
    placed = ops.add_diagram_container(doc, members=[a, b], x=0, y=0, width=500, height=300)
    ops.translate_node(doc, b, 100, 60)
    result = ops.reflow(doc)
    assert result.containers_refit == 0
    assert _rect(doc, placed.ref.id) == (0.0, 0.0, 500.0, 300.0)


def test_reflow_can_be_told_to_leave_containers_alone() -> None:
    doc = _doc()
    a, b = _pair(doc)
    placed = ops.add_diagram_container(doc, members=[a, b])
    ops.translate_node(doc, b, 100, 60)
    result = ops.reflow(doc, containers=False)
    assert result.containers_refit == 0
    assert _rect(doc, placed.ref.id) == (24.0, 24.0, 272.0, 132.0)


def test_reflow_refits_containers_even_with_edges_switched_off() -> None:
    doc = _doc()
    a, b = _pair(doc)
    placed = ops.add_diagram_container(doc, members=[a, b])
    ops.translate_node(doc, b, 100, 60)
    result = ops.reflow(doc, edges=False)
    assert result.edges_rerouted == 0
    assert result.containers_refit == 1
    assert _rect(doc, placed.ref.id) == (24.0, 24.0, 372.0, 192.0)


def test_reflow_reports_a_container_whose_members_are_all_gone() -> None:
    doc = _doc()
    a, b = _pair(doc)
    placed = ops.add_diagram_container(doc, members=[a, b])
    before = _rect(doc, placed.ref.id)
    ops.delete_node(doc, a)
    ops.delete_node(doc, b)
    result = ops.reflow(doc)
    assert result.containers_refit == 0
    assert result.skipped_containers == [placed.ref.id]
    assert _rect(doc, placed.ref.id) == before  # left exactly as it was


def test_reflow_scope_reaches_a_container_through_its_members() -> None:
    doc = _doc()
    a, b = _pair(doc)
    c = ops.add_diagram_node(doc, kind="service", label="C", x=400, y=250, width=60, height=40)
    held = ops.add_diagram_container(doc, members=[a, b])
    other = ops.add_diagram_container(doc, members=[c.ref.id])
    ops.translate_node(doc, b, 40, 40)
    ops.translate_node(doc, c.ref.id, 40, 40)
    before_other = _rect(doc, other.ref.id)
    result = ops.reflow(doc, scope=[b])
    assert result.containers_refit == 1
    assert _rect(doc, held.ref.id) == (24.0, 24.0, 312.0, 172.0)
    assert _rect(doc, other.ref.id) == before_other  # out of scope, untouched


# --- 5. render ---------------------------------------------------------------


def test_a_container_paints_a_wash_behind_its_members() -> None:
    renderer = get_renderer()
    if not renderer.available():
        pytest.skip("no resvg backend available")
    from PIL import Image

    doc = _doc()
    a = ops.add_diagram_node(doc, kind="service", label="A", x=60, y=60, width=100, height=60)
    b = ops.add_diagram_node(doc, kind="service", label="B", x=340, y=60, width=100, height=60)
    ops.add_diagram_container(doc, members=[a.ref.id, b.ref.id], kind="cluster")

    result = renderer.render(RenderRequest(svg=export_svg(doc)))
    image = Image.open(io.BytesIO(result.png)).convert("RGBA")

    # Between the two nodes: inside the container's box, outside every member.
    wash = image.getpixel((250, 90))
    assert isinstance(wash, tuple)
    assert 0 < wash[3] <= 16  # ink at 3%: present, but barely
    alpha = wash[3] / 255
    over_white = [round(channel * alpha + 255 * (1 - alpha)) for channel in wash[:3]]
    assert all(244 <= channel <= 252 for channel in over_white)  # a hint of grey on the page

    outside = image.getpixel((560, 380))  # beyond the container entirely
    assert isinstance(outside, tuple)
    assert outside[3] == 0

    on_member = image.getpixel((80, 75))
    assert isinstance(on_member, tuple)
    assert on_member[:3] == (236, 238, 241)  # the node still paints over the wash
    assert on_member[3] == 255
