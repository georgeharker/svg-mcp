"""Theme discovery, loading, and materialization.

A theme is a directory ``<name>/`` holding ``styles.css`` (required) plus an optional
``theme.toml`` manifest, ``variants/*.css`` overlays, and ``guidance.md``. A bare ``<name>.css``
file in the themes directory is also a theme — styles only.

Search paths are always passed in, so a caller (or a test) decides where themes come from;
:func:`default_search_paths` computes the usual chain: project, then user-global, then the
themes bundled with the package — last, so a user theme of the same name shadows a builtin.
"""

from __future__ import annotations

import tomllib
from collections.abc import Mapping, Sequence
from importlib.resources import files
from pathlib import Path

from pydantic import ValidationError

from ..model.errors import ThemeError
from .css import (
    CATEGORIES,
    Rule,
    collect_class_names,
    collect_descriptions,
    namespace_rules,
    parse_css,
    parse_variant_css,
    resolve_token_table,
    resolve_vars,
    serialize_rules,
    tier_sort,
)
from .model import MaterializedTheme, Theme, ThemeManifest

_STYLES = "styles.css"
_MANIFEST = "theme.toml"
_GUIDANCE = "guidance.md"
_VARIANTS = "variants"
_BUILTIN = "builtin"

DEFAULT_THEME = "default"
"""The bundled theme every document can fall back on — the one name always resolvable."""


def builtin_themes_path() -> Path:
    """The themes shipped inside the package (``theme/builtin``), wherever it is installed."""
    # Resolved through importlib.resources so an installed wheel is found the same way a
    # source checkout is; the package is never zipped, so a real filesystem path comes back.
    return Path(str(files(__package__ or "svg_mcp.theme").joinpath(_BUILTIN)))


def default_search_paths(project: Path | None = None) -> list[Path]:
    """The usual theme roots: the project's ``.svg-mcp/themes``, the user's, then the builtins.

    The bundled themes come last, so a project or user theme sharing a builtin's name shadows it.
    """
    root = project if project is not None else Path.cwd()
    return [
        root / ".svg-mcp" / "themes",
        Path.home() / ".config" / "svg-mcp" / "themes",
        builtin_themes_path(),
    ]


def load_theme(name: str, search_paths: Sequence[Path]) -> Theme:
    """Load the first theme called ``name`` found along ``search_paths`` (earlier paths win)."""
    for base in search_paths:
        directory = base / name
        if directory.is_dir():
            return _load_directory(directory, name)
        bare = base / f"{name}.css"
        if bare.is_file():
            return _load_bare(bare, name)
    searched = ", ".join(str(path) for path in search_paths) or "(no search paths)"
    raise ThemeError(f"no theme named {name!r}; searched {searched}")


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ThemeError(f"cannot read {path}: {exc}") from exc


def _load_bare(path: Path, name: str) -> Theme:
    styles = _read(path)
    parsed = parse_css(styles, origin=str(path))
    return Theme(
        name=name,
        source=path,
        manifest=ThemeManifest(name=name),
        styles_css=styles,
        tokens=parsed.tokens,
        rules=parsed.rules,
    )


def _load_manifest(path: Path) -> ThemeManifest:
    try:
        data = tomllib.loads(_read(path))
    except tomllib.TOMLDecodeError as exc:
        raise ThemeError(f"{path}: invalid TOML: {exc}") from exc
    try:
        return ThemeManifest.model_validate(data)
    except ValidationError as exc:
        raise ThemeError(f"{path}: invalid manifest: {exc}") from exc


def _load_directory(directory: Path, name: str) -> Theme:
    styles_path = directory / _STYLES
    if not styles_path.is_file():
        raise ThemeError(f"theme {name!r} at {directory} has no {_STYLES}")
    manifest_path = directory / _MANIFEST
    has_manifest = manifest_path.is_file()
    manifest = _load_manifest(manifest_path) if has_manifest else ThemeManifest(name=name)
    theme_name = manifest.name or name

    styles = _read(styles_path)
    parsed = parse_css(styles, origin=str(styles_path))

    variants: dict[str, str] = {}
    variant_tokens: dict[str, Mapping[str, str]] = {}
    diagnostics: list[str] = []
    for variant, relative in manifest.variants.items():
        variant_path = directory / relative
        if not variant_path.is_file():
            raise ThemeError(
                f"{manifest_path}: variant {variant!r} points at {relative!r}, which is missing"
            )
        variants[variant] = _read(variant_path)
        variant_tokens[variant] = parse_variant_css(variants[variant], origin=str(variant_path))
    for variant_path in sorted((directory / _VARIANTS).glob("*.css")):
        variant = variant_path.stem
        if variant in variants:
            continue
        if has_manifest:
            diagnostics.append(
                f"variant file {variant_path.name} is not registered in {_MANIFEST} [variants]"
            )
        variants[variant] = _read(variant_path)
        variant_tokens[variant] = parse_variant_css(variants[variant], origin=str(variant_path))

    guidance_path = directory / _GUIDANCE
    guidance = _read(guidance_path) if guidance_path.is_file() else None

    diagnostics.extend(_coverage_diagnostics(manifest, parsed.rules, theme_name))
    return Theme(
        name=theme_name,
        source=directory,
        manifest=manifest,
        styles_css=styles,
        variants=variants,
        guidance=guidance,
        tokens=parsed.tokens,
        variant_tokens=variant_tokens,
        rules=parsed.rules,
        diagnostics=tuple(diagnostics),
    )


def _coverage_diagnostics(
    manifest: ThemeManifest, rules: Sequence[Rule], theme_name: str
) -> list[str]:
    """Advisories where the manifest claims coverage the stylesheet does not actually provide."""
    hooked = {
        selector.compounds[0].classes[0]
        for rule in rules
        for selector in rule.selectors
        if len(selector.compounds) == 1
        and selector.compounds[0].element is None
        and len(selector.compounds[0].classes) == 1
    }
    out: list[str] = []
    for category in manifest.serves.categories:
        if category in CATEGORIES and category not in hooked:
            out.append(
                f"theme {theme_name!r} serves category {category!r} with no .{category} rule"
            )
    for role in manifest.serves.roles:
        if role not in hooked:
            out.append(f"theme {theme_name!r} serves role {role!r} with no .{role} rule")
    return out


def materialize(theme: Theme, variant: str | None = None) -> MaterializedTheme:
    """Resolve a theme (optionally overlaid with a variant) into emittable, namespaced CSS."""
    tokens = dict(theme.tokens)
    if variant is not None:
        if variant not in theme.variant_tokens:
            available = ", ".join(sorted(theme.variant_tokens)) or "(none)"
            raise ThemeError(
                f"theme {theme.name!r} has no variant {variant!r}; available: {available}"
            )
        tokens.update(theme.variant_tokens[variant])
    origin = str(theme.source)
    # The table is brought to a fixpoint FIRST: a token may name another token, and everything
    # downstream (rule substitution, and every consumer that reads ``tokens`` directly) is
    # entitled to assume what ``ServingTheme`` promises — that a token value is a literal.
    tokens = resolve_token_table(tokens, origin=origin)
    rules = resolve_vars(theme.rules, tokens, origin=origin)
    rules = namespace_rules(rules, theme.name)
    rules = tier_sort(rules, theme.name, theme.manifest.serves.roles)
    return MaterializedTheme(
        name=theme.name,
        variant=variant,
        css=serialize_rules(rules),
        tokens=tokens,
        descriptions=collect_descriptions(rules),
        class_names=collect_class_names(rules),
    )
