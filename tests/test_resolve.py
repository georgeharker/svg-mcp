"""Name-collision safety, hierarchy paths, target guards, and relative restacking.

These cover the field-reported footguns: duplicate names silently resolving to the wrong node,
ops "succeeding" on a meaningless target (clipping a gradient), and stacking by index.
"""

from __future__ import annotations

import pytest

from svg_mcp import ops
from svg_mcp.model.errors import AmbiguousReference, InvalidArgument, NodeNotFound
from svg_mcp.serialize import export_svg
from svg_mcp.session import DocumentStore

STOPS = [(0.0, "#fff", 1.0), (1.0, "#000", 1.0)]


def test_duplicate_name_raises_ambiguous() -> None:
    _, doc = DocumentStore().create(100, 100)
    ops.add_rect(doc, x=0, y=0, width=10, height=10, name="sheen")
    ops.add_rect(doc, x=20, y=0, width=10, height=10, name="sheen")
    with pytest.raises(AmbiguousReference) as exc:
        doc.resolve("sheen")
    assert "sheen" in str(exc.value) and "id" in str(exc.value).lower()


def test_name_and_gradient_collision_prefers_the_shape() -> None:
    # A gradient (in <defs>) and a shape sharing a name: prefer the renderable shape, since the
    # def isn't a valid target for visual ops. (Two *shapes* sharing a name stays ambiguous —
    # see test_duplicate_name_raises_ambiguous.)
    _, doc = DocumentStore().create(100, 100)
    ops.define_linear_gradient(doc, x1=0, y1=0, x2=0, y2=1, stops=STOPS, name="sheen")
    rect = ops.add_rect(doc, x=0, y=0, width=10, height=10, name="sheen")
    assert doc.resolve("sheen").get_id() == rect.id


def test_hierarchy_path_disambiguates() -> None:
    _, doc = DocumentStore().create(100, 100)
    grp = ops.create_group(doc, name="bezel")
    inner = ops.add_rect(doc, x=0, y=0, width=10, height=10, name="sheen", parent=grp.id)
    ops.add_rect(doc, x=20, y=0, width=10, height=10, name="sheen")  # second, at root
    assert doc.resolve("path:bezel/sheen").get_id() == inner.id


def test_a_name_containing_a_slash_is_a_name_not_a_query() -> None:
    # The whole point of the prefix grammar: a graph import names its nodes after ids like
    # `src/ops/diagram.py`, and those must stay usable as handles.
    _, doc = DocumentStore().create(100, 100)
    node = ops.add_rect(doc, x=0, y=0, width=10, height=10, name="src/ops/diagram.py")
    assert doc.resolve("src/ops/diagram.py").get_id() == node.id
    assert doc.resolve("name:src/ops/diagram.py").get_id() == node.id


def test_a_bare_slash_that_matches_nothing_teaches_the_path_form() -> None:
    _, doc = DocumentStore().create(100, 100)
    grp = ops.create_group(doc, name="bezel")
    ops.add_rect(doc, x=0, y=0, width=10, height=10, name="sheen", parent=grp.id)
    with pytest.raises(NodeNotFound) as exc:
        doc.resolve("bezel/sheen")  # unambiguous as a query, but it is not a NAME
    assert "path:bezel/sheen" in str(exc.value)


def test_the_prefixes_break_a_name_versus_id_collision() -> None:
    # `resolve` prefers an id, so a node NAMED like another node's id was unreachable. Now it
    # is one prefix away — the second limb of the same in-band ambiguity.
    _, doc = DocumentStore().create(100, 100)
    first = ops.add_rect(doc, x=0, y=0, width=10, height=10)
    second = ops.add_rect(doc, x=20, y=0, width=10, height=10, name=first.id)
    assert doc.resolve(first.id).get_id() == first.id  # bare: the id still wins
    assert doc.resolve(f"id:{first.id}").get_id() == first.id
    assert doc.resolve(f"name:{first.id}").get_id() == second.id


def test_a_prefix_is_stripped_exactly_once_so_any_literal_name_is_reachable() -> None:
    _, doc = DocumentStore().create(100, 100)
    node = ops.add_rect(doc, x=0, y=0, width=10, height=10, name="name:sheen")
    assert doc.resolve("name:name:sheen").get_id() == node.id


