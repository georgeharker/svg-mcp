"""cairo + pango render backend — secondary (vector output, in-process).

Planned (DESIGN.md §11.2/§11.3): render the model onto a cairo surface with text shaped by
pango (pangocffi/pangocairocffi). Wins resvg can't give: true vector output (PDF/PS/SVG),
lowest latency, full Python control. Costs we take on here: text-on-path is a manual
glyph-walk along the path arc-length, and SVG filters must be hand-rolled.

Stub for now — declares the backend so it's registered and discoverable. Needs the ``cairo``
optional extra (Homebrew cairo + pango).
"""

from __future__ import annotations

from .base import RenderError, RenderRequest, RenderResult


class CairoRenderer:
    """Render via cairocffi + pangocairocffi. Not yet implemented."""

    name = "cairo"

    def available(self) -> bool:
        try:
            import cairocffi  # noqa: F401
            import pangocairocffi  # noqa: F401
            import pangocffi  # noqa: F401
        except ImportError:
            return False
        return True

    def render(self, request: RenderRequest) -> RenderResult:  # pragma: no cover - stub
        raise RenderError(
            "cairo backend not implemented yet (DESIGN.md §15 phase 4). Use the resvg backend."
        )
