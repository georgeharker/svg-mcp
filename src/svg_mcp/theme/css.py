"""The theme CSS pipeline: parse → lint → resolve var() → namespace → tier-sort → serialize.

Every step is a pure function over the structures defined here, so each is testable on its own
and materializing a variant is just a different token table fed to the same steps.

The accepted selector grammar is deliberately narrow — type, class, compound ``type.class``,
descendant, and child — because that is the subset verified to render identically through resvg.
Anything outside it is rejected at load time rather than silently ignored at render time.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final

import tinycss2
from tinycss2.ast import Node as CssNode

from ..model.errors import ThemeError

CATEGORIES: Final[tuple[str, ...]] = ("shape", "text", "connector", "container", "image", "chart")
"""The six node categories a theme may hook. Fixed — routing depends on the closed set."""

# tinycss2 token type -> what an author would call it, for the rejection message.
_REJECTED_TOKENS: Final[dict[str, str]] = {
    "hash": "id selector",
    "[] block": "attribute selector",
    "string": "string",
    "() block": "parenthesized group",
    "function": "functional pseudo-class",
}
_REJECTED_LITERALS: Final[dict[str, str]] = {
    ":": "pseudo-class or pseudo-element",
    "*": "universal selector",
    "+": "adjacent-sibling combinator",
    "~": "general-sibling combinator",
    "|": "namespace separator",
}


@dataclass(frozen=True, slots=True)
class Compound:
    """One compound selector: an optional type name plus zero or more class names."""

    element: str | None
    classes: tuple[str, ...]

    def text(self) -> str:
        return (self.element or "") + "".join(f".{name}" for name in self.classes)


@dataclass(frozen=True, slots=True)
class Selector:
    """A complex selector: compounds joined by descendant (`` ``) or child (`` > ``) combinators."""

    compounds: tuple[Compound, ...]
    combinators: tuple[str, ...]

    def text(self) -> str:
        parts = [self.compounds[0].text()]
        for combinator, compound in zip(self.combinators, self.compounds[1:], strict=True):
            parts.append(combinator)
            parts.append(compound.text())
        return "".join(parts)


@dataclass(frozen=True, slots=True)
class Declaration:
    """One CSS declaration, value kept as text (var() resolution rewrites it later)."""

    prop: str
    value: str


@dataclass(frozen=True, slots=True)
class Rule:
    """One qualified rule: a selector list, its declarations, and its ``/** */`` description."""

    selectors: tuple[Selector, ...]
    declarations: tuple[Declaration, ...]
    description: str | None = None

    def selector_text(self) -> str:
        return ", ".join(selector.text() for selector in self.selectors)

    def text(self) -> str:
        body = "; ".join(f"{d.prop}:{d.value}" for d in self.declarations)
        return f"{self.selector_text()} {{ {body} }}"


@dataclass(frozen=True, slots=True)
class ParsedCss:
    """A linted stylesheet: its rules in author order plus the tokens its ``:root`` blocks set."""

    rules: tuple[Rule, ...]
    tokens: Mapping[str, str]


# --- step 1-3, 7: parse, lint, collect tokens, attach descriptions ----------


def parse_css(text: str, *, origin: str) -> ParsedCss:
    """Parse and lint a theme stylesheet. ``origin`` labels the file in error messages."""
    rules: list[Rule] = []
    tokens: dict[str, str] = {}
    pending: str | None = None  # a /** */ description waiting for the rule it precedes
    for node in tinycss2.parse_stylesheet(text, skip_comments=False, skip_whitespace=True):
        kind = str(node.type)
        if kind == "comment":
            body = str(node.value)
            if body.startswith("*"):
                pending = body[1:].strip()
            continue
        if kind == "error":
            raise ThemeError(f"{origin}: CSS parse error: {node.message}")
        if kind == "at-rule":
            raise ThemeError(
                f"{origin}: at-rule '@{node.lower_at_keyword}' is not allowed in a theme "
                "stylesheet; themes are a flat list of plain rules"
            )
        if kind != "qualified-rule":
            continue
        raw_selector = _raw_selector(node.prelude)
        declarations = _parse_declarations(node.content, origin=origin, selector=raw_selector)
        if _is_root(node.prelude):
            for declaration in declarations:
                if not declaration.prop.startswith("--"):
                    raise ThemeError(
                        f"{origin}: ':root' may only declare tokens (custom properties); "
                        f"found '{declaration.prop}'"
                    )
                tokens[declaration.prop] = declaration.value
            pending = None
            continue
        selectors = _parse_selector_list(node.prelude, origin=origin)
        for declaration in declarations:
            if declaration.prop.startswith("--"):
                raise ThemeError(
                    f"{origin}: custom property '{declaration.prop}' is declared outside "
                    f"':root' (in rule '{raw_selector}'); tokens belong in a ':root' block"
                )
        rules.append(Rule(selectors=selectors, declarations=declarations, description=pending))
        pending = None
    return ParsedCss(rules=tuple(rules), tokens=tokens)


def parse_variant_css(text: str, *, origin: str) -> Mapping[str, str]:
    """Parse a variant overlay: ``:root`` token blocks only, anything else is a lint error."""
    parsed = parse_css(text, origin=origin)
    if parsed.rules:
        raise ThemeError(
            f"{origin}: a variant may only contain ':root' token blocks; found the rule "
            f"'{parsed.rules[0].selector_text()}'"
        )
    return parsed.tokens


def _raw_selector(prelude: Sequence[CssNode]) -> str:
    return str(tinycss2.serialize(prelude)).strip()


def _is_root(prelude: Sequence[CssNode]) -> bool:
    """True for a prelude that is exactly ``:root`` (the only place tokens may be declared)."""
    significant = [tok for tok in prelude if str(tok.type) != "whitespace"]
    if len(significant) != 2:
        return False
    first, second = significant
    return (
        str(first.type) == "literal"
        and str(first.value) == ":"
        and str(second.type) == "ident"
        and str(second.value) == "root"
    )


def _parse_declarations(
    content: Sequence[CssNode], *, origin: str, selector: str
) -> tuple[Declaration, ...]:
    out: list[Declaration] = []
    for node in tinycss2.parse_blocks_contents(content, skip_comments=True, skip_whitespace=True):
        kind = str(node.type)
        if kind == "error":
            raise ThemeError(f"{origin}: CSS parse error in rule '{selector}': {node.message}")
        if kind == "at-rule":
            raise ThemeError(
                f"{origin}: at-rule '@{node.lower_at_keyword}' is not allowed "
                f"(in rule '{selector}')"
            )
        if kind != "declaration":
            continue
        name = str(node.name)
        prop = name if name.startswith("--") else str(node.lower_name)
        value = str(tinycss2.serialize(node.value)).strip()
        if node.important:
            value = f"{value} !important"
        out.append(Declaration(prop=prop, value=value))
    return tuple(out)


def _parse_selector_list(prelude: Sequence[CssNode], *, origin: str) -> tuple[Selector, ...]:
    whole = _raw_selector(prelude)
    groups: list[list[CssNode]] = [[]]
    for token in prelude:
        if str(token.type) == "literal" and str(token.value) == ",":
            groups.append([])
            continue
        groups[-1].append(token)
    selectors = [_parse_selector(group, origin=origin, whole=whole) for group in groups]
    return tuple(selectors)


def _parse_selector(tokens: Sequence[CssNode], *, origin: str, whole: str) -> Selector:
    """Parse one comma-separated selector, rejecting anything outside the supported subset."""
    compounds: list[Compound] = []
    combinators: list[str] = []
    element: str | None = None
    classes: list[str] = []
    started = False
    pending: str | None = None  # a combinator seen but not yet consumed by a compound

    index = 0
    count = len(tokens)
    while index < count:
        token = tokens[index]
        kind = str(token.type)
        if kind == "whitespace":
            if started and pending is None:
                pending = " "
            index += 1
            continue
        is_class = kind == "literal" and str(token.value) == "."
        if kind == "ident" or is_class:
            if pending is not None:
                compounds.append(Compound(element=element, classes=tuple(classes)))
                combinators.append(pending)
                element, classes, pending = None, [], None
            if is_class:
                following = tokens[index + 1] if index + 1 < count else None
                if following is None or str(following.type) != "ident":
                    raise ThemeError(_bad(origin, whole, "a malformed class selector"))
                classes.append(str(following.value))
                index += 2
            else:
                if element is not None or classes:
                    raise ThemeError(_bad(origin, whole, "an unsupported selector construct"))
                element = str(token.value)
                index += 1
            started = True
            continue
        if kind == "literal":
            char = str(token.value)
            if char == ">":
                if not started:
                    raise ThemeError(_bad(origin, whole, "a leading child combinator"))
                pending = " > "
                index += 1
                continue
            raise ThemeError(_bad(origin, whole, f"a {_REJECTED_LITERALS.get(char, char)}"))
        raise ThemeError(_bad(origin, whole, f"an {_REJECTED_TOKENS.get(kind, kind)}"))

    if not started:
        raise ThemeError(_bad(origin, whole, "an empty selector"))
    compounds.append(Compound(element=element, classes=tuple(classes)))
    return Selector(compounds=tuple(compounds), combinators=tuple(combinators))


def _bad(origin: str, selector: str, what: str) -> str:
    return (
        f"{origin}: selector '{selector}' uses {what}, which theme CSS does not support; "
        "use type, class, compound (type.class), descendant, or child selectors only"
    )


# --- step 4: var() resolution ----------------------------------------------


def resolve_token_table(tokens: Mapping[str, str], *, origin: str) -> dict[str, str]:
    """Resolve the token table against ITSELF, to a fixpoint; the result is var-free.

    Tokens routinely chain — ``--ink: var(--gray-900)`` is the ordinary way to name a palette
    entry — and a table left unresolved hands that literal ``var(--gray-900)`` to every consumer
    that reads a token directly (a chart's ink, a facade's padding) rather than through a rule.
    The contract is the rules': a reference cycle names the chain, and a var() naming a token
    that is neither declared nor given a fallback is a load error rather than a silent drop.
    """
    return {
        name: _resolve_value(
            value, tokens, origin=origin, prop=name, selector=":root", seen=(name,)
        )
        for name, value in tokens.items()
    }


def resolve_vars(
    rules: Sequence[Rule], tokens: Mapping[str, str], *, origin: str
) -> tuple[Rule, ...]:
    """Substitute every ``var(--x)`` / ``var(--x, fallback)`` from ``tokens``; output is var-free.

    A token that is neither declared nor given a fallback is a load error, not a silent drop.
    """
    resolved: list[Rule] = []
    for rule in rules:
        selector = rule.selector_text()
        resolved.append(
            Rule(
                selectors=rule.selectors,
                declarations=tuple(
                    Declaration(
                        prop=declaration.prop,
                        value=_resolve_value(
                            declaration.value,
                            tokens,
                            origin=origin,
                            prop=declaration.prop,
                            selector=selector,
                            seen=(),
                        ),
                    )
                    for declaration in rule.declarations
                ),
                description=rule.description,
            )
        )
    return tuple(resolved)


def _resolve_value(
    value: str,
    tokens: Mapping[str, str],
    *,
    origin: str,
    prop: str,
    selector: str,
    seen: tuple[str, ...],
) -> str:
    out: list[str] = []
    index = 0
    while True:
        start = value.find("var(", index)
        if start < 0:
            out.append(value[index:])
            break
        if start > 0 and (value[start - 1].isalnum() or value[start - 1] in "-_"):
            out.append(value[index : start + 4])  # part of a longer identifier, not a var()
            index = start + 4
            continue
        out.append(value[index:start])
        end = _closing_paren(value, start + 3)
        if end < 0:
            raise ThemeError(f"{origin}: unbalanced var() in '{prop}' of rule '{selector}'")
        name, fallback = _split_var(value[start + 4 : end])
        if not name.startswith("--"):
            raise ThemeError(
                f"{origin}: var({name}) in '{prop}' of rule '{selector}' is not a token; "
                "tokens are custom properties and start with '--'"
            )
        if name in seen:
            chain = " -> ".join([*seen, name])
            raise ThemeError(f"{origin}: token reference cycle: {chain}")
        if name in tokens:
            source, next_seen = tokens[name], (*seen, name)
        elif fallback is not None:
            source, next_seen = fallback, seen
        else:
            raise ThemeError(
                f"{origin}: unknown token '{name}' used by '{prop}' in rule '{selector}'; "
                "declare it in a ':root' block or give the var() a fallback"
            )
        out.append(
            _resolve_value(
                source, tokens, origin=origin, prop=prop, selector=selector, seen=next_seen
            )
        )
        index = end + 1
    return "".join(out)


def _closing_paren(value: str, open_index: int) -> int:
    depth = 0
    for index in range(open_index, len(value)):
        if value[index] == "(":
            depth += 1
        elif value[index] == ")":
            depth -= 1
            if depth == 0:
                return index
    return -1


def _split_var(inner: str) -> tuple[str, str | None]:
    """Split a var()'s contents into (token name, fallback) at the first top-level comma."""
    depth = 0
    for index, char in enumerate(inner):
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif char == "," and depth == 0:
            return inner[:index].strip(), inner[index + 1 :].strip()
    return inner.strip(), None


