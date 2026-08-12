"""Theme residency ops: which themes a document holds, what each serves, and how nodes hook in.

Themes are a role-routed SET, not a stack. Every resident theme holds a set of ROUTE KEYS — the
node categories (shape, text, connector, container, image) and any role names it declares — and a
key is served by exactly one theme. Loading a theme that wants a held key EVICTS it from the
previous holder, and says so; nothing is silently taken.

New nodes hook into whichever theme serves their category (and their role, when given), which is
the whole point of the routing table: a constructor states its category, the document decides
which stylesheet that means today. A role nothing is routed for at all falls back to the bundled
``default`` theme, materialized on first need — so roles work in a document that loaded nothing,
while a document that names no role is untouched by any of this.

``set_theme_variant`` re-materializes every resident against a token overlay; ``apply_theme`` and
``clear_theme`` work the other way round from residency — they re-dress nodes that ALREADY exist,
over one subtree, and deliberately leave the routing table alone.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from pathlib import Path
from typing import Literal

from inkex import BaseElement
from pydantic import BaseModel, Field

from ..model.document import Document, ThemeMeta
from ..model.errors import InvalidArgument, ThemeError
from ..theme.css import CATEGORIES
from ..theme.loader import DEFAULT_THEME, default_search_paths, materialize
from ..theme.loader import load_theme as _read_theme
from ..theme.model import MaterializedTheme, Theme
from .resources import _class_list, _set_class_list, _sync_stylesheet, apply_styles


class StyleInfo(BaseModel):
    """One style a document offers: its class name, the theme it came from, its doc comment."""

    name: str
    theme: str | None = None
    description: str | None = None


class ThemeResidency(BaseModel):
    """What a load/sync/replace left in place: routes taken, what it displaced, what it offers."""

    theme: str
    variant: str | None = None
    routes_taken: list[str] = Field(default_factory=list)
    evicted: dict[str, str] = Field(default_factory=dict)
    unloaded: list[str] = Field(default_factory=list)
    guidance: str | None = None
    styles: list[StyleInfo] = Field(default_factory=list)


class ThemeRemoval(BaseModel):
    """What unloading a theme freed — and which of its classes nodes are still carrying."""

    theme: str
    routes_freed: list[str] = Field(default_factory=list)
    dangling_classes: list[str] = Field(default_factory=list)


class VariantOutcome(BaseModel):
    """One resident theme's answer to a variant switch — what it actually materialized with."""

    theme: str
    variant_used: str | None = None


class VariantSwitch(BaseModel):
    """The result of switching every resident theme to a variant, and what stayed behind."""

    variant: str | None = None
    themes: list[VariantOutcome] = Field(default_factory=list)
    pinned_nodes: int = 0


class ThemeScopeChange(BaseModel):
    """What a scoped apply/clear moved: the theme involved and how many class refs changed."""

    theme: str | None = None
    variant: str | None = None
    nodes_touched: int = 0
    classes_added: int = 0
    classes_removed: int = 0


# --- residency ---------------------------------------------------------------


def _install(
    doc: Document,
    theme: Theme,
    result: MaterializedTheme,
    *,
    search_paths: Sequence[Path],
    routes: Sequence[str],
) -> None:
    """Put a materialized theme's block and meta into the document, replacing any prior one."""
    doc.theme_css[result.name] = result.css
    doc.theme_meta[result.name] = ThemeMeta(
        variant=result.variant,
        tokens=dict(result.tokens),
        descriptions=dict(result.descriptions),
        source=theme.source,
        search_paths=list(search_paths),
        routes=list(routes),
        class_names=result.class_names,
        kinds=dict(theme.manifest.kinds),
    )
    _sync_stylesheet(doc)


def materialize_into(
    doc: Document,
    theme: Theme,
    variant: str | None = None,
    *,
    search_paths: Sequence[Path] | None = None,
    routes: Sequence[str] | None = None,
) -> MaterializedTheme:
    """Materialize ``theme`` into ``doc``, replacing any block it previously contributed.

    Re-applying a theme keeps its original position in the stylesheet, so switching variants
    swaps the block in place rather than stacking a second copy after it.
    """
    result = materialize(theme, variant)
    _install(doc, theme, result, search_paths=search_paths or (), routes=routes or ())
    return result


