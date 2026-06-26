"""Name-collision safety, hierarchy paths, target guards, and relative restacking.

These cover the field-reported footguns: duplicate names silently resolving to the wrong node,
ops "succeeding" on a meaningless target (clipping a gradient), and stacking by index.
"""

from __future__ import annotations

import pytest

from svg_mcp import ops
from svg_mcp.model.errors import AmbiguousReference, InvalidArgument
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
    assert doc.resolve("bezel/sheen").get_id() == inner.id


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
