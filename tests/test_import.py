"""import_svg: load SVG (inline or file) into a registered session document."""

from __future__ import annotations

from pathlib import Path

import pytest

from svg_mcp import ops
from svg_mcp.model.errors import InvalidArgument
from svg_mcp.serialize import export_svg
from svg_mcp.session import DocumentStore

SAMPLE = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="120" height="80">'
    '<rect x="10" y="10" width="40" height="30" fill="#abcdef"/></svg>'
)


def test_load_inline_svg_round_trips() -> None:
    doc = ops.load_svg_document(svg=SAMPLE)
    assert "#abcdef" in export_svg(doc)


def test_load_from_path(tmp_path: Path) -> None:
    f = tmp_path / "in.svg"
    f.write_text(SAMPLE, encoding="utf-8")
    doc = ops.load_svg_document(path=str(f))
    assert "#abcdef" in export_svg(doc)


def test_requires_exactly_one_source() -> None:
    with pytest.raises(InvalidArgument):
        ops.load_svg_document()
    with pytest.raises(InvalidArgument):
        ops.load_svg_document(svg=SAMPLE, path="/x.svg")


def test_register_makes_document_active() -> None:
    store = DocumentStore()
    doc = ops.load_svg_document(svg=SAMPLE)
    doc_id = store.register(doc)
    assert store.active_id == doc_id
    assert store.peek(doc_id) is doc
