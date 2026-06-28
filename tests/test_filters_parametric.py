"""Parametric effect stack: serialized list, describe, edit-by-index, stacking, removal."""

from __future__ import annotations

import pytest

from svg_mcp import ops
from svg_mcp.model.document import Document
from svg_mcp.model.errors import InvalidArgument
from svg_mcp.ops.resources import EffectInfo, FxParams
from svg_mcp.session import DocumentStore


def _rect() -> tuple[Document, str]:
    _, doc = DocumentStore().create(100, 100)
    r = ops.add_rect(doc, x=10, y=10, width=40, height=40, name="r")
    return doc, r.id


def _effects(doc: Document, target: str) -> list[EffectInfo]:
    info = ops.get_filter(doc, target)
    assert info is not None
    fx = info["effects"]
    assert isinstance(fx, list)
    return [e for e in fx if isinstance(e, dict)]


def _params(effect: EffectInfo) -> FxParams:
    params = effect["params"]
    assert isinstance(params, dict)
    return params


def _filter_id(doc: Document, target: str) -> object:
    info = ops.get_filter(doc, target)
    assert info is not None
    return info["id"]


def test_get_filter_describes_stack_with_params() -> None:
    doc, _rid = _rect()
    ops.apply_drop_shadow(doc, "r", dx=3, dy=4, blur=5, color="#123456", opacity=0.7)
    fx = _effects(doc, "r")
    assert len(fx) == 1 and fx[0]["index"] == 0 and fx[0]["kind"] == "drop_shadow"
    assert _params(fx[0])["dx"] == 3 and _params(fx[0])["color"] == "#123456"


def test_get_filter_none_without_filter() -> None:
    doc, _rid = _rect()
    assert ops.get_filter(doc, "r") is None


def test_effects_stack_by_default() -> None:
    doc, _rid = _rect()
    ops.apply_drop_shadow(doc, "r")  # below
    ops.apply_gloss(doc, "r")  # above
    ops.apply_inner_shadow(doc, "r")  # above
    assert [e["kind"] for e in _effects(doc, "r")] == ["drop_shadow", "gloss", "inner_shadow"]


def test_replace_starts_a_fresh_stack() -> None:
    doc, _rid = _rect()
    ops.apply_drop_shadow(doc, "r")
    ops.apply_outer_glow(doc, "r", replace=True)
    assert [e["kind"] for e in _effects(doc, "r")] == ["outer_glow"]


def test_edit_filter_changes_one_effect_by_index() -> None:
    doc, _rid = _rect()
    ops.apply_drop_shadow(doc, "r")
    ops.apply_outer_glow(doc, "r", size=4, color="#ffffff", opacity=1.0)
    fid = _filter_id(doc, "r")
    ops.edit_filter(doc, "r", {"size": 9, "color": "#1e3a8a"}, index=1)
    fx = _effects(doc, "r")
    assert _filter_id(doc, "r") == fid  # same filter, rebuilt in place
    assert _params(fx[1])["size"] == 9 and _params(fx[1])["color"] == "#1e3a8a"
    assert _params(fx[1])["opacity"] == 1.0  # untouched param kept
    assert fx[0]["kind"] == "drop_shadow"  # other effect unaffected


def test_edit_filter_rejects_unknown_param_and_bad_index() -> None:
    doc, _rid = _rect()
    ops.apply_blur(doc, "r", std_deviation=2)
    with pytest.raises(InvalidArgument):
        ops.edit_filter(doc, "r", {"dx": 5})  # blur has no dx
    with pytest.raises(InvalidArgument):
        ops.edit_filter(doc, "r", {"std_deviation": 4}, index=3)  # out of range


def test_remove_effect_and_clear() -> None:
    doc, _rid = _rect()
    ops.apply_drop_shadow(doc, "r")
    ops.apply_outer_glow(doc, "r")
    ops.remove_effect(doc, "r", 0)
    assert [e["kind"] for e in _effects(doc, "r")] == ["outer_glow"]
    ops.remove_effect(doc, "r", 0)  # last one -> filter detached
    assert ops.get_filter(doc, "r") is None
    ops.apply_gloss(doc, "r")
    ops.clear_effects(doc, "r")
    assert ops.get_filter(doc, "r") is None


def test_all_composites_apply_and_round_trip() -> None:
    for kind in (
        "inner_shadow",
        "outer_glow",
        "inner_glow",
        "outline",
        "bevel",
        "gloss",
        "grain",
    ):
        doc, _rid = _rect()
        getattr(ops, f"apply_{kind}")(doc, "r")
        assert _effects(doc, "r")[0]["kind"] == kind, kind


def test_custom_define_filter_is_described_but_not_editable() -> None:
    doc, _rid = _rect()
    prim = ops.FePrimitive(tag="feGaussianBlur", attrs={"stdDeviation": "2"})
    fid = ops.define_filter(doc, primitives=[prim])
    ops.apply_filter(doc, "r", fid)
    info = ops.get_filter(doc, "r")
    assert info is not None and info["kind"] == "custom"
    assert "feGaussianBlur" in (info.get("primitives") or [])
    with pytest.raises(InvalidArgument):
        ops.edit_filter(doc, "r", {"size": 5})