def _styles_of(result: MaterializedTheme, theme: str | None = None) -> list[StyleInfo]:
    names = sorted(set(result.class_names) | set(result.descriptions))
    return [
        StyleInfo(name=name, theme=theme, description=result.descriptions.get(name))
        for name in names
    ]


def _default_routes(theme: Theme) -> list[str]:
    """What a theme serves when the caller doesn't say: its manifest's categories, then its roles.

    A bare ``<name>.css`` theme has no manifest, hence no routes — it materializes but hooks
    nothing until the caller names the roles it should serve.
    """
    serves = theme.manifest.serves
    return list(dict.fromkeys([*serves.categories, *serves.roles]))


def _check_serviceable(theme: Theme, routes: Iterable[str], class_names: frozenset[str]) -> None:
    """Reject routing a ROLE to a theme that neither declares nor styles it (a typo, not a job)."""
    for key in routes:
        if key in CATEGORIES:
            continue
        if key in theme.manifest.serves.roles or f"{theme.name}-{key}" in class_names:
            continue
        offered = sorted(set(theme.manifest.serves.roles) | {*CATEGORIES})
        raise InvalidArgument(
            f"theme {theme.name!r} cannot serve role {key!r}: it neither declares it under "
            f"serves.roles nor defines a .{theme.name}-{key} style. It can serve: {offered}"
        )


def _take_routes(
    doc: Document, name: str, routes: Sequence[str], *, expect_free: bool
) -> dict[str, str]:
    """Point ``routes`` at ``name``, evicting whoever held them; returns route -> old holder."""
    held = {
        key: doc.theme_routing[key]
        for key in routes
        if doc.theme_routing.get(key) not in (None, name)
    }
    if held and expect_free:
        detail = ", ".join(f"{key} (held by {holder})" for key, holder in sorted(held.items()))
        raise InvalidArgument(
            f"theme {name!r} wants route(s) another theme already serves: {detail}; "
            "load without expect_free to take them over, or narrow `roles`"
        )
    for key, holder in held.items():
        meta = doc.theme_meta.get(holder)
        if meta is not None and key in meta.routes:
            meta.routes.remove(key)
    for key in [k for k, v in doc.theme_routing.items() if v == name and k not in routes]:
        del doc.theme_routing[key]  # routes this theme held but no longer claims
    for key in routes:
        doc.theme_routing[key] = name
    return held


def load_theme(
    doc: Document,
    name: str,
    *,
    roles: list[str] | None = None,
    variant: str | None = None,
    expect_free: bool = False,
    search_paths: list[Path] | None = None,
) -> ThemeResidency:
    """Load a theme into the document and route to it the keys it serves.

    Without ``roles`` the theme takes what its manifest claims (categories then roles); with them
    it takes exactly that subset. A key another theme holds is taken over (reported in
    ``evicted``) unless ``expect_free``, which turns the contention into an error instead.
    """
    paths = list(search_paths) if search_paths is not None else default_search_paths()
    theme = _read_theme(name, paths)
    result = materialize(theme, variant)
    routes = list(dict.fromkeys(roles)) if roles is not None else _default_routes(theme)
    _check_serviceable(theme, routes, result.class_names)
    evicted = _take_routes(doc, theme.name, routes, expect_free=expect_free)
    _install(doc, theme, result, search_paths=paths, routes=routes)
    return ThemeResidency(
        theme=theme.name,
        variant=result.variant,
        routes_taken=routes,
        evicted=evicted,
        guidance=theme.guidance,
        styles=_styles_of(result),
    )


def _resident(doc: Document, name: str) -> ThemeMeta:
    meta = doc.theme_meta.get(name)
    if meta is None or name not in doc.theme_css:
        resident = sorted(doc.theme_css) or ["(none)"]
        raise InvalidArgument(f"theme {name!r} is not loaded; resident themes: {resident}")
    return meta


