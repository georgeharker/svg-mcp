"""Read side: selectors, outline, and computed geometry/style queries."""

from __future__ import annotations

from .inspect import (
    convert_units,
    describe_document,
    describe_node,
    get_computed_style,
    get_geometry,
    get_params,
    get_transform,
    list_resources,
)
from .outline import OutlineNode, get_bbox, outline
from .select import extract_image, find, get_subtree

__all__ = [
    "OutlineNode",
    "outline",
    "get_bbox",
    "describe_document",
    "describe_node",
    "list_resources",
    "get_computed_style",
    "get_transform",
    "get_geometry",
    "get_params",
    "convert_units",
    "find",
    "get_subtree",
    "extract_image",
]
