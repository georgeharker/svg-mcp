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

import contextlib
import os
import shutil
import subprocess
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory

from .base import RenderError, RenderRequest
from .resvg_py import resvg_renderer

_RASTER = ("png", "jpeg", "jpg", "webp")
_VECTOR = ("pdf", "ps", "eps")
SUPPORTED_FORMATS: tuple[str, ...] = (*_RASTER, *_VECTOR, "svg")


def rsvg_available() -> bool:
    """True if the librsvg ``rsvg-convert`` binary (faithful vector export) is installed."""
    return shutil.which("rsvg-convert") is not None


def write_render_file(path: Path, data: bytes) -> Path:
    """Write render output atomically and durably; returns the resolved absolute path.

    Writes a same-directory temp file, flushes and fsyncs it, atomically renames it over the
    target, and fsyncs the parent directory entry before returning — so a reader (or a watcher
    pipeline) never observes a torn or half-old file, and the call returning means the bytes
    are durably on disk.
    """
    target = path.resolve()
    tmp = target.with_name(f".{target.name}.tmp-{os.getpid()}-{os.urandom(4).hex()}")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(tmp, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)  # atomic: readers see the old file or the new one, never a stub
        dir_fd = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)  # the rename itself, so a crash cannot lose the new name
        finally:
            os.close(dir_fd)
    except OSError as exc:
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)
        reason = (
            "directory does not exist"
            if not target.parent.exists()
            else f"the parent {target.parent} exists but the write was refused"
        )
        raise RenderError(
            f"export could not write {target}: {exc}. {reason}. If this path is under a "
            "sandbox or tmpfs, write inside the project directory instead."
        ) from exc
    return target


def export_bytes(svg: str, fmt: str, *, scale: float = 1.0, background: str | None = None) -> bytes:
    """Render ``svg`` to ``fmt`` and return the file bytes. Raises :class:`RenderError`."""
    fmt = fmt.lower()
    if fmt == "svg":
        return svg.encode("utf-8")

    if fmt in _RASTER:
        png = (
            resvg_renderer().render(RenderRequest(svg=svg, scale=scale, background=background)).png
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