def _dangling_classes(doc: Document, name: str) -> list[str]:
    """Classes of the departing theme that nodes still carry (their rules are gone now)."""
    prefix = f"{name}-"
    return sorted(
        {
            cls
            for node in doc.svg.iter()
            for cls in str(node.get("class") or "").split()
            if cls.startswith(prefix)
        }
    )


def unload_theme(doc: Document, name: str) -> ThemeRemoval:
    """Remove a theme's rules, meta, and routes; reports classes nodes are still linked to.

    Unloading does NOT strip class attributes — a listed ``dangling_class`` is a node still asking
    for a rule that no longer exists. Remove them with ``remove_styles``, or load a theme that
    defines them again.
    """
    _resident(doc, name)
    freed = sorted(key for key, holder in doc.theme_routing.items() if holder == name)
    for key in freed:
        del doc.theme_routing[key]
    dangling = _dangling_classes(doc, name)
    del doc.theme_css[name]
    del doc.theme_meta[name]
    _sync_stylesheet(doc)
    return ThemeRemoval(theme=name, routes_freed=freed, dangling_classes=dangling)


def _reread(meta: ThemeMeta, name: str) -> tuple[Theme, list[Path]]:
    """Re-read a resident theme from where it came, its own directory taking precedence."""
    paths = list(dict.fromkeys([meta.source.parent, *meta.search_paths]))
    return _read_theme(name, paths), paths


def sync_theme(doc: Document, name: str) -> ThemeResidency:
    """Re-read a resident theme from disk and re-materialize it, keeping its variant and routes.

    The way to pick up an edit to a theme's CSS mid-session. Errors if the theme is no longer
    where it was loaded from.
    """
    meta = _resident(doc, name)
    variant, routes = meta.variant, list(meta.routes)
    theme, paths = _reread(meta, name)
    result = materialize(theme, variant)
    _install(doc, theme, result, search_paths=paths, routes=routes)
    return ThemeResidency(
        theme=theme.name,
        variant=result.variant,
        routes_taken=routes,
        guidance=theme.guidance,
        styles=_styles_of(result),
    )


def replace_theme(
    doc: Document,
    name: str,
    *,
    roles: list[str] | None = None,
    variant: str | None = None,
    expect_free: bool = False,
    search_paths: list[Path] | None = None,
) -> ThemeResidency:
    """Unload EVERY resident theme, then load this one — swap the whole look in one call."""
    unloaded = list(doc.theme_css)
    for resident in unloaded:
        unload_theme(doc, resident)
    result = load_theme(
        doc,
        name,
        roles=roles,
        variant=variant,
        expect_free=expect_free,
        search_paths=search_paths,
    )
    result.unloaded = unloaded
    return result


def _pinned_nodes(doc: Document) -> int:
    """Nodes carrying an inline style — the props a variant switch deliberately cannot move."""
    return sum(1 for node in doc.svg.iter() if str(node.get("style") or "").strip())


def set_theme_variant(doc: Document, variant: str | None) -> VariantSwitch:
    """Switch EVERY resident theme to ``variant``, or back to its base tokens with ``None``.

    The variant NAME is the only vocabulary shared across themes: a resident that declares no
    such variant falls back to its base tokens silently rather than blocking the switch, so
    "go dark" means the same thing to a document however many themes dress it. Routing is
    untouched — this re-materializes what is already resident, it does not move any node.
    """
    outcomes: list[VariantOutcome] = []
    for name in list(doc.theme_css):
        meta = doc.theme_meta[name]
        theme, paths = _reread(meta, name)
        wanted = variant if variant is not None and variant in theme.variant_tokens else None
        result = materialize(theme, wanted)
        _install(doc, theme, result, search_paths=paths, routes=list(meta.routes))
        outcomes.append(VariantOutcome(theme=name, variant_used=result.variant))
    return VariantSwitch(variant=variant, themes=outcomes, pinned_nodes=_pinned_nodes(doc))


