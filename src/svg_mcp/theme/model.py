"""The theme data model: the manifest schema plus the loaded and materialized theme records."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .css import Rule

Category = Literal["shape", "text", "connector", "container", "image"]
"""The five node categories a theme may serve. Closed set — routing depends on it."""


class Serves(BaseModel):
    """What a theme claims to cover: which categories it hooks and which roles it names."""

    model_config = ConfigDict(extra="forbid")

    categories: list[Category] = Field(default_factory=list)
    roles: list[str] = Field(default_factory=list)


class ThemeManifest(BaseModel):
    """``theme.toml`` — coverage, diagram kind → shape mapping, and the variant registry."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    serves: Serves = Field(default_factory=Serves)
    kinds: dict[str, str] = Field(default_factory=dict)
    variants: dict[str, str] = Field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Theme:
    """A theme as loaded from disk: its sources plus everything parsing already established."""

    name: str
    source: Path
    manifest: ThemeManifest
    styles_css: str
    variants: Mapping[str, str] = field(default_factory=dict)
    guidance: str | None = None
    tokens: Mapping[str, str] = field(default_factory=dict)
    variant_tokens: Mapping[str, Mapping[str, str]] = field(default_factory=dict)
    rules: tuple[Rule, ...] = ()
    diagnostics: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class MaterializedTheme:
    """A theme resolved against one variant: ready-to-emit CSS and what went into it."""

    name: str
    variant: str | None
    css: str
    tokens: Mapping[str, str]
    descriptions: Mapping[str, str]
    class_names: frozenset[str] = frozenset()
