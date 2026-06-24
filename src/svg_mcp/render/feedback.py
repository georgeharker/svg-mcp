"""Feedback packaging — hand the rasterized image back to the model.

The render bytes are handed back **directly as base64 image content** (FastMCP's ``Image``
base64-encodes them into an MCP image block the model sees inline) — no intermediate
re-encoding by default. ``downscale_png`` remains available as an explicit opt-in for callers
that want to cap image size for token cost.
"""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO

from .base import RenderResult

# FastMCP's Image content type. Import location differs across fastmcp/mcp versions.
try:  # fastmcp >= 2.x
    from fastmcp.utilities.types import Image as MCPImage
except ImportError:  # pragma: no cover - fallback for the bundled mcp SDK
    from mcp.server.fastmcp import Image as MCPImage  # type: ignore[assignment]

__all__ = ["Feedback", "MCPImage", "build_feedback", "downscale_png"]


@dataclass(slots=True)
class Feedback:
    """The model-facing result of a render: a base64 image content block + a text summary."""

    image: MCPImage
    summary: str
    width: int
    height: int


def downscale_png(png: bytes, max_edge: int) -> tuple[bytes, int, int]:
    """Downscale a PNG so its long edge is <= ``max_edge``. Returns (png, width, height).

    No-ops (returns the original bytes) when already within bounds.
    """
    from PIL import Image as PILImage

    with PILImage.open(BytesIO(png)) as img:
        width, height = img.size
        longest = max(width, height)
        if longest <= max_edge or longest == 0:
            return png, width, height
        ratio = max_edge / longest
        new_size = (max(1, round(width * ratio)), max(1, round(height * ratio)))
        resized = img.convert("RGBA").resize(new_size, PILImage.Resampling.LANCZOS)
        out = BytesIO()
        resized.save(out, format="PNG")
        return out.getvalue(), new_size[0], new_size[1]


def build_feedback(
    result: RenderResult,
    *,
    max_edge: int | None = None,
    note: str | None = None,
) -> Feedback:
    """Package a render for the model.

    By default the raw rasterized PNG is handed back directly as base64 image content. Pass
    ``max_edge`` to downscale first (opt-in, e.g. to cap token cost on very large renders).
    """
    png, width, height = result.png, result.width, result.height
    if max_edge is not None:
        png, width, height = downscale_png(png, max_edge)

    parts = [f"{result.width}x{result.height}px via {result.backend} in {result.duration_ms:.0f}ms"]
    if (width, height) != (result.width, result.height):
        parts.append(f"(downscaled to {width}x{height})")
    if note:
        parts.append(note)

    return Feedback(
        image=MCPImage(data=png, format="png"),
        summary=" ".join(parts),
        width=width,
        height=height,
    )
