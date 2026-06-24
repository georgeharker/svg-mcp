"""Render + feedback backend tests.

The PNG-dimension and downscale tests run with no external binary. The resvg smoke test is
skipped when the binary is absent, so the suite stays green in CI without resvg installed.
"""

from __future__ import annotations

import io

import pytest

from svg_mcp.render import get_renderer
from svg_mcp.render.base import RenderRequest
from svg_mcp.render.feedback import downscale_png
from svg_mcp.render.resvg import _png_dimensions

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
