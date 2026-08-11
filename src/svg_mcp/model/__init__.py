"""The canonical document model: an inkex-backed DOM behind a stable facade."""

from __future__ import annotations

from .document import Document, ThemeMeta
from .errors import DocumentNotFound, InvalidArgument, NodeNotFound, SvgMcpError, ThemeError
from .handles import NodeRef

__all__ = [
    "Document",
    "ThemeMeta",
    "NodeRef",
    "SvgMcpError",
    "DocumentNotFound",
    "NodeNotFound",
    "InvalidArgument",
    "ThemeError",
]
