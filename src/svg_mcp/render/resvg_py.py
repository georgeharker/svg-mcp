"""In-process resvg renderer via the optional ``resvg-py`` binding.

Same engine as the ``resvg`` CLI — verified pixel-identical output — but with **no external
binary**: it's a pure pip dependency installed by the ``resvg`` extra (``pip install
'svg-mcp[resvg]'``). That makes the package self-contained for ``uvx`` / pip installs, so a
``brew install resvg`` step isn't required.

``resvg_renderer()`` is the smart default used everywhere: prefer the CLI when it's on PATH
(slightly faster, no per-call binding overhead), otherwise fall back to this in-process binding.
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
                "resvg-py not installed — add the in-process renderer with the 'resvg' extra "
                "(pip install 'svg-mcp[resvg]'), or install the resvg CLI."
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