# --- step 5: namespacing ----------------------------------------------------


def namespace_rules(rules: Sequence[Rule], name: str) -> tuple[Rule, ...]:
    """Prefix every class token with ``{name}-`` (idempotent); type selectors are left alone."""
    prefix = f"{name}-"

    def prefixed(class_name: str) -> str:
        return class_name if class_name.startswith(prefix) else prefix + class_name

    return tuple(
        Rule(
            selectors=tuple(
                Selector(
                    compounds=tuple(
                        Compound(
                            element=compound.element,
                            classes=tuple(prefixed(c) for c in compound.classes),
                        )
                        for compound in selector.compounds
                    ),
                    combinators=selector.combinators,
                )
                for selector in rule.selectors
            ),
            declarations=rule.declarations,
            description=rule.description,
        )
        for rule in rules
    )


# --- step 6: tier sort ------------------------------------------------------
#
# Stylesheet order breaks equal-specificity ties, so precedence is encoded as order:
# category hooks < type hooks < role hooks < everything else (part rules and bare type rules).


def tier_of(selector: Selector, name: str, roles: Collection[str]) -> int:
    """The precedence tier of one namespaced selector (0 category … 3 specific)."""
    if len(selector.compounds) != 1:
        return 3
    compound = selector.compounds[0]
    if compound.element is not None or len(compound.classes) != 1:
        return 3
    class_name = compound.classes[0]
    prefix = f"{name}-"
    if not class_name.startswith(prefix):
        return 3
    suffix = class_name[len(prefix) :]
    if suffix in CATEGORIES:
        return 0
    category, separator, kind = suffix.partition("--")
    if separator and category in CATEGORIES and kind:
        return 1
    if suffix in roles:
        return 2
    return 3


