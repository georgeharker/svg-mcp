"""Typed errors for the model/ops/query layers."""

from __future__ import annotations


class SvgMcpError(Exception):
    """Base class for all svg-mcp domain errors."""


class DocumentNotFound(SvgMcpError):
    """No document exists for the given id."""


class NodeNotFound(SvgMcpError):
    """No node matched the given id or name within a document."""


class AmbiguousReference(SvgMcpError):
    """A friendly name matched more than one node; qualify by hierarchy or use the id."""


class InvalidArgument(SvgMcpError):
    """A tool/op argument was structurally valid but semantically wrong."""


class NoSession(SvgMcpError):
    """A request that touches session state supplied no session id.

    Every session-state tool requires the `session_id` minted by `create_session`, on every
    protocol era (the schema rejects a missing one; this guards any path that skipped it).
    The error's guidance is what an agent needs to recover on its own.
    """


class ThemeError(SvgMcpError):
    """A theme could not be discovered, parsed, linted, or materialized."""