def list_styles(doc: Document) -> list[StyleInfo]:
    """Every style the document can attach: each theme's classes (in load order), then doc-local."""
    out: list[StyleInfo] = []
    for theme, meta in doc.theme_meta.items():
        out.extend(
            StyleInfo(name=cls, theme=theme, description=meta.descriptions.get(cls))
            for cls in sorted(set(meta.class_names) | set(meta.descriptions))
        )
    out.extend(StyleInfo(name=name) for name in doc.styles)
    return out


# --- auto-applied hooks ------------------------------------------------------


def _hook(doc: Document, theme: str | None, suffix: str) -> str | None:
    """The class ``{theme}-{suffix}`` if that theme's CSS actually defines it, else nothing."""
    if theme is None:
        return None
    meta = doc.theme_meta.get(theme)
    name = f"{theme}-{suffix}"
    return name if meta is not None and name in meta.class_names else None


def _available_styles(doc: Document) -> list[str]:
    defined = {cls for meta in doc.theme_meta.values() for cls in meta.class_names}
    return sorted(defined | set(doc.styles))


def resolve_style_ref(doc: Document, ref: str) -> str:
    """Resolve a caller's style name to a class the document defines.

    Accepts ``@name`` or ``name``, and in order: an exact materialized class, ``{theme}-{name}``
    for a single resident theme, or a doc-local named style. Two themes defining the same short
    name is an error — there is no house rule for which one was meant.
    """
    name = ref.removeprefix("@")
    if any(name in meta.class_names for meta in doc.theme_meta.values()):
        return name
    prefixed = [
        theme for theme, meta in doc.theme_meta.items() if f"{theme}-{name}" in meta.class_names
    ]
    if len(prefixed) == 1:
        return f"{prefixed[0]}-{name}"
    if prefixed:
        candidates = ", ".join(f"{theme}-{name}" for theme in prefixed)
        raise InvalidArgument(
            f"style {name!r} is ambiguous across resident themes ({candidates}); "
            "name the theme-prefixed class you mean"
        )
    if name in doc.styles:
        return name
    raise InvalidArgument(f"no style named {name!r}; available: {_available_styles(doc)}")


_FALLBACK_CATEGORIES: frozenset[str] = frozenset({"chart"})
"""Categories the bundled default may be summoned for, not just roles.

A category fallback is only defensible where the category has no unthemed tradition to protect.
``shape``/``text``/``connector``/``container``/``image`` all have one — a plain ``add_rect`` must
keep rendering as a plain rect — and the bundled default deliberately defines no rule for them.
A chart is the other case: there is no such thing as an unstyled chart, so a themeless document
gets the default's ``.chart`` rather than an unreadable pile of black-on-black marks. Naming the
categories here (rather than asking the theme) keeps every unthemed primitive off the disk-read
path this fallback would otherwise take on every single construction.
"""


def _fallback_theme(doc: Document, suffix: str) -> str | None:
    """The bundled ``default`` theme, materialized on FIRST NEED, if it defines ``-{suffix}``.

    Installed with no routes at all: it dresses the roles (and the chart category) a document
    actually names and nothing else, so a document that never names one stays exactly as it
    would be with no theme engine in play. A project or user theme called ``default`` shadows
    the bundled one, the same way it does for an explicit ``load_theme``. Returns the NAME of
    the theme that answered, so a caller can go on to ask it for its other hooks too.
    """
    if DEFAULT_THEME in doc.theme_meta:
        return DEFAULT_THEME if _hook(doc, DEFAULT_THEME, suffix) is not None else None
    paths = default_search_paths()
    theme = _read_theme(DEFAULT_THEME, paths)
    if theme.name in doc.theme_meta:
        return theme.name if _hook(doc, theme.name, suffix) is not None else None
    result = materialize(theme)
    if f"{theme.name}-{suffix}" not in result.class_names:
        return None  # nothing to serve, so nothing is installed either
    _install(doc, theme, result, search_paths=paths, routes=())
    return theme.name


def _fallback_hook(doc: Document, role: str) -> str | None:
    """Serve a role no resident theme was asked to cover, from the bundled ``default`` theme."""
    name = _fallback_theme(doc, role)
    return None if name is None else _hook(doc, name, role)


