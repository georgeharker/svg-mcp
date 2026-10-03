"""In-process resvg renderer via the ``resvg-py`` binding.

Same engine as the ``resvg`` CLI — verified pixel-identical output — but with **no external
binary**: ``resvg-py`` is a core dependency of svg-mcp (installed automatically with the
package), so a bare install renders with no ``brew install`` step. The binding is faster
than the CLI for small renders (no process startup) but slower for large/document-scale
renders; ``resvg_renderer()`` below picks accordingly.

``resvg_renderer()`` is the smart default used everywhere: prefer the CLI when it's on PATH
(parallel rendering wins on big canvases), otherwise fall back to this in-process binding.
"""

from __future__ import annotations

import time

from .base import Renderer, RenderError, RenderRequest, RenderResult
from .resvg import ResvgCliRenderer, _png_dimensions


class ResvgPyRenderer:
    """Render via the in-process ``resvg-py`` binding (no subprocess, no binary)."""

    name = "resvg-py"

    def available(self) -> bool:
        try:
            import resvg_py  # noqa: F401
        except Exception:
            return False
        return True

    def render(self, request: RenderRequest) -> RenderResult:
        try:
            import resvg_py
        except Exception as exc:
            raise RenderError(
                "resvg-py not installed (it is a core svg-mcp dependency — the environment "
                "looks corrupt). Reinstall svg-mcp (pip install svg-mcp), or install the "
                "resvg CLI (`brew install resvg`) as a fallback renderer."
            ) from exc

        start = time.perf_counter()
        # Explicit pixel dims win; otherwise apply scale as zoom (None = natural size). Pass each
        # arg explicitly so the binding's Literal-typed rendering switches keep their defaults.
        zoom = (
            float(request.scale)
            if request.width is None and request.height is None and request.scale != 1.0
            else None
        )
        try:
            png = bytes(
                resvg_py.svg_to_bytes(
                    svg_string=request.svg,
                    width=request.width,
                    height=request.height,
                    zoom=zoom,
                    background=request.background,
                    dpi=float(request.dpi),
                )
            )
        except Exception as exc:
            raise RenderError(f"resvg-py failed: {exc}") from exc

        duration_ms = (time.perf_counter() - start) * 1000.0
        width, height = _png_dimensions(png)
        return RenderResult(
            png=png, width=width, height=height, backend=self.name, duration_ms=duration_ms
        )


def resvg_renderer() -> Renderer:
    """The default resvg renderer: prefer the CLI if present, else the in-process binding."""
    cli = ResvgCliRenderer()
    if cli.available():
        return cli
    return ResvgPyRenderer()
