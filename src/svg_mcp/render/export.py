"""Multi-format export — faithful raster (resvg) and vector (librsvg / rsvg-convert).

Format → engine:
- ``png``           : resvg (the preview baseline).
- ``jpeg`` / ``webp``: resvg PNG, converted with Pillow (same pixels, different container).
- ``pdf``/``ps``/``eps``: ``rsvg-convert`` (librsvg) — true vector output.
- ``svg``           : the serialized source itself.

cairo (cairosvg) is deliberately NOT a backend here: it silently drops SVG filters (e.g. a
drop shadow renders blank), so it is unfaithful to the document. librsvg renders filters
correctly and adds vector PDF/PS/EPS, so it is the faithful vector engine.
"""

from __future__ import annotations

import shutil
import subprocess
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory

from .base import RenderError, RenderRequest
from .resvg import ResvgCliRenderer

_RASTER = ("png", "jpeg", "jpg", "webp")
_VECTOR = ("pdf", "ps", "eps")
SUPPORTED_FORMATS: tuple[str, ...] = (*_RASTER, *_VECTOR, "svg")


def rsvg_available() -> bool:
    """True if the librsvg ``rsvg-convert`` binary (faithful vector export) is installed."""
    return shutil.which("rsvg-convert") is not None


def export_bytes(svg: str, fmt: str, *, scale: float = 1.0, background: str | None = None) -> bytes:
    """Render ``svg`` to ``fmt`` and return the file bytes. Raises :class:`RenderError`."""
    fmt = fmt.lower()
    if fmt == "svg":
        return svg.encode("utf-8")

    if fmt in _RASTER:
        png = (
            ResvgCliRenderer()
            .render(RenderRequest(svg=svg, scale=scale, background=background))
            .png
        )
        if fmt == "png":
            return png
        from PIL import Image as PILImage

        with PILImage.open(BytesIO(png)) as image:
            out = BytesIO()
            if fmt in ("jpeg", "jpg"):
                image.convert("RGB").save(out, format="JPEG", quality=92)
            else:
                image.save(out, format="WEBP", quality=92)
            return out.getvalue()

    if fmt in _VECTOR:
        binary = shutil.which("rsvg-convert")
        if binary is None:
            raise RenderError(
                f"{fmt} export needs the librsvg 'rsvg-convert' binary "
                "(macOS: `brew install librsvg`)"
            )
        with TemporaryDirectory(prefix="svg-mcp-") as tmp:
            in_svg = Path(tmp) / "in.svg"
            out_file = Path(tmp) / f"out.{fmt}"
            in_svg.write_text(svg, encoding="utf-8")
            args = [binary, "-f", fmt, "-o", str(out_file)]
            if background is not None:
                args += ["-b", background]
            if scale != 1.0:
                args += ["--zoom", str(scale)]
            args.append(str(in_svg))
            proc = subprocess.run(args, capture_output=True, check=False, timeout=60)
            if proc.returncode != 0 or not out_file.exists():
                raise RenderError(
                    f"rsvg-convert failed: {proc.stderr.decode('utf-8', 'replace').strip()}"
                )
            return out_file.read_bytes()

    raise RenderError(f"unsupported format {fmt!r}; choices: {', '.join(SUPPORTED_FORMATS)}")
