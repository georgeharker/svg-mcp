"""Typed errors for the model/ops/query layers."""

from __future__ import annotations


class SvgMcpError(Exception):
    """Base class for all svg-mcp domain errors."""


class DocumentNotFound(SvgMcpError):
    """No document exists for the given id."""


class NodeNotFound(SvgMcpError):
    """No node matched the given id or name within a document."""


class InvalidArgument(SvgMcpError):
    """A tool/op argument was structurally valid but semantically wrong."""
