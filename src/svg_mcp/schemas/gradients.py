"""Pydantic schemas for gradient definitions."""

from __future__ import annotations

from pydantic import BaseModel, Field

from .style import Color


class GradientStop(BaseModel):
    """One color stop in a gradient."""

    offset: float = Field(ge=0, le=1)
    color: Color
    opacity: float = Field(default=1.0, ge=0, le=1)
