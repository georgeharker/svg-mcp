"""Parametric filters: serialized intent, get_filter (describe), edit_filter (param-by-param)."""

from __future__ import annotations

import pytest

from svg_mcp import ops
from svg_mcp.model.document import Document
from svg_mcp.model.errors import InvalidArgument
from svg_mcp.session import DocumentStore


def _rect() -> tuple[Document, str]:
    _, doc = DocumentStore().create(100, 100)
    r = ops.add_rect(doc, x=10, y=10, width=40, height=40, name="r")
    return doc, r.id


def test_get_filter_reads_composite_params() -> None:
    doc, rid = _rect()
    ops.apply_drop_shadow(doc, "r", dx=3, dy=4, blur=5, color="#123456", opacity=0.7)
    info = ops.get_filter(doc, "r")
    assert info is not None and info["kind"] == "drop_shadow"
    p = info["params"]
    assert isinstance(p, dict)
    assert p["dx"] == 3 and p["color"] == "#123456" and p["opacity"] == 0.7


def test_get_filter_reads_builtin_params() -> None:
    doc, rid = _rect()
    ops.apply_blur(doc, "r", std_deviation=4)
    info = ops.get_filter(doc, "r")
    assert info is not None and info["kind"] == "blur"
    assert isinstance(info["params"], dict) and info["params"]["std_deviation"] == 4


def test_get_filter_none_without_filter() -> None:
    doc, rid = _rect()
    assert ops.get_filter(doc, "r") is None


def test_edit_filter_changes_params_in_place() -> None:
    doc, rid = _rect()
    ops.apply_outer_glow(doc, "r", blur=4, color="#ffffff", opacity=1.0)
    before = ops.get_filter(doc, "r")
    assert before is not None
    fid_before = before["id"]
    ops.edit_filter(doc, "r", {"color": "#1e3a8a", "blur": 8})  # one-by-one merge
    after = ops.get_filter(doc, "r")
    assert after is not None
    assert after["id"] == fid_before  # same filter, rebuilt in place
    p = after["params"]
    assert isinstance(p, dict)
    assert p["color"] == "#1e3a8a" and p["blur"] == 8 and p["opacity"] == 1.0  # opacity kept


def test_edit_filter_rejects_unknown_param() -> None:
    doc, rid = _rect()
    ops.apply_blur(doc, "r", std_deviation=2)
    with pytest.raises(InvalidArgument):
        ops.edit_filter(doc, "r", {"dx": 5})  # blur has no dx


def test_edit_filter_requires_a_filter() -> None:
    doc, rid = _rect()
    with pytest.raises(InvalidArgument):
        ops.edit_filter(doc, "r", {"blur": 5})


def test_composite_effects_apply_and_render_primitives() -> None:
    # Each new composite attaches a multi-primitive filter that get_filter round-trips.
    for kind in (
        "inner_shadow", "outer_glow", "inner_glow", "outline", "bevel", "gloss", "grain",
    ):
        doc, _rid = _rect()
        getattr(ops, f"apply_{kind}")(doc, "r")
        info = ops.get_filter(doc, "r")
        assert info is not None and info["kind"] == kind, kind


def test_custom_define_filter_is_described_but_not_editable() -> None:
    doc, rid = _rect()
    prim = ops.FePrimitive(tag="feGaussianBlur", attrs={"stdDeviation": "2"})
    fid = ops.define_filter(doc, primitives=[prim])
    ops.apply_filter(doc, "r", fid)
    info = ops.get_filter(doc, "r")
    assert info is not None and info["kind"] == "custom"
    assert "feGaussianBlur" in (info.get("primitives") or [])
    with pytest.raises(InvalidArgument):
        ops.edit_filter(doc, "r", {"blur": 5})
