"""Render + feedback backend tests.

The PNG-dimension and downscale tests run with no external binary. The resvg smoke test is
skipped when the binary is absent, so the suite stays green in CI without resvg installed.
"""

from __future__ import annotations

import io

import pytest

from svg_mcp.render import SUPPORTED_FORMATS, export_bytes, get_renderer, rsvg_available
from svg_mcp.render.base import RenderError, RenderRequest
from svg_mcp.render.feedback import downscale_png
from svg_mcp.render.resvg import _png_dimensions
from svg_mcp.render.resvg_py import ResvgPyRenderer, resvg_renderer

SAMPLE_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100">'
    '<rect width="100" height="100" fill="red"/></svg>'
)


def _make_png(width: int, height: int) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGBA", (width, height), (255, 0, 0, 255)).save(buf, format="PNG")
    return buf.getvalue()


def test_png_dimensions_reads_ihdr() -> None:
    assert _png_dimensions(_make_png(40, 30)) == (40, 30)


def test_downscale_is_noop_within_bounds() -> None:
    png = _make_png(100, 80)
    out, width, height = downscale_png(png, max_edge=1024)
    assert (width, height) == (100, 80)
    assert out is png


def test_downscale_caps_long_edge() -> None:
    png = _make_png(2000, 1000)
    out, width, height = downscale_png(png, max_edge=1024)
    assert max(width, height) == 1024
    assert (width, height) == (1024, 512)
    assert out is not png


def test_resvg_smoke() -> None:
    renderer = get_renderer("resvg")
    if not renderer.available():
        pytest.skip("resvg binary not installed")
    result = renderer.render(RenderRequest(svg=SAMPLE_SVG))
    assert result.png[:8] == b"\x89PNG\r\n\x1a\n"
    assert (result.width, result.height) == (100, 100)
    assert result.backend == "resvg"


def test_export_svg_passthrough() -> None:
    assert export_bytes(SAMPLE_SVG, "svg") == SAMPLE_SVG.encode("utf-8")


def test_export_unknown_format_raises() -> None:
    with pytest.raises(RenderError):
        export_bytes(SAMPLE_SVG, "tiff")


def test_supported_formats_cover_raster_and_vector() -> None:
    for fmt in ("png", "jpeg", "webp", "pdf", "ps", "eps", "svg"):
        assert fmt in SUPPORTED_FORMATS


def test_resvg_py_in_process_render() -> None:
    renderer = ResvgPyRenderer()
    if not renderer.available():
        pytest.skip("resvg-py not installed")
    result = renderer.render(RenderRequest(svg=SAMPLE_SVG))
    assert result.png[:8] == b"\x89PNG\r\n\x1a\n"
    assert (result.width, result.height) == (100, 100)
    assert result.backend == "resvg-py"


def test_resvg_renderer_factory_works_without_cli() -> None:
    # The default factory renders even when the CLI binary is absent (in-process fallback),
    # so a bare install is self-contained.
    renderer = resvg_renderer()
    if not renderer.available():
        pytest.skip("no resvg backend available (neither CLI nor resvg-py)")
    result = renderer.render(RenderRequest(svg=SAMPLE_SVG))
    assert result.png[:8] == b"\x89PNG\r\n\x1a\n"
    assert (result.width, result.height) == (100, 100)


def test_export_raster_formats() -> None:
    if not resvg_renderer().available():
        pytest.skip("no resvg backend available")
    png = export_bytes(SAMPLE_SVG, "png")
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    jpeg = export_bytes(SAMPLE_SVG, "jpeg")
    assert jpeg[:3] == b"\xff\xd8\xff"  # JPEG SOI
    webp = export_bytes(SAMPLE_SVG, "webp")
    assert webp[:4] == b"RIFF" and webp[8:12] == b"WEBP"


def test_export_pdf_when_rsvg_available() -> None:
    if not rsvg_available():
        pytest.skip("rsvg-convert not installed")
    pdf = export_bytes(SAMPLE_SVG, "pdf")
    assert pdf[:5] == b"%PDF-"
