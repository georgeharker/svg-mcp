"""svg-mcp: a FastMCP server for structured, hierarchical SVG authoring.

The package is layered (see DESIGN.md):

- ``schemas``   pydantic tool/wire contracts (validation only)
- ``model``     the canonical document model (inkex-backed DOM + our facade)
- ``ops``       hierarchy mutations as pure functions over the model
- ``query``     read side: selectors, outline, computed style/transform/bbox
- ``serialize`` model -> SVG string (export and render input)
- ``ingest``    SVG string -> model (safe parse)
- ``render``    Renderer protocol + backends (resvg primary, cairo/inkscape later)
- ``session``   multi-document store
- ``server``    FastMCP app wiring tools to ops/query/render

Only ``render`` and the package skeleton exist today; the model layer lands next.
"""

__version__ = "0.2.2"
