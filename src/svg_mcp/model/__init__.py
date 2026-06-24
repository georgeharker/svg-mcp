"""The canonical document model: an inkex-backed DOM behind a stable facade."""

from __future__ import annotations

from .document import Document
from .errors import DocumentNotFound, InvalidArgument, NodeNotFound, SvgMcpError
from .handles import NodeRef

__all__ = [
    "Document",
    "NodeRef",
    "SvgMcpError",
    "DocumentNotFound",
    "NodeNotFound",
    "InvalidArgument",
]