def test_an_ambiguous_name_suggests_a_path_that_actually_resolves() -> None:
    _, doc = DocumentStore().create(100, 100)
    grp = ops.create_group(doc, name="bezel")
    ops.add_rect(doc, x=0, y=0, width=10, height=10, name="sheen", parent=grp.id)
    ops.add_rect(doc, x=20, y=0, width=10, height=10, name="sheen")
    with pytest.raises(AmbiguousReference) as exc:
        doc.resolve("sheen")
    suggestion = str(exc.value).split("(e.g. ")[1].split(" (")[0]
    assert suggestion.startswith("path:")
    assert doc.resolve(suggestion) is not None  # the hint is a handle you can paste back


def test_apply_clip_on_gradient_is_rejected() -> None:
    _, doc = DocumentStore().create(100, 100)
    ops.define_linear_gradient(doc, x1=0, y1=0, x2=0, y2=1, stops=STOPS, name="grad")
    shape = ops.add_rect(doc, x=0, y=0, width=20, height=20)
    clip = ops.define_clip(doc, content=[shape.id])
    with pytest.raises(InvalidArgument):
        ops.apply_clip(doc, "grad", clip)


def test_reparent_below_places_beneath_in_paint_order() -> None:
    _, doc = DocumentStore().create(100, 100)
    bezel = ops.add_rect(doc, x=0, y=0, width=50, height=50, name="bezel")
    gloss = ops.add_rect(doc, x=0, y=0, width=50, height=25, name="gloss")  # added last → on top
    ops.reparent(doc, "gloss", None, below="bezel")
    svg = export_svg(doc)
    assert svg.index(gloss.id) < svg.index(bezel.id)  # gloss now earlier → painted beneath


def test_reparent_above_places_on_top() -> None:
    _, doc = DocumentStore().create(100, 100)
    bezel = ops.add_rect(doc, x=0, y=0, width=50, height=50, name="bezel")  # added first → bottom
    gloss = ops.add_rect(doc, x=0, y=0, width=50, height=25, name="gloss")
    ops.reparent(doc, "bezel", None, above="gloss")
    svg = export_svg(doc)
    assert svg.index(gloss.id) < svg.index(bezel.id)  # bezel moved after gloss → on top


# --- review fixes ------------------------------------------------------------


def _candidates(message: str) -> list[str]:
    """The pasteable handle out of each candidate an ambiguity error listed."""
    listed = message.split("Candidates: ")[1]
    return [entry.split(" (")[0] for entry in listed.split("; ")]


def test_ambiguity_hints_resolve_when_the_name_itself_contains_a_slash() -> None:
    # A graph import names its nodes after ids like `src/ops/x.py`. Suggesting
    # `path:<parent>/src/ops/x.py` answers "which one?" with a handle that resolves to neither:
    # the query splits the name into segments no node ever had.
    _, doc = DocumentStore().create(100, 100)
    grp = ops.create_group(doc, name="bezel")
    inner = ops.add_rect(doc, x=0, y=0, width=10, height=10, name="src/ops/x.py", parent=grp.id)
    outer = ops.add_rect(doc, x=20, y=0, width=10, height=10, name="src/ops/x.py")
    with pytest.raises(AmbiguousReference) as exc:
        doc.resolve("src/ops/x.py")
    handles = _candidates(str(exc.value))
    assert len(handles) == 2
    assert {str(doc.resolve(handle).get_id()) for handle in handles} == {inner.id, outer.id}


def test_ambiguity_hints_resolve_for_two_duplicates_at_the_document_root() -> None:
    # Neither has an ancestor to qualify it, and the bare name it used to suggest is the very
    # thing that was just rejected as ambiguous.
    _, doc = DocumentStore().create(100, 100)
    first = ops.add_rect(doc, x=0, y=0, width=10, height=10, name="sheen")
    second = ops.add_rect(doc, x=20, y=0, width=10, height=10, name="sheen")
    with pytest.raises(AmbiguousReference) as exc:
        doc.resolve("sheen")
    handles = _candidates(str(exc.value))
    assert len(handles) == 2
    assert {str(doc.resolve(handle).get_id()) for handle in handles} == {first.id, second.id}
