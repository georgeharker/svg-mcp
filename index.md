# svg-mcp

An MCP server that lets an AI agent author **SVG vector graphics** as a structured,
hierarchical document — build it up with tool calls, then *see* it by rendering to an image and
iterate in a tight render-and-adjust loop. It is built around an inkex-backed document model with
parametric primitives (squircles, rounded polygons, superellipses, pills), boolean operations,
path offsetting, variable-width strokes, gradients, filters, and a self-contained in-process
renderer.

Start with the [overview and quick start](README.md), see it in action in the
[Gallery](docs/gallery.md), or copy a recipe from the [Cookbook](docs/cookbook.md).

| I want to… | Document |
|------------|----------|
| Get started & install | [Overview (README)](README.md) |
| See what it can draw | [Gallery](docs/gallery.md) |
| Copy a working example | [Cookbook](docs/cookbook.md) |
| Look up a tool | [Tool catalogue](INKEX_PRIMITIVES.md) |
| Understand the design | [Design & architecture](DESIGN.md) |
