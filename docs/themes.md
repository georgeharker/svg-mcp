# Themes — a design language for documents

A **theme** is a document-wide design language authored as real CSS: named styles, tokens,
and prose house rules, packaged in a directory the server loads by name. Documents that load
a theme get consistent styling automatically; agents authoring into one are told the rules.

## Theme package format

```
.svg-mcp/themes/<name>/
  styles.css          # the design language itself — real CSS (required)
  theme.toml          # manifest: what it serves, kind→shape map, variants (optional)
  variants/dark.css   # token overrides only (optional)
  guidance.md         # prose house rules, returned to the agent on load (optional)
```

A bare `<name>.css` dropped in the themes directory is also a valid minimal theme.
Search order: project `.svg-mcp/themes/`, then `~/.config/svg-mcp/themes/`, then the
bundled themes — first hit wins, so a project theme named `default` shadows the built-in.

### styles.css

Tokens are CSS custom properties declared in `:root`, consumed with `var()`:

```css
:root {
  --box-fill: #e9f1fa;
  --box-stroke: #1b4f8a;
}

/** A running service. */
.service { fill: var(--box-fill); stroke: var(--box-stroke); stroke-width: 3 }

/** Box labels never inherit the border. */
.service text { fill: var(--ink); stroke: none; font-family: Avenir Next, sans-serif }
```

- `var()` is resolved **server-side at materialization** — the emitted document CSS is
  var-free, so renderers never need custom-property support.
- Class names are written unprefixed and **namespaced on load** (`.service` →
  `.blueprint-service`), including inside descendant selectors.
- The selector grammar is the render-verified subset: type, class, descendant, and child
  (`>`) selectors. Attribute selectors, pseudo-classes, ids, `*`, and at-rules are
  rejected at load — loudly, not by silent non-rendering.
- A `/** ... */` doc-comment above a rule becomes that style's description in `list_styles`.
- Top-level type-selector rules (`text { font-family: ... }`) are allowed and deliberately
  un-namespaced: they are document-wide defaults.

### theme.toml

```toml
name = "blueprint"

[serves]
categories = ["shape"]                      # of: shape, text, connector, container, image, chart
roles = ["service", "datastore", "title"]   # open vocabulary

[kinds]                                     # the SHAPE half of a diagram kind
service = "squircle"
datastore = "rect"

[variants]
dark = "variants/dark.css"
```

Variant files may contain only `:root` token overrides — a dark mode is typically ten lines.

## Residency and routing

Themes on a document form a **role-routed set, not a stack**: each resident theme serves
specific route keys (categories and roles), and a key is served by exactly one theme.

- `load_theme(name)` — materialize + route the keys the manifest declares (or pass
  `roles=[...]` to take a subset). Taking a key another theme holds **evicts it, with a
  report**; `expect_free=true` errors instead. The response includes the theme's
  **guidance** and its style menu.
- `unload_theme` / `sync_theme` (re-read from disk) / `replace_theme` (swap the whole set).
- `set_theme_variant("dark")` — every resident re-materializes with that variant; themes
  without it keep base. `set_theme_variant()` returns all to base.
- `list_styles()` — every attachable class with its description; `describe_document` shows
  residency and routing.

## How nodes pick up styling

Constructors attach up to three class hooks the serving theme actually defines:
`{theme}-{category}` (e.g. `blueprint-shape`), `{theme}-{category}--{primitive}`
(`blueprint-shape--pill`), and `{theme}-{role}` (`blueprint-title`) — precedence
category < type < role, resolved by stylesheet order. Pass `role=` to say what a thing
*is*; pass `styles=["@name"]` to attach a specific named style; pass `themed=false` for a
bare node.

**The precedence rule in one sentence: if you typed it, it sticks; if the theme supplied
it, it stays linked.** A node's inline `style` is explicit — it beats every class rule and
deliberately does not follow theme or variant switches. Everything theme-derived updates
when the theme does.

## Scoped application

- `apply_theme(target, theme)` — dress an *existing* subtree without changing routing:
  nodes carrying another theme's classes gain this theme's counterparts by suffix
  (`default-service` → `+blueprint-service`), bare facade nodes derive hooks from their
  recorded category. `mode="replace"` strips other themes' classes first.
- `clear_theme(target)` — remove theme classes from a subtree.

## The bundled `default` theme

Ships with svg-mcp and implicitly serves any diagram/text/chart *role* no resident theme
routes — so diagrams and charts work in a document that loaded nothing, and a role-free
document is untouched by it (bare shapes stay bare). It is also the reference example for
theme authors: six diagram kinds, three edge kinds, five text roles, chart series palette,
annotation kinds, and a dark variant.
