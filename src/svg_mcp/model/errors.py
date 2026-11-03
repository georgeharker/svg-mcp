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
    """A 2026-era (stateless) request supplied no session identity.

    Stateless-era requests carry no durable wire identity (each request is its own
    connection), so per-session state needs a minted session id — from `create_session`
    passed back per call, or sent ambigently as the `X-Svg-Mcp-Session` header by the
    client/proxy. The error's guidance is what an agent needs to recover on its own.
    """


class ThemeError(SvgMcpError):
    """A theme could not be discovered, parsed, linted, or materialized."""
