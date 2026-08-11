"""The theme engine: load a themes directory, lint its CSS, and materialize it for a document."""

from __future__ import annotations

from .css import (
    CATEGORIES,
    Compound,
    Declaration,
    ParsedCss,
    Rule,
    Selector,
    collect_class_names,
    collect_descriptions,
    namespace_rules,
    parse_css,
    parse_variant_css,
    resolve_vars,
    serialize_rules,
    tier_of,
    tier_sort,
)
from .loader import (
    DEFAULT_THEME,
    builtin_themes_path,
    default_search_paths,
    load_theme,
    materialize,
)
from .model import Category, MaterializedTheme, Serves, Theme, ThemeManifest

__all__ = [
    "CATEGORIES",
    "DEFAULT_THEME",
    "Category",
    "Compound",
    "Declaration",
    "MaterializedTheme",
    "ParsedCss",
    "Rule",
    "Selector",
    "Serves",
    "Theme",
    "ThemeManifest",
    "builtin_themes_path",
    "collect_class_names",
    "collect_descriptions",
    "default_search_paths",
    "load_theme",
    "materialize",
    "namespace_rules",
    "parse_css",
    "parse_variant_css",
    "resolve_vars",
    "serialize_rules",
    "tier_of",
    "tier_sort",
]
