"""text_to_path (pure-Python glyph outlines), font listing, and duplicate suffix."""

from __future__ import annotations

import functools

import pytest

from svg_mcp import ops
from svg_mcp.schemas import ShapeStyle
from svg_mcp.serialize import export_svg
from svg_mcp.session import DocumentStore
from svg_mcp.typeset import list_font_families, measure_text, text_to_path_d

# Pin the font these tests measure against. Glyph metrics differ per family, so an unpinned
# choice makes width assertions machine-dependent: a box that passes on a mac (Helvetica) can
# fail on a Linux dev box that falls back to, say, a CJK face with narrow Latin glyphs. Order
# is macOS faces first, then the families a Linux/CI box reliably has.
_PREFERRED = [
    "Helvetica",
    "Arial",
    "Verdana",
    "DejaVu Sans",
    "Liberation Sans",
    "Nimbus Sans",
    "FreeSans",
    "Noto Sans",
    "Menlo",
    "Courier New",
    "Times New Roman",
]


@functools.lru_cache(maxsize=1)
def _a_text_font() -> str:
    """A Latin-designed family present on this machine, for stable glyph metrics."""
    families = list_font_families()
    for name in _PREFERRED:
        if name in families:
            return name
    pytest.skip(f"none of the pinned test fonts are installed: {', '.join(_PREFERRED)}")


def test_list_font_families() -> None:
    families = list_font_families()
    assert isinstance(families, list) and families
    assert all(isinstance(name, str) and name for name in families)


def test_text_to_path_d_produces_outline() -> None:
    d = text_to_path_d("Ag", font_family=_a_text_font(), font_size=40, x=10, y=50)
    assert d.startswith("M") and "Z" in d.upper()


def test_text_to_path_op_replaces_text_with_path() -> None:
    _, doc = DocumentStore().create(240, 80)
    font = _a_text_font()
    ops.add_text(
        doc, x=10, y=50, content="Hi", name="t", style={"font-family": font, "font-size": "40px"}
    )
    ref = ops.text_to_path(doc, "t")
    assert ref.tag == "path" and ref.name == "t"
    svg = export_svg(doc)
    assert "<text" not in svg and "<path" in svg


def test_text_to_path_flattens_tspans_with_per_run_fill() -> None:
    _, doc = DocumentStore().create(300, 100)
    font = _a_text_font()
    ops.add_text(
        doc,
        x=20,
        y=60,
        content="A",
        name="t",
        style={"font-family": font, "font-size": "40px", "fill": "#111111"},
    )
    ops.add_text_run(doc, parent="t", text="B", style={"fill": "#ff0000"})
    ref = ops.text_to_path(doc, "t")
    assert ref.tag == "g" and ref.name == "t"  # multi-run -> group of per-run paths
    svg = export_svg(doc)
    assert "<text" not in svg and svg.count("<path") == 2
    assert "#ff0000" in svg and "#111111" in svg


def test_text_to_path_follows_textpath() -> None:
    _, doc = DocumentStore().create(320, 200)
    ops.add_path(doc, d="M20,150 C80,40 240,40 300,150", name="curve")
    font = _a_text_font()
    text = ops.add_text_on_path(
        doc,
        path="curve",
        content="curved",
        style={"font-family": font, "font-size": "28px", "fill": "#1e3a8a"},
    )
    ref = ops.text_to_path(doc, text.id)
    assert ref.tag == "path"
    svg = export_svg(doc)
    assert "<text" not in svg and "textPath" not in svg
    # Glyphs span along the curve: the outline is much wider than one glyph, but narrower than
    # the flat advance (the arc bends the run back in). Measured against the font's own advance
    # rather than a fixed pixel count, which would only hold for one family.
    advance, _ = measure_text("curved", font_family=font, font_size=28)
    box = ops.path_bbox(doc, ref.id)
    assert box is not None
    assert advance / 2 < box["width"] < advance


def test_clear_clip_prunes_orphan_def() -> None:
    _, doc = DocumentStore().create(120, 120)
    rect = ops.add_rect(doc, x=0, y=0, width=60, height=60)
    shape = ops.add_circle(doc, cx=30, cy=30, r=20)
    clip = ops.define_clip(doc, content=[shape.id])
    ops.apply_clip(doc, rect.id, clip)
    assert "clipPath" in export_svg(doc)
    ops.clear_clip(doc, rect.id)
    assert "clipPath" not in export_svg(doc)  # orphaned def removed


def test_measure_text_scales_with_content_and_size() -> None:
    font = _a_text_font()
    w1, h1 = measure_text("AAAA", font_family=font, font_size=40)
    w2, h2 = measure_text("AA", font_family=font, font_size=40)
    assert w1 > w2 > 0  # more glyphs -> wider
    assert h1 == h2 > 0  # line height is independent of content
    w_big, h_big = measure_text("AA", font_family=font, font_size=80)
    assert w_big > w2 and h_big > h2  # both scale with font size


def test_style_dict_emits_new_typography_props() -> None:
    style = ShapeStyle(
        word_spacing="2px",
        text_decoration="underline",
        dominant_baseline="middle",
        paint_order="stroke fill",
    )
    out = style.to_style_dict()
    assert out["word-spacing"] == "2px"
    assert out["text-decoration"] == "underline"
    assert out["dominant-baseline"] == "middle"
    assert out["paint-order"] == "stroke fill"


def test_duplicate_adds_suffix() -> None:
    _, doc = DocumentStore().create(100, 100)
    ops.add_rect(doc, x=0, y=0, width=10, height=10, name="box")
    copy = ops.duplicate(doc, "box")
    assert copy.name == "box-copy"
    assert copy.id != doc.resolve("box").get_id()