def worn_theme(doc: Document, element: BaseElement, *suffixes: str) -> str | None:
    """The resident theme a facade is actually DRESSED BY, or None if it wears no hook at all.

    Read off the classes the element carries, matched against each resident theme's materialized
    class names — never off an insertion-order scan of the routing table, which answers with
    whoever serves a key TODAY and can disagree with the hook the facade was built with. The
    ``suffixes`` are tried in order of authority (a facade's own role first, then its category),
    so the answer is the theme that dressed this element, not merely one it shares a class with.

    None means the facade wears none of them — it was built ``themed=False``, and a rebuild that
    dressed it anyway would quietly overturn that decision.
    """
    worn = set(_class_list(element))
    for suffix in suffixes:
        for theme, meta in doc.theme_meta.items():
            name = f"{theme}-{suffix}"
            if name in worn and name in meta.class_names:
                return theme
    return None


def serving_theme_name(doc: Document, key: str) -> str | None:
    """Which RESIDENT theme's classes a facade's parts should wear for ``key``.

    Whoever is routed for the key; failing that, whichever resident theme actually defines a
    class for it — which is how the bundled default, installed route-free by the fallback above,
    is found again when a chart facade goes on to dress its axes and series. Nothing is loaded
    here: a key nothing resident answers for gets None, and the parts stay bare.
    """
    routed = doc.theme_routing.get(key)
    if routed is not None:
        return routed
    return next((name for name in doc.theme_meta if _hook(doc, name, key) is not None), None)


def _fallback_roles(doc: Document) -> list[str]:
    """What the fallback theme could have served — for the error when the named role is not it."""
    meta = doc.theme_meta.get(DEFAULT_THEME)
    if meta is not None:
        return sorted(cls.removeprefix(f"{DEFAULT_THEME}-") for cls in meta.class_names)
    with suppress(ThemeError):
        return sorted(_read_theme(DEFAULT_THEME, default_search_paths()).manifest.serves.roles)
    return []


@dataclass(frozen=True, slots=True)
class ServingTheme:
    """What a facade needs from the theme that will dress a role, before it has a node to hook.

    ``kinds`` is the manifest's diagram kind → shape primitive map and ``tokens`` its resolved
    custom properties — the geometry half of a role, where the CSS carries the paint half.
    """

    name: str | None = None
    kinds: Mapping[str, str] = dataclass_field(default_factory=dict)
    tokens: Mapping[str, str] = dataclass_field(default_factory=dict)


def serving_theme(doc: Document, role: str) -> ServingTheme:
    """The theme that will dress ``role``: whoever is routed for it, else the bundled ``default``.

    The same fallback :func:`apply_auto_styles` uses, asked one step earlier — a facade must know
    which shape a kind wants (and what to pad it by) BEFORE it has an element to hook. A resident
    theme answers from its recorded meta; otherwise the bundled default is READ but not installed,
    because installing it is ``_fallback_hook``'s call to make: a role nothing serves must leave no
    rules behind. A document with no themes at all and no readable default answers with nothing,
    and the caller falls back on its own defaults.
    """
    routed = doc.theme_routing.get(role)
    name = routed if routed is not None else DEFAULT_THEME
    meta = doc.theme_meta.get(name)
    if meta is not None:
        return ServingTheme(name=name, kinds=meta.kinds, tokens=meta.tokens)
    with suppress(ThemeError):
        theme = _read_theme(DEFAULT_THEME, default_search_paths())
        return ServingTheme(
            name=theme.name, kinds=theme.manifest.kinds, tokens=materialize(theme).tokens
        )
    return ServingTheme()


