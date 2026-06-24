"""Pydantic schema for the raw filter graph (``define_filter``)."""

from __future__ import annotations

from pydantic import BaseModel, Field


class FilterPrimitive(BaseModel):
    """One filter-primitive node (e.g. ``feGaussianBlur``) with attributes and nested children.

    ``children`` carries nested primitives like ``feMergeNode`` (inside ``feMerge``) or
    ``feFuncR`` (inside ``feComponentTransfer``). Wire primitives with ``in``/``in2``/``result``
    attributes.
    """

    tag: str
    attrs: dict[str, str] = Field(default_factory=dict)
    children: list[FilterPrimitive] = Field(default_factory=list)


FilterPrimitive.model_rebuild()