def tier_sort(rules: Sequence[Rule], name: str, roles: Collection[str]) -> tuple[Rule, ...]:
    """Order rules by tier, stable within a tier so author order is preserved.

    A rule with a selector list takes the most general tier any of its selectors reaches.
    """
    return tuple(
        sorted(rules, key=lambda rule: min(tier_of(s, name, roles) for s in rule.selectors))
    )


# --- steps 7-8: descriptions and serialization ------------------------------


def collect_class_names(rules: Iterable[Rule]) -> frozenset[str]:
    """Every class name the sheet targets, from ANY compound position — what the theme "defines".

    A compound naming several classes at once (``.a.b``) defines neither on its own, so it is
    skipped; ``.container > .label`` defines both, since attaching either class is meaningful.
    """
    return frozenset(
        compound.classes[0]
        for rule in rules
        for selector in rule.selectors
        for compound in selector.compounds
        if len(compound.classes) == 1
    )


def collect_descriptions(rules: Iterable[Rule]) -> dict[str, str]:
    """Map each documented rule's first class name to its ``/** */`` description."""
    out: dict[str, str] = {}
    for rule in rules:
        if rule.description is None:
            continue
        for selector in rule.selectors:
            keyed = next((c.classes[0] for c in selector.compounds if c.classes), None)
            if keyed is not None:
                out.setdefault(keyed, rule.description)
                break
    return out


def serialize_rules(rules: Iterable[Rule]) -> str:
    """Compact CSS, one rule per line — the same shape the named-style sheet emits."""
    return "\n".join(rule.text() for rule in rules)