def resolve_auto_styles(
    doc: Document,
    *,
    category: str | None,
    prim: str | None = None,
    role: str | None = None,
    styles: Sequence[str] | None = None,
    themed: bool = True,
) -> list[str]:
    """Work out the class list a node would wear, WITHOUT needing the node — or touching the tree.

    Every way this can fail (an unservable role, a style name nothing defines) fails here, so a
    caller can validate a dressing before it commits any structure to the document and be sure
    that the matching :func:`apply_auto_styles` will not raise afterwards. See it for the rules.
    """
    classes: list[str] = []
    category_theme = doc.theme_routing.get(category) if category is not None else None
    if themed and category is not None:
        if category_theme is None and category in _FALLBACK_CATEGORIES:
            category_theme = _fallback_theme(doc, category)
        suffixes = [category, f"{category}--{prim}"] if prim else [category]
        classes.extend(
            hook for hook in (_hook(doc, category_theme, s) for s in suffixes) if hook is not None
        )
    if themed and role is not None:
        role_theme = doc.theme_routing.get(role) or category_theme
        hook = _hook(doc, role_theme, role)
        if hook is None and role_theme is None:
            hook = _fallback_hook(doc, role)
            if hook is None:
                offered = _fallback_roles(doc)
                raise InvalidArgument(
                    f"no theme is routed for role {role!r} and the fallback theme does not "
                    f"define it; the fallback offers: {offered or '(none)'}"
                )
        if hook is None:
            served = sorted(key for key in doc.theme_routing if key not in CATEGORIES)
            raise InvalidArgument(
                f"no resident theme defines a style for role {role!r}; "
                f"roles currently served: {served or '(none)'}"
            )
        classes.append(hook)
    classes.extend(resolve_style_ref(doc, ref) for ref in styles or ())
    return classes


def apply_auto_styles(
    doc: Document,
    element: BaseElement,
    *,
    category: str | None,
    prim: str | None = None,
    role: str | None = None,
    styles: Sequence[str] | None = None,
    themed: bool = True,
) -> list[str]:
    """Link a new node to the themes serving it, then to the styles the caller named.

    Hooks are attached in cascade order — category, primitive type, role — and only when the
    routed theme defines that class; the explicit ``styles`` go last so they win equal-specificity
    ties. ``themed=False`` skips the hooks but still attaches what the caller named explicitly.

    A role no theme was routed for at all falls back to the bundled ``default`` theme; a role the
    theme serving this category simply doesn't define is still an error, since that routing was
    a deliberate choice. An UNROUTED category falls back the same way, but only for the handful
    of categories in ``_FALLBACK_CATEGORIES`` — see there for why that is not all of them.
    """
    classes = resolve_auto_styles(
        doc, category=category, prim=prim, role=role, styles=styles, themed=themed
    )
    if classes:
        apply_styles(doc, str(element.get_id()), classes)
    return classes


# --- scoped apply / clear ----------------------------------------------------
#
# Residency dresses what is built NEXT; these two dress what is already there, over one subtree
# and without touching the routing table. The translation between themes runs on class SUFFIXES:
# a node wearing `house-service` is a service, so under `alt` it should wear `alt-service`.


def _owner(doc: Document, class_name: str) -> tuple[str, str] | None:
    """The materialized theme a class belongs to and its suffix, or None for a doc-local class."""
    for theme, meta in doc.theme_meta.items():
        if class_name in meta.class_names:
            return theme, class_name.removeprefix(f"{theme}-")
    return None


def _derived_hooks(doc: Document, theme: str, element: BaseElement) -> list[str]:
    """The category (and type) hooks this theme offers an element carrying no theme class.

    What the node IS comes off the ``data-category``/``data-prim`` stamped at construction —
    never off its id or name, which the caller is free to change and imported SVG supplies
    arbitrarily. A node without the stamp (imported, or foreign content) derives NOTHING; only
    its existing theme classes can be translated, since those state a role outright.
    """
    from .construct import _CATEGORY_ATTR, _PRIM_ATTR  # construct imports this module, not v.v.

    category = str(element.get(_CATEGORY_ATTR) or "")
    if not category:
        return []
    prim = str(element.get(_PRIM_ATTR) or "")
    suffixes = [category, f"{category}--{prim}"] if prim else [category]
    return [hook for hook in (_hook(doc, theme, s) for s in suffixes) if hook is not None]


def _scope(element: BaseElement) -> Iterator[BaseElement]:
    """The target and every element under it; comments and processing instructions are skipped."""
    for node in element.iter():
        if isinstance(node.tag, str):
            yield node


def _materialize_for_scope(
    doc: Document, theme: str, variant: str | None, search_paths: Sequence[Path] | None
) -> tuple[frozenset[str], str | None]:
    """Ensure ``theme``'s CSS is in the document, WITHOUT routing anything to it.

    Residency is routing; a scoped apply is a one-off dressing of a subtree, so the theme's
    rules land in the stylesheet and the routing table is left exactly as it was. A theme
    already resident keeps its routes — only its variant is honoured if a new one is named.
    """
    meta = doc.theme_meta.get(theme)
    if meta is not None and (variant is None or meta.variant == variant):
        return meta.class_names, meta.variant
    if search_paths is not None:
        paths = list(search_paths)
    elif meta is not None:
        paths = list(dict.fromkeys([meta.source.parent, *meta.search_paths]))
    else:
        paths = default_search_paths()
    loaded = _read_theme(theme, paths)
    routes = list(meta.routes) if meta is not None else []
    result = materialize_into(doc, loaded, variant, search_paths=paths, routes=routes)
    return result.class_names, result.variant


def apply_theme(
    doc: Document,
    target: str,
    theme: str,
    *,
    mode: Literal["paste", "replace"] = "paste",
    variant: str | None = None,
    search_paths: list[Path] | None = None,
) -> ThemeScopeChange:
    """Dress one subtree in a theme — the nodes that already exist, without changing routing.

    ``paste`` layers the theme on top: every node keeps the classes it has and gains this
    theme's equivalent of each (same suffix, e.g. ``house-service`` → ``alt-service``), which
    wins the cascade because this theme's block was materialized last. ``replace`` strips the
    other themes' classes first, carrying their roles across as it goes. A node wearing no theme
    class at all gets this theme's category hooks, derived from what it was built as.

    Classes no resident theme defines — anything from ``define_style`` — are never touched.
    """
    defines, used_variant = _materialize_for_scope(doc, theme, variant, search_paths)
    prefix = f"{theme}-"
    change = ThemeScopeChange(theme=theme, variant=used_variant)
    for node in _scope(doc.resolve(target)):
        current = _class_list(node)
        owned = [(cls, _owner(doc, cls)) for cls in current]
        foreign = [(cls, own[1]) for cls, own in owned if own is not None and own[0] != theme]
        mine = [cls for cls, own in owned if own is not None and own[0] == theme]
        drop = {cls for cls, _ in foreign} if mode == "replace" else set()
        add = [prefix + suffix for _, suffix in foreign if prefix + suffix in defines]
        # Category hooks are the fallback for a node no theme has dressed — a node that carries
        # a role (its own, another theme's, or one just translated) keeps that as its only word.
        if not add and not mine and (mode == "replace" or not foreign):
            add = _derived_hooks(doc, theme, node)
        remaining = [cls for cls in current if cls not in drop]
        gained = [cls for cls in dict.fromkeys(add) if cls not in remaining]
        if not gained and len(remaining) == len(current):
            continue
        _set_class_list(node, [*remaining, *gained])
        change.nodes_touched += 1
        change.classes_added += len(gained)
        change.classes_removed += len(current) - len(remaining)
    return change


def clear_theme(doc: Document, target: str, theme: str | None = None) -> ThemeScopeChange:
    """Undress one subtree: drop the classes of one materialized theme, or of every one.

    The rules stay in the document — this unlinks nodes from them. Doc-local styles from
    ``define_style`` are left alone, and a node left with no classes loses the attribute.
    """
    if theme is not None:
        _resident(doc, theme)
    names = {theme} if theme is not None else set(doc.theme_meta)
    change = ThemeScopeChange(theme=theme)
    for node in _scope(doc.resolve(target)):
        current = _class_list(node)
        kept = [
            cls for cls in current if (owner := _owner(doc, cls)) is None or owner[0] not in names
        ]
        if len(kept) == len(current):
            continue
        _set_class_list(node, kept)
        change.nodes_touched += 1
        change.classes_removed += len(current) - len(kept)
    return change
